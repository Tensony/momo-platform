import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from momo.api.auth import JwtVerifier, user_from_header
from momo.common.errors import NotAuthenticated, NotAuthorized
from tests.jwt_helpers import CLIENT_ID, ISSUER, StubJwks, make_token

V = JwtVerifier(ISSUER, CLIENT_ID, StubJwks())


def test_valid_token_yields_user_with_groups():
    u = V.verify(make_token("user-1", ["agents"]))
    assert u.sub == "user-1" and "agents" in u.groups and not u.is_admin


def test_admin_passes_every_group_check():
    u = V.verify(make_token("a", ["admins"]))
    u.require_group("agents")
    with pytest.raises(NotAuthorized):
        V.verify(make_token("b")).require_group("agents")


@pytest.mark.parametrize("kwargs", [
    dict(exp_in=-60),                                   # expired
    dict(issuer="https://evil.example.com/pool"),       # wrong issuer
    dict(client_id="someone-elses-app"),                # token for another client
    dict(token_use="id"),                               # ID token, not access token
    dict(drop=("sub",)),                                # no subject
    dict(drop=("exp",)),                                # no expiry
])
def test_bad_claims_rejected(kwargs):
    with pytest.raises(NotAuthenticated):
        V.verify(make_token(**kwargs))


def test_token_signed_with_other_key_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(NotAuthenticated):
        V.verify(make_token(key=other))


def test_algorithm_downgrade_rejected():
    forged = jwt.encode({"sub": "x", "iss": ISSUER, "client_id": CLIENT_ID, "token_use": "access",
                         "exp": 4102444800}, "secret", algorithm="HS256")
    with pytest.raises(NotAuthenticated):
        V.verify(forged)
    none_token = jwt.encode({"sub": "x", "iss": ISSUER}, None, algorithm="none")
    with pytest.raises(NotAuthenticated):
        V.verify(none_token)


def test_garbage_and_header_parsing():
    with pytest.raises(NotAuthenticated):
        V.verify("not-a-jwt")
    for header in (None, "", "Basic abc", "Bearer"):
        with pytest.raises(NotAuthenticated):
            user_from_header(V, header)
    assert user_from_header(V, "Bearer " + make_token("u")).sub == "u"
