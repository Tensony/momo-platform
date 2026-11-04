import uuid

import pytest

from momo.api.app import create_app
from momo.api.auth import JwtVerifier
from tests.integration.helpers import assert_money_conserved, balance
from tests.jwt_helpers import CLIENT_ID, ISSUER, StubJwks, make_token

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db):
    app = create_app(JwtVerifier(ISSUER, CLIENT_ID, StubJwks()))
    app.testing = False
    return app.test_client()


def H(sub, groups=(), key=None, **extra):
    h = {"Authorization": f"Bearer {make_token(sub, groups)}"}
    if key:
        h["Idempotency-Key"] = key
    h.update(extra)
    return h


def mk_wallet(client, sub, kind="CUSTOMER", groups=()):
    r = client.post("/wallets", json={"kind": kind}, headers=H(sub, groups))
    assert r.status_code == 201, r.json
    return r.json["id"]


def fund(client, agent_sub, agent_wallet, to_wallet, amount):
    """Real funding path: admin tops up agent float, agent cashes-in to customer."""
    admin = H("admin-1", ["admins"], key=f"topup-{uuid.uuid4()}")
    assert client.post("/admin/float-topups", json={"to_wallet": agent_wallet, "amount": amount}, headers=admin).status_code == 201
    r = client.post("/agents/cash-in", json={"from_wallet": agent_wallet, "to_wallet": to_wallet, "amount": amount},
                    headers=H(agent_sub, ["agents"], key=f"cashin-{uuid.uuid4()}"))
    assert r.status_code == 201, r.json


def test_health_needs_no_auth(client):
    assert client.get("/health").json == {"status": "ok"}


def test_unauthenticated_and_forged_requests_are_401(client):
    assert client.post("/wallets", json={}).status_code == 401
    assert client.get("/wallets/x", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_end_to_end_money_flow_and_correlation_id(client):
    agent_w = mk_wallet(client, "agent-1", "AGENT", ["agents"])
    alice = mk_wallet(client, "alice")
    bob = mk_wallet(client, "bob")
    shop = mk_wallet(client, "shop", "MERCHANT", ["merchants"])
    fund(client, "agent-1", agent_w, alice, 100_000)

    r = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 25_000},
                    headers=H("alice", key="t-0000001", **{"X-Correlation-ID": "corr-abcdef12"}))
    assert r.status_code == 201 and r.headers["X-Correlation-ID"] == "corr-abcdef12"
    r = client.post("/merchant-payments", json={"from_wallet": alice, "to_wallet": shop, "amount": 10_000},
                    headers=H("alice", key="m-0000001"))
    assert r.status_code == 201
    r = client.post("/agents/cash-out", json={"from_wallet": alice, "to_wallet": agent_w, "amount": 5_000},
                    headers=H("alice", key="c-0000001"))
    assert r.status_code == 201
    assert (balance(alice), balance(bob), balance(shop)) == (60_000, 25_000, 10_000)

    led = client.get(f"/wallets/{alice}/ledger", headers=H("alice")).json["entries"]
    assert led and all(e["hash"] for e in led)
    assert client.get(f"/wallets/{alice}", headers=H("alice")).json["balance"] == 60_000
    assert_money_conserved()


def test_authorization_boundaries(client):
    alice, bob = mk_wallet(client, "alice"), mk_wallet(client, "bob")
    # can't spend from someone else's wallet
    r = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 1}, headers=H("bob", key="x-0000001"))
    assert r.status_code == 403
    # can't read someone else's wallet or ledger
    assert client.get(f"/wallets/{alice}", headers=H("bob")).status_code == 403
    assert client.get(f"/wallets/{alice}/ledger", headers=H("bob")).status_code == 403
    assert client.get(f"/wallets/{alice}", headers=H("root", ["admins"])).status_code == 200
    # privileged operations need groups
    assert client.post("/wallets", json={"kind": "AGENT"}, headers=H("bob")).status_code == 403
    assert client.post("/wallets", json={"kind": "MERCHANT"}, headers=H("bob")).status_code == 403
    assert client.post("/admin/float-topups", json={"to_wallet": alice, "amount": 5}, headers=H("bob", key="y-0000001")).status_code == 403
    assert client.post("/agents/cash-in", json={"from_wallet": alice, "to_wallet": bob, "amount": 5}, headers=H("bob", key="z-0000001")).status_code == 403


