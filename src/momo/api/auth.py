"""Authentication: verification of Amazon Cognito access tokens and local platform JWTs.

Features:
  * BCrypt password protection for user credentials.
  * Role-Based Access Control (RBAC) via User groups: customers, agents, admins, merchants.
  * Dual-mode JWT verification:
      1. Local platform access tokens signed with HMAC-SHA256 (HS256).
      2. Cognito user pool access tokens verified against JWKS (RS256).
  * Enforces algorithms, expiration, issuer, subject, and token_use='access'.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field

import bcrypt
import jwt
from jwt import PyJWKClient

from ..common.errors import NotAuthenticated, NotAuthorized

LOCAL_JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_urlsafe(48)
LOCAL_ISSUER = "momo-platform"
LOCAL_CLIENT_ID = "momo-platform"
LOCAL_ACCESS_TOKEN_TTL_SECONDS = 60 * 60


def hash_password(password: str) -> str:
    """Hash password securely using BCrypt with salt rounds=12."""
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    try:
        encoded_password = password.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Password must contain valid UTF-8 text") from exc
    if len(encoded_password) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(encoded_password, salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against BCrypt hash."""
    if not isinstance(password, str) or not password or not isinstance(hashed, str) or not hashed:
        return False
    try:
        encoded_password = password.encode("utf-8")
        if len(encoded_password) > 72:
            return False
        return bcrypt.checkpw(encoded_password, hashed.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return False


def issue_token(sub: str, username: str, role: str, exp_hours: int = 1) -> str:
    """Mint a local platform JWT access token with role-based groups."""
    now = int(time.time())
    # Standardize group name to match RBAC checks (customers, agents, admins, merchants)
    group = f"{role}s" if not role.endswith("s") else role
    claims = {
        "sub": str(sub),
        "username": username,
        "role": role,
        "cognito:groups": [group],
        "token_use": "access",
        "client_id": LOCAL_CLIENT_ID,
        "iss": LOCAL_ISSUER,
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + (exp_hours * 3600),
    }
    return jwt.encode(claims, LOCAL_JWT_SECRET, algorithm="HS256")


@dataclass(frozen=True)
class User:
    sub: str
    groups: frozenset[str] = field(default_factory=frozenset)
    username: str | None = None
    token_id: str | None = None
    token_expires_at: int | None = None
    local_token: bool = False

    @property
    def is_admin(self) -> bool:
        return "admins" in self.groups

    @property
    def is_agent(self) -> bool:
        return "agents" in self.groups or self.is_admin

    @property
    def is_customer(self) -> bool:
        return "customers" in self.groups

    @property
    def role(self) -> str | None:
        for group, role in (
            ("admins", "admin"),
            ("agents", "agent"),
            ("merchants", "merchant"),
            ("customers", "customer"),
        ):
            if group in self.groups:
                return role
        return None

    def require_group(self, group: str) -> None:
        if group not in self.groups and not self.is_admin:
            raise NotAuthorized(f"requires membership of the '{group}' group")


class JwtVerifier:
    def __init__(self, issuer: str, client_id: str, jwks_client):
        self.issuer = issuer
        self.client_id = client_id
        self.jwks_client = jwks_client

    @classmethod
    def for_cognito(cls, region: str, user_pool_id: str, client_id: str) -> "JwtVerifier":
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        jwks = PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600)
        return cls(issuer, client_id, jwks)

    @classmethod
    def from_env(cls) -> "JwtVerifier":
        return cls.for_cognito(
            os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "af-south-1"),
            os.environ.get("COGNITO_USER_POOL_ID", "local-pool"),
            os.environ.get("COGNITO_CLIENT_ID", "local-client"),
        )

    def verify(self, token: str) -> User:
        if not isinstance(token, str) or not token.strip():
            raise NotAuthenticated("invalid or expired token")

        # Inspect unverified claims to determine token issuer
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception as exc:
            raise NotAuthenticated("invalid or expired token") from exc

        # 1. Local platform tokens
        if unverified.get("iss") == LOCAL_ISSUER:
            try:
                claims = jwt.decode(
                    token,
                    LOCAL_JWT_SECRET,
                    algorithms=["HS256"],
                    issuer=LOCAL_ISSUER,
                    options={"require": ["exp", "iss", "sub"], "verify_aud": False},
                    leeway=5,
                )
            except Exception as exc:
                raise NotAuthenticated("invalid or expired token") from exc

            if claims.get("token_use") != "access":
                raise NotAuthenticated("an access token is required")
            if claims.get("client_id") != LOCAL_CLIENT_ID:
                raise NotAuthenticated("token was not issued for this application")

            return User(
                sub=claims["sub"],
                groups=frozenset(claims.get("cognito:groups", [])),
                username=claims.get("username"),
                token_id=claims.get("jti"),
                token_expires_at=claims.get("exp"),
                local_token=True,
            )

        # 2. External Cognito user pool tokens (RS256 via JWKS)
        try:
            key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
                leeway=5,
            )
        except Exception as exc:  # PyJWTError, JWKS fetch errors, malformed tokens ...
            raise NotAuthenticated("invalid or expired token") from exc

        if claims.get("token_use") != "access":
            raise NotAuthenticated("an access token is required")
        if claims.get("client_id") != self.client_id:
            raise NotAuthenticated("token was not issued for this application")

        return User(
            sub=claims["sub"],
            groups=frozenset(claims.get("cognito:groups", [])),
            username=claims.get("username"),
            token_id=claims.get("jti"),
            token_expires_at=claims.get("exp"),
        )


def user_from_header(verifier: JwtVerifier, header: str | None) -> User:
    if not header or not header.lower().startswith("bearer "):
        raise NotAuthenticated("missing bearer token")
    return verifier.verify(header[7:].strip())
