"""Unit tests for the Demo Platform Management API routes."""

import pytest

from momo.api.app import create_app
from momo.api.auth import JwtVerifier


@pytest.fixture
def client():
    # Use dummy verifier
    app = create_app(verifier=JwtVerifier("https://dummy", "client", None))
    app.config["TESTING"] = True
    app.config["ENABLE_DEMO_API"] = True
    with app.test_client() as c:
        yield c


def test_demo_api_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MOMO_DEMO_API_ENABLED", raising=False)
    app = create_app(verifier=JwtVerifier("https://dummy", "client", None))
    app.config["TESTING"] = True
    assert app.config["ENABLE_DEMO_API"] is False
    with app.test_client() as client:
        response = client.get("/api/state")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "NOT_FOUND"


def test_demo_api_state(client):
    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert "dashboard" in data
    assert "wallets" in data
    assert len(data["wallets"]) >= 4
    assert data["reconciliation"]["status"] == "OK"


def test_demo_api_transfer_flow(client):
    client.post("/api/reset")
    # Fetch state to get wallet ids
    st = client.get("/api/state").get_json()
    alice = next(w for w in st["wallets"] if "Alice" in w["label"])
    bob = next(w for w in st["wallets"] if "Bob" in w["label"])

    # 1. Normal transfer
    res = client.post(
        "/api/transactions/submit",
        json={
            "role": "customer",
            "type": "TRANSFER",
            "from_wallet": alice["id"],
            "to_wallet": bob["id"],
            "amount_ngwee": 5_000,
            "idempotency_key": "test-key-01",
        },
    )
    assert res.status_code == 201
    payload = res.get_json()
    assert payload["transaction"]["status"] == "COMPLETED"
    assert payload["replayed"] is False

    # 2. Replay with same key
    replay_res = client.post(
        "/api/transactions/submit",
        json={
            "role": "customer",
            "type": "TRANSFER",
            "from_wallet": alice["id"],
            "to_wallet": bob["id"],
            "amount_ngwee": 5_000,
            "idempotency_key": "test-key-01",
        },
    )
    assert replay_res.status_code == 201
    replay_payload = replay_res.get_json()
    assert replay_payload["replayed"] is True

    # 3. Conflict with different payload
    conflict_res = client.post(
        "/api/transactions/submit",
        json={
            "role": "customer",
            "type": "TRANSFER",
            "from_wallet": alice["id"],
            "to_wallet": bob["id"],
            "amount_ngwee": 10_000,
            "idempotency_key": "test-key-01",
        },
    )
    assert conflict_res.status_code == 422


def test_demo_api_dispute_and_reversal(client):
    client.post("/api/reset")
    st = client.get("/api/state").get_json()
    alice = next(w for w in st["wallets"] if "Alice" in w["label"])
    bob = next(w for w in st["wallets"] if "Bob" in w["label"])

    # Submit transaction
    tx_res = client.post(
        "/api/transactions/submit",
        json={
            "role": "customer",
            "type": "TRANSFER",
            "from_wallet": alice["id"],
            "to_wallet": bob["id"],
            "amount_ngwee": 2_500,
            "idempotency_key": "tx-to-dispute-01",
        },
    ).get_json()
    txn_id = tx_res["transaction"]["transaction_id"]

    # Raise dispute
    disp_res = client.post(
        "/api/disputes/raise",
        json={"role": "customer", "txn_id": txn_id, "reason": "Incorrect payment amount"},
    )
    assert disp_res.status_code == 201
    disp_id = disp_res.get_json()["dispute"]["dispute_id"]

    # Admin resolves with REVERSE
    resolve_res = client.post(
        f"/api/disputes/{disp_id}/resolve",
        json={"role": "admin", "action": "REVERSE", "note": "Approved by auditor"},
    )
    assert resolve_res.status_code == 200
    assert resolve_res.get_json()["dispute"]["status"] == "RESOLVED_REVERSED"


def test_demo_api_tamper_detection(client):
    client.post("/api/reset")
    # Enable tamper
    tamper_res = client.post("/api/tamper", json={"tamper": True}).get_json()
    assert tamper_res["reconciliation"]["status"] == "DISCREPANCY"
    assert "broken_hash_chains" in tamper_res["reconciliation"]["issues"]

    # Restore
    restore_res = client.post("/api/tamper", json={"tamper": False}).get_json()
    assert restore_res["reconciliation"]["status"] == "OK"