def test_read_only_console_endpoints_enforce_ownership_and_admin_access(client):
    agent_wallet = mk_wallet(client, "agent-console", "AGENT", ["agents"])
    alice = mk_wallet(client, "alice-console")
    bob = mk_wallet(client, "bob-console")
    fund(client, "agent-console", agent_wallet, alice, 10_000)
    payment = client.post(
        "/transfers",
        json={"from_wallet": alice, "to_wallet": bob, "amount": 2_500},
        headers=H("alice-console", key="console-transfer-1"),
    )
    assert payment.status_code == 201
    txn_id = payment.json["transaction_id"]

    alice_wallets = client.get("/wallets?limit=10", headers=H("alice-console"))
    assert alice_wallets.status_code == 200
    assert {w["owner_id"] for w in alice_wallets.json["wallets"]} == {"alice-console"}
    assert alice_wallets.json["total"] == 1
    assert client.get("/wallets?search=alice-console", headers=H("alice-console")).json["total"] == 1
    assert client.get("/wallets", headers=H("bob-console")).json["total"] == 1
    assert client.get("/wallets?limit=201", headers=H("alice-console")).status_code == 400
    assert client.get(f"/wallets?search={'x' * 101}", headers=H("alice-console")).status_code == 400
    assert client.get("/wallets", headers=H("alice-console")).json["wallets"][0]["balance"] == 7_500

    alice_txns = client.get("/transactions", headers=H("alice-console"))
    assert alice_txns.status_code == 200
    assert [t["transaction_id"] for t in alice_txns.json["transactions"]] == [txn_id]
    assert client.get(
        f"/transactions?search={txn_id[:8]}", headers=H("alice-console")
    ).json["total"] == 1
    assert client.get("/transactions", headers=H("bob-console")).json["total"] == 1
    assert client.get("/transactions", headers=H("mallory")).json["total"] == 0

    journal = client.get(f"/transactions/{txn_id}/ledger", headers=H("alice-console"))
    assert journal.status_code == 200
    assert len(journal.json["entries"]) == 4
    assert client.get(f"/transactions/{txn_id}/ledger", headers=H("mallory")).status_code == 403

    operation = client.get(
        "/operations?type=TRANSFER&key=console-transfer-1", headers=H("alice-console")
    )
    assert operation.status_code == 200
    assert operation.json["operation_status"] == "COMPLETED"
    assert operation.json["transaction"]["transaction_id"] == txn_id
    assert client.get(
        "/operations?type=TRANSFER&key=console-transfer-1", headers=H("bob-console")
    ).status_code == 404

    dispute = client.post(
        f"/transactions/{txn_id}/disputes",
        json={"reason": "Console test dispute"},
        headers=H("alice-console"),
    )
    assert dispute.status_code == 201
    assert client.get("/disputes", headers=H("alice-console")).json["total"] == 1
    assert client.get("/disputes", headers=H("bob-console")).json["total"] == 0

    assert client.get("/admin/fraud-alerts", headers=H("alice-console")).status_code == 403
    assert client.get("/admin/settlement-reports", headers=H("alice-console")).status_code == 403
    admin = H("admin-console", ["admins"])
    assert client.get("/admin/fraud-alerts", headers=admin).json["total"] >= 0
    assert client.get("/admin/settlement-reports", headers=admin).status_code == 200


