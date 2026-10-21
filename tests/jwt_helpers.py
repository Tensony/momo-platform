"""Throw-away RSA key + Cognito-shaped tokens for tests. Nothing here is committed key material:
the key is generated fresh in memory on every test run."""
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://cognito-idp.af-south-1.amazonaws.com/af-south-1_TESTPOOL"
CLIENT_ID = "test-client-id"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SigningKey:
    def __init__(self, key):
        self.key = key


class StubJwks:
    def __init__(self, key=None):
        self.key = key or _KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return _SigningKey(self.key)


def make_token(sub=None, groups=(), *, issuer=ISSUER, client_id=CLIENT_ID, token_use="access",
               exp_in=3600, key=None, alg="RS256", extra=None, drop=()):
    now = int(time.time())
    claims = {"sub": sub or str(uuid.uuid4()), "iss": issuer, "client_id": client_id,
              "token_use": token_use, "iat": now, "exp": now + exp_in, "username": "tester"}
    if groups:
        claims["cognito:groups"] = list(groups)
    claims.update(extra or {})
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, key or _KEY, algorithm=alg)


def private_key():
    return _KEY
