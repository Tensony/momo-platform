"""User repository and authentication service.

Supports both PostgreSQL-backed persistence and a memory-backed fallback for standalone/demo usage.
Enforces:
  * BCrypt password protection.
  * Role separation: customer, agent, admin.
  * Prevention of self-registration for agent and admin roles.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone

from ..common.errors import BusinessRejection, InvalidRequest, NotAuthenticated
from ..common.money import parse_currency
from .auth import LOCAL_ACCESS_TOKEN_TTL_SECONDS, hash_password, issue_token, verify_password

PHONE_RE = re.compile(r"^\+?[0-9]{9,15}$")

# Pre-seeded users for presentation/demonstration
DEFAULT_SEED_USERS = [
    {
        "id": "10000000-0000-0000-0000-000000000001",
        "username": "admin@momo.platform",
        "phone": "+260971000000",
        "email": "admin@momo.platform",
        "role": "admin",
        # Default pass: Admin123!
        "password_hash": "$2b$12$4v0Pj933P7fUqLwUeG5e5O2XzYwZ5sJpT5XkPz8mX7zM4zV8jT9yK",
        "full_name": "System Administrator",
        "status": "ACTIVE",
    },
    {
        "id": "10000000-0000-0000-0000-000000000002",
        "username": "AGENT-007",
        "phone": "+260971000007",
        "email": "agent007@momo.platform",
        "role": "agent",
        # Default pass: Agent123!
        "password_hash": "$2b$12$4v0Pj933P7fUqLwUeG5e5O2XzYwZ5sJpT5XkPz8mX7zM4zV8jT9yK",
        "full_name": "Lusaka Agent Terminal 007",
        "status": "ACTIVE",
    },
    {
        "id": "10000000-0000-0000-0000-000000000003",
        "username": "+260971000001",
        "phone": "+260971000001",
        "email": "alice@example.com",
        "role": "customer",
        # Default pass: Customer123!
        "password_hash": "$2b$12$4v0Pj933P7fUqLwUeG5e5O2XzYwZ5sJpT5XkPz8mX7zM4zV8jT9yK",
        "full_name": "Alice Mwanza",
        "status": "ACTIVE",
    },
]

# Thread-safe in-memory cache / fallback store
_store_lock = threading.Lock()
_memory_users: dict[str, dict] = {}
_revoked_tokens: dict[str, int] = {}


def _init_memory_store():
    global _memory_users
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        return
    if os.environ.get("AUTH_DEMO_USERS", "").lower() != "true":
        return
    if not _memory_users:
        with _store_lock:
            if not _memory_users:
                # Pre-hash real passwords for the seeds
                admin_pw = hash_password("Admin123!")
                agent_pw = hash_password("Agent123!")
                cust_pw = hash_password("Customer123!")
                for seed, pw in (
                    (DEFAULT_SEED_USERS[0], admin_pw),
                    (DEFAULT_SEED_USERS[1], agent_pw),
                    (DEFAULT_SEED_USERS[2], cust_pw),
                ):
                    u = {**seed, "password_hash": pw, "created_at": datetime.now(timezone.utc).isoformat()}
                    _memory_users[u["id"]] = u


_init_memory_store()


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]", "", str(phone or "").strip())
    if not PHONE_RE.match(cleaned):
        raise InvalidRequest("Enter a valid mobile phone number (9–15 digits)")
    return cleaned


def validate_password_strength(password: str) -> None:
    if not isinstance(password, str) or len(password) < 8:
        raise InvalidRequest("Password must be at least 8 characters long")
    try:
        if len(password.encode("utf-8")) > 72:
            raise InvalidRequest("Password must be at most 72 UTF-8 bytes")
    except UnicodeError as exc:
        raise InvalidRequest("Password must contain valid text") from exc
    if not any(c.isdigit() for c in password):
        raise InvalidRequest("Password must include at least one digit")
    if not any(c.isalpha() for c in password):
        raise InvalidRequest("Password must include at least one letter")


def _get_user_by_identifier(identifier: str, role: str | None = None) -> dict | None:
    ident = str(identifier or "").strip().lower()
    norm_phone = re.sub(r"[\s\-\(\)]", "", ident)

    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        from ..common.db import transaction

        with transaction() as conn:
            role_filter = "AND role = %s" if role is not None else ""
            params = (ident, norm_phone, ident, role) if role is not None else (ident, norm_phone, ident)
            row = conn.execute(
                f"""SELECT id, username, phone, email, role, password_hash, full_name, status, created_at
                      FROM users
                     WHERE (LOWER(username) = %s OR phone = %s OR LOWER(email) = %s)
                       {role_filter}""",
                params,
            ).fetchone()
            if row:
                return dict(row)
        return None

    _init_memory_store()
    with _store_lock:
        for u in _memory_users.values():
            if role and u.get("role") != role:
                continue
            username_match = (u.get("username") or "").lower() == ident
            phone_match = (u.get("phone") or "") == norm_phone if norm_phone else False
            email_match = (u.get("email") or "").lower() == ident
            if username_match or phone_match or email_match:
                return u
    return None


def register_customer(
    phone: str,
    password: str,
    full_name: str,
    currency: str = "ZMW",
) -> dict:
    """Register a new customer account. Self-registration is strictly restricted to customers."""
    phone = normalize_phone(phone)
    validate_password_strength(password)
    full_name = str(full_name or "").strip()
    if not full_name:
        raise InvalidRequest("Full name is required")

    existing = _get_user_by_identifier(phone)
    if existing:
        raise BusinessRejection("An account with this phone number already exists")

    currency = parse_currency(currency or "ZMW")
    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    created_at = datetime.now(timezone.utc).isoformat()

    user_record = {
        "id": user_id,
        "username": phone,
        "phone": phone,
        "email": None,
        "role": "customer",
        "password_hash": pw_hash,
        "full_name": full_name,
        "status": "ACTIVE",
        "created_at": created_at,
    }

    wallet_info = None
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        import psycopg

        from .. import service
        from ..common.db import transaction

        try:
            with transaction() as conn:
                conn.execute(
                    """INSERT INTO users (id, username, phone, email, role, password_hash, full_name, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, phone, phone, None, "customer", pw_hash, full_name, "ACTIVE"),
                )
                wallet_info = service.create_wallet(user_id, "CUSTOMER", currency, conn=conn)
        except psycopg.errors.UniqueViolation as exc:
            raise BusinessRejection("An account with this phone number already exists") from exc
    else:
        with _store_lock:
            if any(
                item.get("phone") == phone or item.get("username", "").lower() == phone.lower()
                for item in _memory_users.values()
            ):
                raise BusinessRejection("An account with this phone number already exists")
            _memory_users[user_id] = user_record

    token = issue_token(user_id, phone, "customer")
    return {
        "token": token,
        "user": {
            "id": user_id,
            "username": phone,
            "phone": phone,
            "role": "customer",
            "full_name": full_name,
            "groups": ["customers"],
        },
        "wallet": wallet_info,
        "expires_in": LOCAL_ACCESS_TOKEN_TTL_SECONDS,
    }


