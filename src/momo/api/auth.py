"""Authentication: verification of Amazon Cognito access tokens (JWT, RS256).

Correct-use checklist (each item is unit-tested in tests/unit/test_auth.py):
  * signature verified against the user pool's JWKS (keys fetched over TLS, cached)
  * algorithm pinned to RS256 - 'none' / HS256 downgrade attacks are rejected
  * `iss` must be exactly this user pool
  * `token_use` must be "access" (ID tokens are for the client app, not for calling APIs)
  * `client_id` must be OUR app client (tokens minted for other clients are rejected)
  * `exp` enforced; `sub` required
Authorization uses Cognito groups from the `cognito:groups` claim: customers, merchants, agents, admins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import jwt
from jwt import PyJWKClient

from ..common.errors import NotAuthenticated, NotAuthorized


@dataclass(frozen=True)
class User:
    sub: str
    groups: frozenset[str] = field(default_factory=frozenset)
    username: str | None = None

    @property
    def is_admin(self) -> bool:
        return "admins" in self.groups

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
            os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"],
            os.environ["COGNITO_USER_POOL_ID"],
            os.environ["COGNITO_CLIENT_ID"],
        )

    def verify(self, token: str) -> User:
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
        )


def user_from_header(verifier: JwtVerifier, header: str | None) -> User:
    if not header or not header.lower().startswith("bearer "):
        raise NotAuthenticated("missing bearer token")
    return verifier.verify(header[7:].strip())