def test_validation_errors(client):
    alice, bob = mk_wallet(client, "alice"), mk_wallet(client, "bob")
    h = H("alice", key="v-0000001")
    for body in ({"from_wallet": alice, "to_wallet": bob, "amount": -5},
                 {"from_wallet": alice, "to_wallet": bob, "amount": 1.5},
                 {"from_wallet": alice, "to_wallet": bob, "amount": "10"},
                 {"from_wallet": alice, "to_wallet": bob}):
        assert client.post("/transfers", json=body, headers=h).status_code == 400, body
    assert client.post("/transfers", data="not json", headers=h).status_code == 400
    assert client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 1}, headers=H("alice")).status_code == 400  # no idempotency key
    assert client.get("/wallets/not-a-uuid", headers=H("alice")).status_code == 404
    assert client.get("/nope").status_code == 404
    assert client.delete("/transfers").status_code == 405


def test_insufficient_funds_returns_422_and_replay_header(client):
    alice, bob = mk_wallet(client, "alice"), mk_wallet(client, "bob")
    body = {"from_wallet": alice, "to_wallet": bob, "amount": 100}
    r1 = client.post("/transfers", json=body, headers=H("alice", key="i-0000001"))
    r2 = client.post("/transfers", json=body, headers=H("alice", key="i-0000001"))
    assert r1.status_code == r2.status_code == 422 and r1.json["code"] == "INSUFFICIENT_FUNDS"
    assert r2.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in r1.headers


def test_reversal_and_dispute_flow(client):
    agent_w, alice, bob = mk_wallet(client, "agent-1", "AGENT", ["agents"]), mk_wallet(client, "alice"), mk_wallet(client, "bob")
    fund(client, "agent-1", agent_w, alice, 50_000)
    txn = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 20_000}, headers=H("alice", key="r-0000001")).json["transaction_id"]

    d = client.post(f"/transactions/{txn}/disputes", json={"reason": "sent to the wrong person"}, headers=H("alice"))
    assert d.status_code == 201
    dispute_id = d.json["dispute_id"]
    assert client.post(f"/transactions/{txn}/disputes", json={"reason": "again please"}, headers=H("alice")).json["dispute_id"] == dispute_id  # one open dispute per txn
    assert client.post(f"/transactions/{txn}/disputes", json={"reason": "i am a stranger"}, headers=H("mallory")).status_code == 403
    assert client.post(f"/transactions/{txn}/reverse", json={}, headers=H("alice", key="rv-000001")).status_code == 403
    assert client.get(f"/disputes/{dispute_id}", headers=H("mallory")).status_code == 403

    res = client.post(f"/disputes/{dispute_id}/resolve", json={"action": "REVERSE", "note": "confirmed"}, headers=H("admin-1", ["admins"]))
    assert res.status_code == 200 and res.json["status"] == "RESOLVED_REVERSED"
    assert (balance(alice), balance(bob)) == (50_000, 0)
    assert client.get(f"/transactions/{txn}", headers=H("alice")).json["status"] == "REVERSED"
    assert client.post(f"/disputes/{dispute_id}/resolve", json={"action": "REVERSE"}, headers=H("admin-1", ["admins"])).status_code == 400
    assert client.post(f"/transactions/{txn}/reverse", json={}, headers=H("admin-1", ["admins"], key="rv-000002")).status_code == 409
    assert_money_conserved()