def authenticate_user(identifier: str, password: str, expected_role: str) -> dict:
    """Authenticate credentials for a specific role portal."""
    if expected_role not in {"customer", "agent", "admin"}:
        raise InvalidRequest("Unsupported login portal")
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or not isinstance(password, str)
        or not password
        or len(identifier) > 254
    ):
        raise InvalidRequest("Username / identifier and password are required")
    try:
        if len(password.encode("utf-8")) > 72:
            raise InvalidRequest("Password must be at most 72 UTF-8 bytes")
    except UnicodeError as exc:
        raise InvalidRequest("Password must contain valid text") from exc

    user = _get_user_by_identifier(identifier, expected_role)
    if (
        not user
        or user.get("status") != "ACTIVE"
        or not verify_password(password, user.get("password_hash", ""))
    ):
        raise NotAuthenticated("Invalid credentials.")

    user_id = str(user["id"])
    username = user.get("username") or user.get("phone") or identifier
    role = user["role"]
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        from ..common.db import transaction

        with transaction() as conn:
            conn.execute("UPDATE users SET last_login = now() WHERE id = %s", (user_id,))
    else:
        with _store_lock:
            stored_user = _memory_users.get(user_id)
            if stored_user:
                stored_user["last_login"] = datetime.now(timezone.utc).isoformat()
    token = issue_token(user_id, username, role)

    return {
        "token": token,
        "user": {
            "id": user_id,
            "username": username,
            "phone": user.get("phone"),
            "email": user.get("email"),
            "role": role,
            "full_name": user.get("full_name"),
            "groups": [f"{role}s"],
        },
        "expires_in": LOCAL_ACCESS_TOKEN_TTL_SECONDS,
    }


def is_token_revoked(token_id: str) -> bool:
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        from ..common.db import transaction

        with transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM revoked_tokens WHERE token_id = %s AND expires_at > now()",
                (token_id,),
            ).fetchone()
        return row is not None

    now = int(datetime.now(timezone.utc).timestamp())
    with _store_lock:
        for expired_id in [key for key, expires_at in _revoked_tokens.items() if expires_at <= now]:
            del _revoked_tokens[expired_id]
        return token_id in _revoked_tokens


def revoke_token(token_id: str, expires_at: int) -> None:
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        from ..common.db import transaction

        with transaction() as conn:
            conn.execute("DELETE FROM revoked_tokens WHERE expires_at <= now()")
            conn.execute(
                """INSERT INTO revoked_tokens (token_id, expires_at)
                   VALUES (%s, to_timestamp(%s))
                   ON CONFLICT (token_id) DO NOTHING""",
                (token_id, expires_at),
            )
        return

    with _store_lock:
        _revoked_tokens[token_id] = expires_at
