"""Unit and integration tests for role-based authentication and access control."""

import pytest

from momo.api.app import create_app
from momo.api.auth import JwtVerifier, hash_password, issue_token, verify_password
from momo.api.user_store import authenticate_user, register_customer
from momo.common.errors import BusinessRejection, NotAuthenticated
from tests.jwt_helpers import CLIENT_ID, ISSUER, StubJwks


@pytest.fixture
def client():
    app = create_app(JwtVerifier(ISSUER, CLIENT_ID, StubJwks()))
    app.config["TESTING"] = True
    return app.test_client()


def test_bcrypt_hashing_and_verification():
    pw = "SecurePass123!"
    h = hash_password(pw)
    assert h.startswith("$2b$")
    assert verify_password(pw, h)
    assert not verify_password("WrongPass123!", h)
    assert not verify_password("", h)


def test_password_strength_enforced():
    with pytest.raises(ValueError):
        hash_password("short")
    with pytest.raises(ValueError):
        hash_password("é" * 37)


def test_customer_registration_and_login():
    phone = "+260979998877"
    reg = register_customer(phone, "CustomerPass1!", "Test User", "ZMW")
    assert reg["user"]["phone"] == phone
    assert reg["user"]["role"] == "customer"
    assert "token" in reg
    assert reg["user"]["groups"] == ["customers"]

    # Login with phone
    auth = authenticate_user(phone, "CustomerPass1!", "customer")
    assert auth["user"]["role"] == "customer"
    assert "token" in auth

    # Wrong password fails
    with pytest.raises(NotAuthenticated):
        authenticate_user(phone, "WrongPassword!", "customer")

    # Duplicate phone registration fails
    with pytest.raises(BusinessRejection):
        register_customer(phone, "AnotherPass1!", "Duplicate User", "ZMW")


def test_role_cross_portal_isolation():
    phone = "+260971000001"
    # Customer cannot login through agent portal
    with pytest.raises(NotAuthenticated):
        authenticate_user(phone, "Customer123!", "agent")

    # Customer cannot login through admin portal
    with pytest.raises(NotAuthenticated):
        authenticate_user(phone, "Customer123!", "admin")

    # Agent can login through agent portal
    agent_auth = authenticate_user("AGENT-007", "Agent123!", "agent")
    assert agent_auth["user"]["role"] == "agent"

    # Agent cannot login through customer portal
    with pytest.raises(NotAuthenticated):
        authenticate_user("AGENT-007", "Agent123!", "customer")

    # Admin can login through admin portal
    admin_auth = authenticate_user("admin@momo.platform", "Admin123!", "admin")
    assert admin_auth["user"]["role"] == "admin"

    # Admin cannot login through customer portal
    with pytest.raises(NotAuthenticated):
        authenticate_user("admin@momo.platform", "Admin123!", "customer")

    merchant_auth = authenticate_user("MERCHANT-001", "Merchant123!", "merchant")
    assert merchant_auth["user"]["role"] == "merchant"
    assert merchant_auth["user"]["groups"] == ["merchants"]

    with pytest.raises(NotAuthenticated):
        authenticate_user("MERCHANT-001", "Merchant123!", "customer")


def test_demo_accounts_require_explicit_local_opt_in(monkeypatch):
    import momo.api.user_store as user_store

    monkeypatch.setattr(user_store, "_memory_users", {})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.setenv("AUTH_DEMO_USERS", "false")
    with pytest.raises(NotAuthenticated):
        authenticate_user("admin@momo.platform", "Admin123!", "admin")


def test_token_issued_and_verified_by_jwt_verifier():
    v = JwtVerifier(ISSUER, CLIENT_ID, StubJwks())
    token = issue_token("cust-123", "+260971234567", "customer")
    user = v.verify(token)
    assert user.sub == "cust-123"
    assert user.username == "+260971234567"
    assert "customers" in user.groups
    assert not user.is_admin

    admin_token = issue_token("admin-1", "admin@momo.platform", "admin")
    admin_user = v.verify(admin_token)
    assert admin_user.is_admin
    assert "admins" in admin_user.groups