def test_reversal_rules(client):
    agent_w, alice, bob = mk_wallet(client, "agent-1", "AGENT", ["agents"]), mk_wallet(client, "alice"), mk_wallet(client, "bob")
    fund(client, "agent-1", agent_w, alice, 10_000)
    admin = lambda k: H("admin-1", ["admins"], key=k)  # noqa: E731
    txn = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 4_000}, headers=H("alice", key="q-0000001")).json["transaction_id"]
    # bob spends the money, so the reversal cannot be honoured: rejected cleanly, nothing breaks
    carol = mk_wallet(client, "carol")
    client.post("/transfers", json={"from_wallet": bob, "to_wallet": carol, "amount": 4_000}, headers=H("bob", key="q-0000002"))
    r = client.post(f"/transactions/{txn}/reverse", json={"reason": "fraud"}, headers=admin("rv-000003"))
    assert r.status_code == 422 and r.json["code"] == "INSUFFICIENT_FUNDS"
    assert client.get(f"/transactions/{txn}", headers=H("alice")).json["status"] == "COMPLETED"
    # reversals of reversals and of unknown transactions are refused
    assert client.post(f"/transactions/{uuid.uuid4()}/reverse", json={}, headers=admin("rv-000004")).status_code == 404
    assert client.post("/transactions/garbage/reverse", json={}, headers=admin("rv-000005")).status_code == 404
    assert_money_conserved()


def test_rejected_dispute_resolution(client):
    agent_w, alice, bob = mk_wallet(client, "agent-1", "AGENT", ["agents"]), mk_wallet(client, "alice"), mk_wallet(client, "bob")
    fund(client, "agent-1", agent_w, alice, 10_000)
    txn = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 1_000}, headers=H("alice", key="d-0000001")).json["transaction_id"]
    did = client.post(f"/transactions/{txn}/disputes", json={"reason": "not sure about this"}, headers=H("bob")).json["dispute_id"]
    r = client.post(f"/disputes/{did}/resolve", json={"action": "REJECT", "note": "legit"}, headers=H("admin-1", ["admins"]))
    assert r.json["status"] == "RESOLVED_REJECTED" and balance(bob) == 1_000
    assert client.post(f"/disputes/{did}/resolve", json={"action": "BOGUS"}, headers=H("admin-1", ["admins"])).status_code == 400
    assert client.post(f"/transactions/{txn}/disputes", json={"reason": "x"}, headers=H("bob")).status_code == 400


def test_fault_injection_header_only_works_when_enabled(client, monkeypatch):
    agent_w, alice, bob = mk_wallet(client, "agent-1", "AGENT", ["agents"]), mk_wallet(client, "alice"), mk_wallet(client, "bob")
    fund(client, "agent-1", agent_w, alice, 10_000)
    r = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 1_000},
                    headers=H("alice", key="f-0000001", **{"X-Fault-Inject": "credit.after"}))
    assert r.status_code == 500 and r.json["code"] == "SAGA_FAILED_COMPENSATED"
    assert (balance(alice), balance(bob)) == (10_000, 0)
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "false")
    r = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 1_000},
                    headers=H("alice", key="f-0000002", **{"X-Fault-Inject": "credit.after"}))
    assert r.status_code == 201
    assert_money_conserved()


def test_transaction_visibility(client):
    agent_w, alice, bob = mk_wallet(client, "agent-1", "AGENT", ["agents"]), mk_wallet(client, "alice"), mk_wallet(client, "bob")
    fund(client, "agent-1", agent_w, alice, 5_000)
    txn = client.post("/transfers", json={"from_wallet": alice, "to_wallet": bob, "amount": 500}, headers=H("alice", key="s-0000001")).json["transaction_id"]
    assert client.get(f"/transactions/{txn}", headers=H("bob")).status_code == 200
    assert client.get(f"/transactions/{txn}", headers=H("eve")).status_code == 403
    assert client.get(f"/transactions/{uuid.uuid4()}", headers=H("eve")).status_code == 404


def test_health_reports_unhealthy_for_rollback_demo(client, monkeypatch):
    monkeypatch.setenv("FORCE_UNHEALTHY", "true")
    assert client.get("/health").status_code == 503


def test_admin_integrity_endpoint(client):
    assert client.get("/admin/integrity", headers=H("bob")).status_code == 403
    r = client.get("/admin/integrity", headers=H("root", ["admins"]))
    assert r.status_code == 200 and r.json["status"] == "OK"
