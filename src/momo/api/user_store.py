"""User repository and authentication service.

Supports both PostgreSQL-backed persistence and a memory-backed fallback for standalone/demo usage.
Enforces:
  * BCrypt password protection.
  * Role separation: customer, agent, admin.
  * Prevention of self-registration for agent and admin roles.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import datetime, timezone

from ..common.errors import BusinessRejection, InvalidRequest, NotAuthenticated
from .auth import hash_password, issue_token, verify_password

log = logging.getLogger(__name__)

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


def _init_memory_store():
    global _memory_users
    if not _memory_users:
        with _store_lock:
            if not _memory_users:
                # Pre-hash real passwords for the seeds
                admin_pw = hash_password("Admin123!")
                agent_pw = hash_password("Agent123!")
                cust_pw = hash_password("Customer123!")
                for seed, pw in zip(DEFAULT_SEED_USERS, [admin_pw, agent_pw, cust_pw]):
                    u = {**seed, "password_hash": pw, "created_at": datetime.now(timezone.utc).isoformat()}
                    _memory_users[u["id"]] = u


_init_memory_store()


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]", "", str(phone or "").strip())
    if not PHONE_RE.match(cleaned):
        raise InvalidRequest("Enter a valid mobile phone number (9–15 digits)")
    return cleaned


def validate_password_strength(password: str) -> None:
    if not password or len(password) < 8:
        raise InvalidRequest("Password must be at least 8 characters long")
    if not any(c.isdigit() for c in password):
        raise InvalidRequest("Password must include at least one digit")
    if not any(c.isalpha() for c in password):
        raise InvalidRequest("Password must include at least one letter")


def _get_user_by_identifier(identifier: str, role: str | None = None) -> dict | None:
    ident = str(identifier or "").strip().lower()
    norm_phone = re.sub(r"[\s\-\(\)]", "", ident)

    # Check database if available
    try:
        from ..common.db import transaction

        with transaction() as conn:
            row = conn.execute(
                """SELECT id, username, phone, email, role, password_hash, full_name, status, created_at
                     FROM users
                    WHERE (LOWER(username) = %s OR phone = %s OR LOWER(email) = %s)
                      AND (%s IS NULL OR role = %s)""",
                (ident, norm_phone, ident, role, role),
            ).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass

    # Fallback to memory store
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

    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)
    created_at = datetime.now(timezone.utc).isoformat()
    currency = (currency or "ZMW").upper()

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

    # Persist to database if available
    wallet_info = None
    try:
        from .. import service
        from ..common.db import transaction

        with transaction() as conn:
            conn.execute(
                """INSERT INTO users (id, username, phone, email, role, password_hash, full_name, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, phone, phone, None, "customer", pw_hash, full_name, "ACTIVE"),
            )
        try:
            wallet_info = service.create_wallet(user_id, "CUSTOMER", currency)
        except Exception as e:
            log.warning("Could not automatically seed wallet on register: %s", e)
    except Exception:
        # Save to memory store
        with _store_lock:
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
    }


def authenticate_user(identifier: str, password: str, expected_role: str) -> dict:
    """Authenticate credentials for a specific role portal."""
    if not identifier or not password:
        raise InvalidRequest("Username / identifier and password are required")

    user = _get_user_by_identifier(identifier, expected_role)
    if not user:
        # Fallback check for seed credentials in memory
        _init_memory_store()
        user = _get_user_by_identifier(identifier, expected_role)

    if not user:
        raise NotAuthenticated(f"Invalid credentials for {expected_role} portal")

    if user.get("status") != "ACTIVE":
        raise BusinessRejection(f"Account is {user.get('status', 'SUSPENDED')}. Contact support.")

    if not verify_password(password, user["password_hash"]):
        raise NotAuthenticated(f"Invalid credentials for {expected_role} portal")

    user_id = str(user["id"])
    username = user.get("username") or user.get("phone") or identifier
    role = user.get("role", expected_role)
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
    }