def test_logout_revokes_local_access_token(client):
    token = authenticate_user("+260971000001", "Customer123!", "customer")["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/auth/me", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_api_auth_endpoints_flow(client):
    assert client.get("/auth-session.js").status_code == 200
    for path in (
        "/", "/login", "/login.html", "/login/customer", "/login/agent", "/login/admin", "/login/merchant",
        "/register", "/register.html",
        "/customer", "/customer/send-money", "/customer/merchant-payment", "/customer/cash-out",
        "/customer/transactions", "/customer/profile", "/agent", "/agent/cash-in",
        "/agent/cash-out", "/agent/transactions", "/agent/profile", "/admin",
        "/admin/users", "/admin/transactions", "/admin/analytics", "/admin/profile", "/demo", "/merchant",
        "/merchant/dashboard",
        "/portal.css", "/auth-pages.css", "/portal.js", "/login.js", "/register.js", "/console.css", "/console.js",
    ):
        assert client.get(path).status_code == 200
    assert b"Welcome to the Future of Mobile Payments." in client.get("/").data
    assert b'href="/login"' in client.get("/").data
    assert b'href="/register"' in client.get("/").data
    assert b'data-required-role="customer"' in client.get("/customer").data
    assert b'data-required-role="agent"' in client.get("/agent").data
    assert b'data-required-role="admin"' in client.get("/admin").data
    assert b"Merchant" in client.get("/login").data
    assert b"data-auth-role=\"merchant\"" in client.get("/login/merchant").data
    assert b"Backend-enforced access" in client.get("/merchant").data
    assert b'href="/customer/cash-out"' in client.get("/customer").data
    assert b'href="/demo"' in client.get("/admin").data
    assert b"Not available for agent-initiated requests" in client.get("/agent/cash-out").data

    # 1. Customer registration endpoint
    phone = "+260975554433"
    r = client.post(
        "/auth/register",
        json={"phone": phone, "password": "RegisterPass1!", "full_name": "New Cust", "currency": "ZMW"},
    )
    assert r.status_code == 201
    data = r.get_json()
    token = data["token"]
    assert token
    assert data["user"]["role"] == "customer"
    assert data["expires_in"] == 3600

    # 2. Authenticate customer
    r = client.post(
        "/auth/login/customer",
        json={"phone": phone, "password": "RegisterPass1!"},
    )
    assert r.status_code == 200
    cust_token = r.get_json()["token"]

    # 3. GET /auth/me with Bearer token
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {cust_token}"})
    assert r.status_code == 200
    assert r.get_json()["user"]["username"] == phone
    assert r.get_json()["user"]["role"] == "customer"

    # 4. Agent login endpoint
    r = client.post(
        "/auth/login/agent",
        json={"agent_id": "AGENT-007", "password": "Agent123!"},
    )
    assert r.status_code == 200
    agent_token = r.get_json()["token"]
    assert agent_token

    # 5. Admin login endpoint
    r = client.post(
        "/auth/login/admin",
        json={"username": "admin@momo.platform", "password": "Admin123!"},
    )
    assert r.status_code == 200
    admin_token = r.get_json()["token"]
    assert admin_token

    # 6. Merchant login endpoint
    r = client.post(
        "/auth/login/merchant",
        json={"merchant_id": "MERCHANT-001", "password": "Merchant123!"},
    )
    assert r.status_code == 200
    assert r.get_json()["token"]
    assert r.get_json()["user"]["groups"] == ["merchants"]

    # 7. Role-Based Access Control check: Customer cannot call admin endpoints
    r = client.get("/admin/fraud-alerts", headers={"Authorization": f"Bearer {cust_token}"})
    assert r.status_code == 403

    # Admin CAN call admin endpoints
    # (Notice: admin/fraud-alerts requires DB, but authorization check passes)


def test_customer_registration_cannot_assign_privileged_role(client):
    response = client.post(
        "/auth/register",
        json={
            "phone": "+260975550001",
            "password": "RegisterPass1!",
            "full_name": "Customer Role",
            "role": "admin",
        },
    )
    assert response.status_code == 201
    assert response.get_json()["user"]["role"] == "customer"
    assert response.get_json()["user"]["groups"] == ["customers"]


def test_role_specific_api_actions_are_rejected_before_resource_access(client, monkeypatch):
    import momo.service as service

    monkeypatch.setattr(service, "create_wallet", lambda *_args: {"id": "agent-wallet"})
    customer_token = issue_token("customer-1", "+260970000001", "customer")
    agent_token = issue_token("agent-1", "AGENT-007", "agent")
    merchant_token = issue_token("merchant-1", "MERCHANT-001", "merchant")
    headers = {"Authorization": f"Bearer {agent_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    merchant_headers = {"Authorization": f"Bearer {merchant_token}"}
    monkeypatch.setattr(service, "create_wallet", lambda *_args, **_kwargs: {"id": "merchant-wallet"})

    assert client.post("/wallets", json={"kind": "CUSTOMER"}, headers=headers).status_code == 403
    assert client.post("/wallets", json={"kind": "AGENT"}, headers=customer_headers).status_code == 403
    assert client.post("/wallets", json={"kind": "AGENT"}, headers=headers).status_code == 201
    assert client.post("/wallets", json={"kind": "MERCHANT"}, headers=merchant_headers).status_code == 201
    assert client.post("/wallets", json={"kind": "CUSTOMER"}, headers=merchant_headers).status_code == 403
    assert client.post("/merchant-payments", json={}, headers=merchant_headers).status_code == 403
    assert client.post("/agents/cash-in", json={}, headers=merchant_headers).status_code == 403
    assert client.get("/admin/integrity", headers=merchant_headers).status_code == 403
    assert client.post("/merchant-payments", json={}, headers=headers).status_code == 403
    assert client.post("/agents/cash-out", json={}, headers=headers).status_code == 403
    assert client.post("/transfers", json={}, headers=headers).status_code == 403


def test_authentication_database_errors_do_not_fall_back_to_demo_users(monkeypatch):
    import momo.common.db as db

    monkeypatch.setenv("DATABASE_URL", "postgresql://unavailable")

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "transaction", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        authenticate_user("AGENT-007", "Agent123!", "agent")


def test_database_backed_api_requires_a_strong_jwt_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        create_app()
