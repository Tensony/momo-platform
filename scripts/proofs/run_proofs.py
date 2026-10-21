#!/usr/bin/env python3
"""Correctness-under-failure proofs for the mobile money platform.

Runs the three assignment proofs over HTTP, exactly as a client would, and writes machine-readable
evidence plus a Markdown report:

  1. IDEMPOTENCY   replaying one request 100 times  => exactly ONE debit
  2. SAGA          a crash at every step            => no money created or destroyed
  3. CONCURRENCY   50 concurrent transfers, funds for 33 => never overdrawn

Usage
-----
  Local  (starts the API in-process against TEST_DATABASE_URL; no AWS needed):
      TEST_DATABASE_URL=postgresql://... python scripts/proofs/run_proofs.py --local

  AWS    (against the deployed ALB; tokens for 3 pre-created Cognito users from scripts/get_tokens.sh):
      python scripts/proofs/run_proofs.py --base-url https://<alb-dns> \
          --admin-token $ADMIN --agent-token $AGENT --user-token $USER

For the AWS run, fault injection must be enabled (terraform var enable_fault_injection=true) and the
fraud velocity limit relaxed so that the concurrency proof measures funds, not fraud rules:
  aws ssm put-parameter --name /momo/fraud/config --type String --overwrite \
        --value '{"velocity_max_txns": 100000}'
Both are restored by `make evidence-reset`. Exit code is non-zero if ANY proof fails.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "evidence-out"
FAULT_POINTS = ["reserve.after", "fraud.before", "credit.before", "credit.after", "finalize.before"]


class Api:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token
        self.local = threading.local()

    def _s(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def call(self, method, path, json_body=None, key=None, headers=None, retries=3):
        h = {"Authorization": f"Bearer {self.token}", "X-Correlation-ID": f"proof-{uuid.uuid4()}"}
        if key:
            h["Idempotency-Key"] = key
        h.update(headers or {})
        for attempt in range(retries):
            try:
                r = self._s().request(method, self.base + path, json=json_body, headers=h, timeout=60)
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(0.2 * 2**attempt)   # network error: SAFE to retry because of Idempotency-Key
                continue
            if r.status_code == 409 and r.json().get("error", {}).get("code") == "REQUEST_IN_PROGRESS":
                time.sleep(0.5)
                continue
            return r
        return r


def check(results, name, cond, detail=""):
    results.append({"check": name, "passed": bool(cond), "detail": detail})
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    return cond


def setup(admin, agent, user, user2):
    """Create wallets and fund them through the real flow: admin float top-up, agent cash-in."""
    agent_w = agent.call("POST", "/wallets", {"kind": "AGENT"}).json()["id"]
    payer = user.call("POST", "/wallets", {"kind": "CUSTOMER"}).json()["id"]
    payee = user2.call("POST", "/wallets", {"kind": "CUSTOMER"}).json()["id"]
    return agent_w, payer, payee


def fund(admin, agent, agent_w, wallet, amount):
    r = admin.call("POST", "/admin/float-topups", {"to_wallet": agent_w, "amount": amount}, key=f"topup-{uuid.uuid4()}")
    assert r.status_code == 201, r.text
    r = agent.call("POST", "/agents/cash-in", {"from_wallet": agent_w, "to_wallet": wallet, "amount": amount}, key=f"cashin-{uuid.uuid4()}")
    assert r.status_code == 201, r.text


def balance(api, wallet):
    return api.call("GET", f"/wallets/{wallet}").json()["balance"]


def integrity(admin):
    r = admin.call("GET", "/admin/integrity")
    return r.json()


def proof_idempotency(admin, agent, user, user2, agent_w, payer, payee):
    print("\n[1] IDEMPOTENCY - replay one transfer 100 times")
    res = []
    fund(admin, agent, agent_w, payer, 100_000)
    before = balance(user, payer)
    key, amount = f"idem-proof-{uuid.uuid4()}", 15_000
    body = {"from_wallet": payer, "to_wallet": payee, "amount": amount}
    with ThreadPoolExecutor(25) as pool:
        responses = list(pool.map(lambda _: user.call("POST", "/transfers", body, key=key), range(100)))
    codes = sorted({r.status_code for r in responses})
    txn_ids = {r.json().get("transaction_id") for r in responses}
    replays = sum(1 for r in responses if r.headers.get("Idempotent-Replayed") == "true")
    after = balance(user, payer)
    ledger = user.call("GET", f"/wallets/{payer}/ledger?limit=500").json()["entries"]
    debits = [e for e in ledger if e["direction"] == "DEBIT" and e["step"] == "reserve" and e["txn_id"] in txn_ids]
    check(res, "all 100 responses are 201 Created", codes == [201], f"status codes seen: {codes}")
    check(res, "all 100 responses refer to the SAME transaction id", len(txn_ids) == 1, f"{len(txn_ids)} distinct id(s)")
    check(res, "exactly 1 response was a real execution, 99 were replays", replays == 99, f"replays={replays}")
    check(res, "payer was debited exactly once", before - after == amount, f"before={before} after={after} amount={amount}")
    check(res, "ledger holds exactly ONE debit entry for the transaction", len(debits) == 1, f"debit entries={len(debits)}")
    changed = user.call("POST", "/transfers", {**body, "amount": amount + 1}, key=key)
    check(res, "same key + different body is refused (422)", changed.status_code == 422, changed.json().get("error", {}).get("code", ""))
    return res


def proof_saga(admin, agent, user, user2, agent_w, payer, payee):
    print("\n[2] SAGA COMPENSATION - inject a crash at every step")
    res = []
    fund(admin, agent, agent_w, payer, 100_000)
    for point in FAULT_POINTS:
        b_payer, b_payee = balance(user, payer), balance(user2, payee)
        r = user.call("POST", "/transfers", {"from_wallet": payer, "to_wallet": payee, "amount": 4_000},
                      key=f"saga-{point}-{uuid.uuid4()}", headers={"X-Fault-Inject": point})
        a_payer, a_payee = balance(user, payer), balance(user2, payee)
        outcome = r.json().get("code") or r.json().get("status")
        check(res, f"crash at {point:<16} -> compensated, balances unchanged",
              r.status_code == 500 and outcome == "SAGA_FAILED_COMPENSATED" and (a_payer, a_payee) == (b_payer, b_payee),
              f"http={r.status_code} outcome={outcome} payer {b_payer}->{a_payer} payee {b_payee}->{a_payee}")
    inv = integrity(admin)
    check(res, "global integrity after all faults: sum of balances = 0, ledger balanced, hash chains valid, escrow empty",
          inv.get("status") == "OK", json.dumps(inv.get("issues")))
    r = user.call("POST", "/transfers", {"from_wallet": payer, "to_wallet": payee, "amount": 4_000}, key=f"saga-ok-{uuid.uuid4()}")
    check(res, "control: the same transfer without a fault completes", r.status_code == 201, f"http={r.status_code}")
    return res


def proof_concurrency(admin, agent, user, user2, agent_w, payer, payee):
    print("\n[3] CONCURRENCY SAFETY - 50 simultaneous transfers, funds for only 33")
    res = []
    current = balance(user, payer)
    funding = 1_000
    # drain the payer to exactly 1000 by cashing out to the agent, then run the race
    if current > funding:
        r = user.call("POST", "/agents/cash-out", {"from_wallet": payer, "to_wallet": agent_w, "amount": current - funding}, key=f"drain-{uuid.uuid4()}")
        assert r.status_code == 201, r.text
    assert balance(user, payer) == funding
    start_payee, amount, n = balance(user2, payee), 30, 50
    barrier = threading.Barrier(n)

    def fire(i):
        barrier.wait()   # release all 50 at the same instant
        return user.call("POST", "/transfers", {"from_wallet": payer, "to_wallet": payee, "amount": amount}, key=f"race-{uuid.uuid4()}")

    with ThreadPoolExecutor(n) as pool:
        responses = list(pool.map(fire, range(n)))
    ok = sum(1 for r in responses if r.status_code == 201)
    insufficient = sum(1 for r in responses if r.status_code == 422 and r.json().get("code") == "INSUFFICIENT_FUNDS")
    other = [r.status_code for r in responses if r.status_code not in (201, 422)]
    final_payer, final_payee = balance(user, payer), balance(user2, payee)
    check(res, "exactly 33 transfers succeeded (floor(1000/30))", ok == 33, f"succeeded={ok}")
    check(res, "the other 17 were rejected with INSUFFICIENT_FUNDS", insufficient == 17, f"rejected={insufficient} other={other}")
    check(res, "payer balance is never negative", final_payer >= 0, f"final={final_payer}")
    check(res, "payer balance == 1000 - 33*30 == 10", final_payer == funding - ok * amount == 10, f"final={final_payer}")
    check(res, "payee received exactly what the payer lost", final_payee - start_payee == funding - final_payer, f"payee +{final_payee - start_payee}")
    ledger = user.call("GET", f"/wallets/{payer}/ledger?limit=500").json()["entries"]
    check(res, "every ledger balance_after is >= 0 (no overdraft at any point in history)", all(e["balance_after"] >= 0 for e in ledger))
    inv = integrity(admin)
    check(res, "global integrity check passes", inv.get("status") == "OK", json.dumps(inv.get("issues")))
    return res


def start_local():
    """Run the real API app on a local port with a throw-away signing key (no AWS needed)."""
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    url = os.environ["TEST_DATABASE_URL"]
    os.environ.update(DATABASE_URL=url, ENABLE_FAULT_INJECTION="true", DB_POOL_MAX="40",
                      FRAUD_CONFIG_JSON='{"velocity_max_txns": 100000}')
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.execute((ROOT / "db" / "schema.sql").read_text())
    from tests.jwt_helpers import CLIENT_ID, ISSUER, StubJwks, make_token
    from werkzeug.serving import make_server

    from momo.api.app import create_app
    from momo.api.auth import JwtVerifier
    app = create_app(JwtVerifier(ISSUER, CLIENT_ID, StubJwks()))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    return base, make_token("admin-1", ["admins"]), make_token("agent-1", ["agents"]), make_token("alice"), make_token("bob")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--admin-token")
    ap.add_argument("--agent-token")
    ap.add_argument("--user-token")
    ap.add_argument("--user2-token")
    a = ap.parse_args()
    if a.local:
        base, t_admin, t_agent, t_user, t_user2 = start_local()
        target = "local (in-process API + PostgreSQL)"
    else:
        if not (a.base_url and a.admin_token and a.agent_token and a.user_token):
            ap.error("--base-url, --admin-token, --agent-token and --user-token are required (or use --local)")
        base, t_admin, t_agent, t_user = a.base_url, a.admin_token, a.agent_token, a.user_token
        t_user2 = a.user2_token or a.user_token
        target = base
    admin, agent, user, user2 = Api(base, t_admin), Api(base, t_agent), Api(base, t_user), Api(base, t_user2)
    agent_w, payer, payee = setup(admin, agent, user, user2)
    started = datetime.now(timezone.utc)
    results = {
        "idempotency": proof_idempotency(admin, agent, user, user2, agent_w, payer, payee),
        "saga_compensation": proof_saga(admin, agent, user, user2, agent_w, payer, payee),
        "concurrency": proof_concurrency(admin, agent, user, user2, agent_w, payer, payee),
    }
    all_ok = all(c["passed"] for checks in results.values() for c in checks)
    OUT.mkdir(exist_ok=True)
    evidence = {"target": target, "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                "passed": all_ok, "results": results}
    (OUT / "proof-results.json").write_text(json.dumps(evidence, indent=2))
    lines = ["# Correctness proofs\n", f"- Target: `{target}`", f"- Run: {started.isoformat()}", f"- Overall: **{'PASS' if all_ok else 'FAIL'}**\n"]
    titles = {"idempotency": "1. Idempotency (100 replays -> 1 debit)", "saga_compensation": "2. Saga compensation (no money created/destroyed)", "concurrency": "3. Concurrency safety (50 parallel, no overdraft)"}
    for k, checks in results.items():
        lines += [f"## {titles[k]}\n", "| Result | Check | Detail |", "|---|---|---|"]
        lines += [f"| {'PASS' if c['passed'] else 'FAIL'} | {c['check']} | {c['detail']} |" for c in checks]
        lines.append("")
    (OUT / "proof-results.md").write_text("\n".join(lines))
    print(f"\nOVERALL: {'PASS' if all_ok else 'FAIL'}   (evidence in {OUT})")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
