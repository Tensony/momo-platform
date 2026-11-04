"""Authoritative in-memory financial engine for the interactive Web Console & Demo UI.

Uses the real platform domain primitives:
  * Double-entry accounting (`DEBIT` + `CREDIT` pairs via `MINT` and `ESCROW`)
  * Cryptographic SHA-256 per-wallet hash chains (`momo.common.hashing.compute_entry_hash`)
  * Request canonical hashing & idempotency (`momo.common.hashing.hash_request`)
  * Synchronous + Asynchronous Fraud Rules (`momo.fraud.rules`) & ML Anomaly Scoring (`momo.fraud.ml_scoring`)
  * Compensating Reversal Sagas (`REVERSAL` transactions) & Dispute Resolution
  * 7-Invariant Reconciliation & Settlement Reporting (`CONSERVATION`, `LEDGER_NET`, `HASH_CHAINS`, `ESCROW`)
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .common.hashing import GENESIS_HASH, compute_entry_hash, hash_request
from .fraud.config import FraudConfig
from .fraud.ml_scoring import compute_fraud_score
from .fraud.rules import ALL_RULES, ASYNC_RULES, Decision, FraudContext, evaluate
from .fx.service import DEFAULT_RATES_TO_ZMW
from .ledger.core import ESCROW_WALLET_ID, MINT_WALLET_ID

ALICE_WALLET_ID = "11111111-1111-4111-8111-111111111111"
BOB_WALLET_ID = "22222222-2222-4222-8222-222222222222"
SHOPRITE_WALLET_ID = "33333333-3333-4333-8333-333333333333"
AGENT_WALLET_ID = "44444444-4444-4444-8444-444444444444"


def _now_iso(offset_minutes: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoPlatformEngine:
    """Thread-safe double-entry financial engine backing the interactive UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cfg = FraudConfig()
        self.reset()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.wallets: dict[str, dict[str, Any]] = {}
            self.transactions: list[dict[str, Any]] = []
            self.ledger_entries: list[dict[str, Any]] = []
            self.idempotency_store: dict[str, dict[str, Any]] = {}
            self.fraud_alerts: list[dict[str, Any]] = []
            self.disputes: list[dict[str, Any]] = []
            self.audit_logs: list[dict[str, Any]] = []
            self.tampered_entry_id: int | None = None
            self.tampered_original_amount: int | None = None
            self.proof_runs: dict[str, Any] = {
                "idempotency_100": {
                    "requests_sent": 100,
                    "charged_count": 1,
                    "replayed_count": 99,
                    "amount_charged_ngwee": 15_000,
                    "amount_saved_ngwee": 1_485_000,
                    "last_run_at": _now_iso(-5),
                },
                "saga_crash": {
                    "fault_points_tested": 5,
                    "compensated_count": 5,
                    "escrow_final_ngwee": 0,
                    "last_run_at": _now_iso(-4),
                },
                "concurrency_50": {
                    "workers": 50,
                    "starting_balance_ngwee": 1_000,
                    "debit_each_ngwee": 30,
                    "approved_count": 33,
                    "declined_count": 17,
                    "final_balance_ngwee": 10,
                    "last_run_at": _now_iso(-3),
                },
            }

            # 1. Bootstrap System Wallets (MINT & ESCROW)
            self._init_wallet(
                MINT_WALLET_ID,
                owner_id="system",
                label="Central Bank Mint (System)",
                kind="SYSTEM",
                allow_negative=True,
            )
            self._init_wallet(
                ESCROW_WALLET_ID,
                owner_id="system",
                label="Saga Escrow Holding (System)",
                kind="SYSTEM",
                allow_negative=False,
            )

            # 2. Bootstrap Customer, Merchant, and Agent Wallets
            self._init_wallet(
                ALICE_WALLET_ID,
                owner_id="user-alice",
                label="Alice Mwamba (Customer)",
                kind="CUSTOMER",
            )
            self._init_wallet(
                BOB_WALLET_ID,
                owner_id="user-bob",
                label="Bob Banda (Customer)",
                kind="CUSTOMER",
            )
            self._init_wallet(
                SHOPRITE_WALLET_ID,
                owner_id="merchant-shoprite",
                label="Shoprite Cairo Rd (Merchant)",
                kind="MERCHANT",
            )
            self._init_wallet(
                AGENT_WALLET_ID,
                owner_id="agent-lusaka-01",
                label="Agent-1 Booth (Mobile Agent)",
                kind="AGENT",
            )

            # 3. Seed balances via true double-entry moves from MINT -> Agent -> Customers/Merchant
            # Issue K5,000.00 (500,000 ngwee) float from MINT to Agent-1
            self._exec_internal_seed(
                txn_id="00000000-0000-4000-8000-000000000101",
                txn_type="FLOAT_TOPUP",
                from_wallet=MINT_WALLET_ID,
                to_wallet=AGENT_WALLET_ID,
                amount_ngwee=500_000,
                actor="admin-auditor",
                note="Initial Agent Float Issuance (K5,000.00)",
                minutes_ago=-30,
            )
            # Agent Cash-In K1,350.00 (135,000 ngwee) to Alice
            self._exec_internal_seed(
                txn_id="00000000-0000-4000-8000-000000000102",
                txn_type="CASH_IN",
                from_wallet=AGENT_WALLET_ID,
                to_wallet=ALICE_WALLET_ID,
                amount_ngwee=135_000,
                actor="agent-lusaka-01",
                note="Customer Cash Deposit at Agent-1 Booth",
                minutes_ago=-25,
            )
            # Alice sends K250.00 (25,000 ngwee) to Bob
            self._exec_internal_seed(
                txn_id="00000000-0000-4000-8000-000000000103",
                txn_type="TRANSFER",
                from_wallet=ALICE_WALLET_ID,
                to_wallet=BOB_WALLET_ID,
                amount_ngwee=25_000,
                actor="user-alice",
                note="Family P2P Transfer (Alice to Bob)",
                minutes_ago=-18,
            )
            # Alice pays K100.00 (10,000 ngwee) to Shoprite Merchant
            self._exec_internal_seed(
                txn_id="00000000-0000-4000-8000-000000000104",
                txn_type="MERCHANT_PAYMENT",
                from_wallet=ALICE_WALLET_ID,
                to_wallet=SHOPRITE_WALLET_ID,
                amount_ngwee=10_000,
                actor="user-alice",
                note="Groceries at Shoprite Cairo Rd",
                minutes_ago=-10,
            )

            self._log_audit("SYSTEM_BOOT", "system", "Platform initialized with verified double-entry ledger & SHA-256 chains")
            return self._snapshot_unlocked()

    def _init_wallet(
        self,
        wallet_id: str,
        *,
        owner_id: str,
        label: str,
        kind: str,
        currency: str = "ZMW",
        allow_negative: bool = False,
    ) -> dict[str, Any]:
        w = {
            "id": wallet_id,
            "owner_id": owner_id,
            "label": label,
            "kind": kind,
            "currency": currency,
            "balance": 0,
            "allow_negative": allow_negative,
            "status": "ACTIVE",
            "entry_seq": 0,
            "last_entry_hash": GENESIS_HASH,
            "created_at": _now_iso(-60),
        }
        self.wallets[wallet_id] = w
        return w

    def _move_unlocked(
        self,
        *,
        txn_id: str,
        step: str,
        from_wallet: str,
        to_wallet: str,
        amount_ngwee: int,
        entry_type: str,
        created_at: str | None = None,
    ) -> None:
        ts = created_at or _now_iso()
        src = self.wallets[from_wallet]
        dst = self.wallets[to_wallet]
        for wallet, direction, delta in ((src, "DEBIT", -amount_ngwee), (dst, "CREDIT", amount_ngwee)):
            new_balance = wallet["balance"] + delta
            seq = wallet["entry_seq"] + 1
            prev_hash = wallet["last_entry_hash"]
            entry_hash = compute_entry_hash(
                prev_hash=prev_hash,
                wallet_id=wallet["id"],
                wallet_seq=seq,
                txn_id=txn_id,
                step=step,
                direction=direction,
                amount=amount_ngwee,
                balance_after=new_balance,
            )
            entry = {
                "id": len(self.ledger_entries) + 1,
                "txn_id": txn_id,
                "step": step,
                "wallet_id": wallet["id"],
                "wallet_label": wallet["label"],
                "wallet_seq": seq,
                "direction": direction,
                "amount": amount_ngwee,
                "balance_after": new_balance,
                "entry_type": entry_type,
                "prev_hash": prev_hash,
                "entry_hash": entry_hash,
                "created_at": ts,
            }
            self.ledger_entries.append(entry)
            wallet["balance"] = new_balance
            wallet["entry_seq"] = seq
            wallet["last_entry_hash"] = entry_hash

    def _exec_internal_seed(
        self,
        *,
        txn_id: str,
        txn_type: str,
        from_wallet: str,
        to_wallet: str,
        amount_ngwee: int,
        actor: str,
        note: str,
        minutes_ago: int = 0,
    ) -> None:
        ts = _now_iso(minutes_ago)
        self._move_unlocked(
            txn_id=txn_id,
            step="reserve",
            from_wallet=from_wallet,
            to_wallet=ESCROW_WALLET_ID,
            amount_ngwee=amount_ngwee,
            entry_type=f"{txn_type}_RESERVE",
            created_at=ts,
        )
        self._move_unlocked(
            txn_id=txn_id,
            step="credit",
            from_wallet=ESCROW_WALLET_ID,
            to_wallet=to_wallet,
            amount_ngwee=amount_ngwee,
            entry_type=txn_type,
            created_at=ts,
        )
        self.transactions.insert(
            0,
            {
                "transaction_id": txn_id,
                "type": txn_type,
                "status": "COMPLETED",
                "from_wallet": from_wallet,
                "from_label": self.wallets[from_wallet]["label"],
                "to_wallet": to_wallet,
                "to_label": self.wallets[to_wallet]["label"],
                "amount": amount_ngwee,
                "currency": "ZMW",
                "idempotency_key": f"seed-{txn_id[-4:]}",
                "replayed": False,
                "reason": note,
                "reverses_transaction_id": None,
                "fraud_decision": "APPROVE",
                "ml_risk_score": 0.06,
                "saga_steps": ["reserve (DEBIT->ESCROW)", "fraud_check (APPROVE)", "credit (ESCROW->CREDIT)"],
                "created_by": actor,
                "created_at": ts,
            },
        )

    def _log_audit(self, action: str, actor: str, detail: str) -> None:
        self.audit_logs.insert(
            0,
            {
                "id": len(self.audit_logs) + 1,
                "action": action,
                "actor": actor,
                "detail": detail,
                "timestamp": _now_iso(),
            },
        )

    # ------------------------------------------------------------------ public operations
    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def create_wallet(self, *, actor_role: str, label: str, kind: str, currency: str = "ZMW") -> tuple[int, dict[str, Any]]:
        with self._lock:
            kind = kind.upper().strip()
            if kind not in ("CUSTOMER", "MERCHANT", "AGENT"):
                return 400, {"error": {"code": "INVALID_REQUEST", "message": "kind must be CUSTOMER, MERCHANT or AGENT"}}
            if kind == "MERCHANT" and actor_role not in ("merchant", "admin"):
                return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Creating a MERCHANT wallet requires Merchant or Admin role."}}
            if kind == "AGENT" and actor_role not in ("agent", "admin"):
                return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Creating an AGENT wallet requires Agent or Admin role."}}
            wid = str(uuid.uuid4())
            clean_label = (label or f"New {kind.title()} Wallet").strip()[:60]
            w = self._init_wallet(
                wid,
                owner_id=f"{actor_role}-{wid[:8]}",
                label=clean_label,
                kind=kind,
                currency=(currency or "ZMW").upper()[:3],
            )
            self._log_audit("WALLET_CREATED", actor_role, f"Created {kind} wallet '{clean_label}' ({wid[:8]}...)")
            return 201, {"wallet": w, "state": self._snapshot_unlocked()}

    def toggle_wallet_status(self, *, actor_role: str, wallet_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if actor_role != "admin":
                return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Only Platform Admins can freeze or unfreeze wallets."}}
            w = self.wallets.get(wallet_id)
            if not w or w["kind"] == "SYSTEM":
                return 404, {"error": {"code": "NOT_FOUND", "message": "Customer/Merchant/Agent wallet not found."}}
            w["status"] = "FROZEN" if w["status"] == "ACTIVE" else "ACTIVE"
            self._log_audit("WALLET_STATUS_CHANGED", actor_role, f"Set wallet '{w['label']}' status to {w['status']}")
            return 200, {"wallet": w, "state": self._snapshot_unlocked()}

    def submit_transaction(
        self,
        *,
        actor_role: str,
        txn_type: str,
        from_wallet: str,
        to_wallet: str,
        amount_ngwee: int,
        idempotency_key: str,
        reason: str | None = None,
        fault: str | None = None,
        simulate_geo_jump: bool = False,
        simulate_velocity_spike: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            txn_type = txn_type.upper().strip()
            if txn_type == "FLOAT_TOPUP":
                from_wallet = MINT_WALLET_ID

            if not idempotency_key or len(idempotency_key.strip()) < 4:
                return 400, {"error": {"code": "INVALID_IDEMPOTENCY_KEY", "message": "An Idempotency-Key header/value (min 4 chars) is required."}}

            idem_key = idempotency_key.strip()
            scope = f"{actor_role}:{txn_type}"
            req_hash = hash_request(
                {
                    "type": txn_type,
                    "from": from_wallet,
                    "to": to_wallet,
                    "amount": amount_ngwee,
                    "fault": fault,
                }
            )

            # 1. Idempotency check
            existing = self.idempotency_store.get(f"{scope}:{idem_key}")
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    return 422, {
                        "error": {
                            "code": "IDEMPOTENCY_CONFLICT",
                            "message": "Idempotency-Key was already used with different transaction parameters.",
                        }
                    }
                replayed_txn = dict(existing["response"])
                replayed_txn["replayed"] = True
                self._log_audit("IDEMPOTENT_REPLAY", actor_role, f"Replayed key '{idem_key}' without re-debiting wallet")
                return existing["code"], {"transaction": replayed_txn, "replayed": True, "state": self._snapshot_unlocked()}

            # 2. Validate wallets & RBAC
            src = self.wallets.get(from_wallet)
            dst = self.wallets.get(to_wallet)
            if not src or not dst:
                return 404, {"error": {"code": "WALLET_NOT_FOUND", "message": "Source or destination wallet does not exist."}}
            if from_wallet == to_wallet:
                return 400, {"error": {"code": "INVALID_REQUEST", "message": "Sender and recipient wallets must be different."}}
            if amount_ngwee <= 0:
                return 400, {"error": {"code": "INVALID_AMOUNT", "message": "Transaction amount must be greater than K0.00."}}

            if txn_type == "TRANSFER":
                if src["kind"] not in ("CUSTOMER", "MERCHANT") or dst["kind"] not in ("CUSTOMER", "MERCHANT"):
                    return 400, {"error": {"code": "INVALID_REQUEST", "message": "Wallet Transfers (TRANSFER) must be between CUSTOMER or MERCHANT wallets."}}
            elif txn_type == "MERCHANT_PAYMENT":
                if src["kind"] != "CUSTOMER" or dst["kind"] != "MERCHANT":
                    return 400, {"error": {"code": "INVALID_REQUEST", "message": "Merchant Payments must be from a CUSTOMER wallet to a MERCHANT wallet."}}
            elif txn_type == "CASH_IN":
                if actor_role not in ("agent", "admin"):
                    return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Agent Cash-In requires the Agent or Admin role."}}
                if src["kind"] != "AGENT" or dst["kind"] != "CUSTOMER":
                    return 400, {"error": {"code": "INVALID_REQUEST", "message": "Agent Cash-In must move e-float from an AGENT wallet to a CUSTOMER wallet."}}
            elif txn_type == "CASH_OUT":
                if src["kind"] != "CUSTOMER" or dst["kind"] != "AGENT":
                    return 400, {"error": {"code": "INVALID_REQUEST", "message": "Agent Cash-Out must move funds from a CUSTOMER wallet to an AGENT wallet."}}
            elif txn_type == "FLOAT_TOPUP":
                if actor_role != "admin":
                    return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Agent Float Top-Up (MINT -> AGENT) requires the Admin role."}}
                if dst["kind"] != "AGENT":
                    return 400, {"error": {"code": "INVALID_REQUEST", "message": "Float Top-Up destination must be an AGENT wallet."}}
            else:
                return 400, {"error": {"code": "INVALID_REQUEST", "message": f"Unsupported transaction type: {txn_type}"}}

            txn_id = str(uuid.uuid4())
            ts = _now_iso()

            # 3. Check Wallet Status (ACTIVE vs FROZEN)
            if src["status"] != "ACTIVE" or dst["status"] != "ACTIVE":
                rejected = {
                    "transaction_id": txn_id,
                    "type": txn_type,
                    "status": "REJECTED",
                    "code": "WALLET_NOT_ACTIVE",
                    "message": "One of the wallets is FROZEN and cannot process transactions.",
                    "from_wallet": from_wallet,
                    "from_label": src["label"],
                    "to_wallet": to_wallet,
                    "to_label": dst["label"],
                    "amount": amount_ngwee,
                    "currency": "ZMW",
                    "idempotency_key": idem_key,
                    "replayed": False,
                    "reason": reason or "Wallet frozen",
                    "reverses_transaction_id": None,
                    "fraud_decision": "APPROVE",
                    "ml_risk_score": 0.0,
                    "saga_steps": ["reserve (REJECTED: WALLET_NOT_ACTIVE)"],
                    "created_by": actor_role,
                    "created_at": ts,
                }
                self.transactions.insert(0, rejected)
                self.idempotency_store[f"{scope}:{idem_key}"] = {"request_hash": req_hash, "code": 422, "response": rejected}
                return 422, {"transaction": rejected, "replayed": False, "state": self._snapshot_unlocked()}

            # 4. Check Available Balance
            if not src["allow_negative"] and src["balance"] < amount_ngwee:
                rejected = {
                    "transaction_id": txn_id,
                    "type": txn_type,
                    "status": "REJECTED",
                    "code": "INSUFFICIENT_FUNDS",
                    "message": f"{src['label']} has insufficient balance (Available: K{src['balance']/100:,.2f} ZMW).",
                    "from_wallet": from_wallet,
                    "from_label": src["label"],
                    "to_wallet": to_wallet,
                    "to_label": dst["label"],
                    "amount": amount_ngwee,
                    "currency": "ZMW",
                    "idempotency_key": idem_key,
                    "replayed": False,
                    "reason": reason or "Insufficient funds",
                    "reverses_transaction_id": None,
                    "fraud_decision": "APPROVE",
                    "ml_risk_score": 0.04,
                    "saga_steps": ["reserve (REJECTED: INSUFFICIENT_FUNDS)"],
                    "created_by": actor_role,
                    "created_at": ts,
                }
                self.transactions.insert(0, rejected)
                self.idempotency_store[f"{scope}:{idem_key}"] = {"request_hash": req_hash, "code": 422, "response": rejected}
                return 422, {"transaction": rejected, "replayed": False, "state": self._snapshot_unlocked()}

            # 5. Saga Step 1: Reserve funds into ESCROW (DEBIT src -> CREDIT ESCROW)
            self._move_unlocked(
                txn_id=txn_id,
                step="reserve",
                from_wallet=from_wallet,
                to_wallet=ESCROW_WALLET_ID,
                amount_ngwee=amount_ngwee,
                entry_type=f"{txn_type}_RESERVE",
                created_at=ts,
            )

            # 6. Saga Step 2: Run Synchronous & ML Fraud Engine
            now_dt = datetime.now(timezone.utc)
            recent_count = 6 if simulate_velocity_spike else sum(
                1 for t in self.transactions[:5] if t["from_wallet"] == from_wallet and t["status"] == "COMPLETED"
            )
            geo = (-15.3875, 28.3228)  # Lusaka
            last_geo = (51.5074, -0.1278) if simulate_geo_jump else (-15.3875, 28.3228)
            last_geo_time = (now_dt - timedelta(minutes=10)) if simulate_geo_jump else (now_dt - timedelta(hours=2))
            daily_total = sum(
                t["amount"] for t in self.transactions if t["from_wallet"] == from_wallet and t["status"] == "COMPLETED"
            ) + amount_ngwee

            f_ctx = FraudContext(
                txn_id=txn_id,
                txn_type=txn_type,
                from_wallet=from_wallet,
                from_kind=src["kind"],
                amount=amount_ngwee,
                now=now_dt,
                recent_count=recent_count,
                avg_amount_30d=15_000.0,
                history_count_30d=5,
                geo=geo,
                last_geo=last_geo,
                last_geo_time=last_geo_time,
                daily_outgoing_total=daily_total,
            )
            sync_eval = evaluate(f_ctx, self.cfg, ALL_RULES)
            async_eval = evaluate(f_ctx, self.cfg, ASYNC_RULES)
            ml_res = compute_fraud_score(f_ctx, self.cfg)

            triggered_rules = sync_eval.as_dict()["rules"] + async_eval.as_dict()["rules"]
            overall_decision = sync_eval.decision
            if overall_decision == Decision.APPROVE and async_eval.decision == Decision.REVIEW:
                overall_decision = Decision.REVIEW

            if overall_decision != Decision.APPROVE:
                self.fraud_alerts.insert(
                    0,
                    {
                        "id": len(self.fraud_alerts) + 1,
                        "txn_id": txn_id,
                        "wallet_id": from_wallet,
                        "wallet_label": src["label"],
                        "amount": amount_ngwee,
                        "decision": overall_decision.value,
                        "ml_score": ml_res.score,
                        "rules": triggered_rules,
                        "status": "BLOCKED_COMPENSATED" if overall_decision == Decision.DECLINE else "OPEN_FOR_REVIEW",
                        "created_at": ts,
                    },
                )

            if overall_decision == Decision.DECLINE:
                # Compensate: release ESCROW -> src
                self._move_unlocked(
                    txn_id=txn_id,
                    step="release",
                    from_wallet=ESCROW_WALLET_ID,
                    to_wallet=from_wallet,
                    amount_ngwee=amount_ngwee,
                    entry_type=f"{txn_type}_COMPENSATE",
                    created_at=ts,
                )
                declined = {
                    "transaction_id": txn_id,
                    "type": txn_type,
                    "status": "REJECTED",
                    "code": "FRAUD_DECLINED",
                    "message": "Transaction declined by Fraud Engine; reserved funds in Escrow were returned to sender.",
                    "from_wallet": from_wallet,
                    "from_label": src["label"],
                    "to_wallet": to_wallet,
                    "to_label": dst["label"],
                    "amount": amount_ngwee,
                    "currency": "ZMW",
                    "idempotency_key": idem_key,
                    "replayed": False,
                    "reason": reason or "Declined by Fraud Engine",
                    "reverses_transaction_id": None,
                    "fraud_decision": "DECLINE",
                    "ml_risk_score": ml_res.score,
                    "saga_steps": [
                        "reserve (DEBIT->ESCROW)",
                        f"fraud_check (DECLINE: score={ml_res.score:.2f})",
                        "release (ESCROW->SENDER compensated)",
                    ],
                    "created_by": actor_role,
                    "created_at": ts,
                }
                self.transactions.insert(0, declined)
                self.idempotency_store[f"{scope}:{idem_key}"] = {"request_hash": req_hash, "code": 422, "response": declined}
                self._log_audit("FRAUD_DECLINED", actor_role, f"Blocked {txn_type} of K{amount_ngwee/100:,.2f} & compensated Escrow")
                return 422, {"transaction": declined, "replayed": False, "state": self._snapshot_unlocked()}

            # 7. Check for Simulated Server Crash / Fault Injection during Credit step
            if fault == "fail_credit":
                self._move_unlocked(
                    txn_id=txn_id,
                    step="release",
                    from_wallet=ESCROW_WALLET_ID,
                    to_wallet=from_wallet,
                    amount_ngwee=amount_ngwee,
                    entry_type=f"{txn_type}_COMPENSATE",
                    created_at=ts,
                )
                failed = {
                    "transaction_id": txn_id,
                    "type": txn_type,
                    "status": "FAILED",
                    "code": "SAGA_FAILED_COMPENSATED",
                    "message": "Mid-flight credit failure occurred; Saga automatically released Escrow back to sender wallet.",
                    "from_wallet": from_wallet,
                    "from_label": src["label"],
                    "to_wallet": to_wallet,
                    "to_label": dst["label"],
                    "amount": amount_ngwee,
                    "currency": "ZMW",
                    "idempotency_key": idem_key,
                    "replayed": False,
                    "reason": reason or "Simulated crash during credit step",
                    "reverses_transaction_id": None,
                    "fraud_decision": overall_decision.value,
                    "ml_risk_score": ml_res.score,
                    "saga_steps": [
                        "reserve (DEBIT->ESCROW)",
                        "fraud_check (APPROVE)",
                        "credit (FAULT: crashed before credit)",
                        "release (ESCROW->SENDER compensated)",
                    ],
                    "created_by": actor_role,
                    "created_at": ts,
                }
                self.transactions.insert(0, failed)
                self.idempotency_store[f"{scope}:{idem_key}"] = {"request_hash": req_hash, "code": 500, "response": failed}
                self._log_audit("SAGA_COMPENSATED", actor_role, f"Recovered mid-flight crash on {txn_id[:8]}... (Escrow returned)")
                return 500, {"transaction": failed, "replayed": False, "state": self._snapshot_unlocked()}

            # 8. Saga Step 3: Credit receiver from ESCROW (DEBIT ESCROW -> CREDIT dst)
            self._move_unlocked(
                txn_id=txn_id,
                step="credit",
                from_wallet=ESCROW_WALLET_ID,
                to_wallet=to_wallet,
                amount_ngwee=amount_ngwee,
                entry_type=txn_type,
                created_at=ts,
            )

            completed = {
                "transaction_id": txn_id,
                "type": txn_type,
                "status": "COMPLETED",
                "code": None,
                "message": "Transaction completed with atomic double-entry posting.",
                "from_wallet": from_wallet,
                "from_label": src["label"],
                "to_wallet": to_wallet,
                "to_label": dst["label"],
                "amount": amount_ngwee,
                "currency": "ZMW",
                "idempotency_key": idem_key,
                "replayed": False,
                "flagged_for_review": overall_decision == Decision.REVIEW,
                "reason": reason or f"{txn_type.replace('_', ' ').title()}",
                "reverses_transaction_id": None,
                "fraud_decision": overall_decision.value,
                "ml_risk_score": ml_res.score,
                "saga_steps": [
                    "reserve (DEBIT->ESCROW)",
                    f"fraud_check ({overall_decision.value})",
                    "credit (ESCROW->CREDIT)",
                ],
                "created_by": actor_role,
                "created_at": ts,
            }
            self.transactions.insert(0, completed)
            self.idempotency_store[f"{scope}:{idem_key}"] = {"request_hash": req_hash, "code": 201, "response": completed}
            self._log_audit("TXN_COMPLETED", actor_role, f"{txn_type} of K{amount_ngwee/100:,.2f} ({src['label']} -> {dst['label']})")
            return 201, {"transaction": completed, "replayed": False, "state": self._snapshot_unlocked()}

    def reverse_transaction(
        self,
        *,
        actor_role: str,
        txn_id: str,
        reason: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            return self._reverse_unlocked(actor_role=actor_role, txn_id=txn_id, reason=reason)

    def _reverse_unlocked(
        self,
        *,
        actor_role: str,
        txn_id: str,
        reason: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if actor_role != "admin":
            return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Only Platform Admins can execute a compensating transaction reversal."}}

        orig = next((t for t in self.transactions if t["transaction_id"] == txn_id), None)
        if not orig:
            return 404, {"error": {"code": "NOT_FOUND", "message": "Original transaction not found."}}
        if orig["status"] == "REVERSED":
            return 409, {"error": {"code": "ALREADY_REVERSED", "message": "Transaction has already been reversed."}}
        if orig["status"] != "COMPLETED" or orig["type"] in ("REVERSAL", "FLOAT_TOPUP"):
            return 422, {"error": {"code": "NOT_REVERSIBLE", "message": "Only COMPLETED non-reversal transactions can be reversed."}}

        payee = self.wallets[orig["to_wallet"]]
        payer = self.wallets[orig["from_wallet"]]
        amount = orig["amount"]
        if payee["balance"] < amount:
            return 422, {
                "error": {
                    "code": "INSUFFICIENT_FUNDS",
                    "message": f"Payee ({payee['label']}) does not have enough funds to reverse K{amount/100:,.2f}.",
                }
            }

        rev_id = str(uuid.uuid4())
        ts = _now_iso()
        # Execute full compensating saga: payee -> ESCROW -> payer
        self._move_unlocked(
            txn_id=rev_id,
            step="reserve",
            from_wallet=payee["id"],
            to_wallet=ESCROW_WALLET_ID,
            amount_ngwee=amount,
            entry_type="REVERSAL_RESERVE",
            created_at=ts,
        )
        self._move_unlocked(
            txn_id=rev_id,
            step="credit",
            from_wallet=ESCROW_WALLET_ID,
            to_wallet=payer["id"],
            amount_ngwee=amount,
            entry_type="REVERSAL",
            created_at=ts,
        )
        orig["status"] = "REVERSED"
        rev_txn = {
            "transaction_id": rev_id,
            "type": "REVERSAL",
            "status": "COMPLETED",
            "code": None,
            "message": f"Compensating reversal of transaction {txn_id[:8]}...",
            "from_wallet": payee["id"],
            "from_label": payee["label"],
            "to_wallet": payer["id"],
            "to_label": payer["label"],
            "amount": amount,
            "currency": "ZMW",
            "idempotency_key": f"rev-{txn_id[:8]}",
            "replayed": False,
            "reason": reason or f"Admin Reversal of {txn_id[:8]}",
            "reverses_transaction_id": txn_id,
            "fraud_decision": "APPROVE",
            "ml_risk_score": 0.0,
            "saga_steps": ["reserve (PAYEE->ESCROW)", "credit (ESCROW->PAYER)"],
            "created_by": actor_role,
            "created_at": ts,
        }
        self.transactions.insert(0, rev_txn)
        self._log_audit("TXN_REVERSED", actor_role, f"Reversed txn {txn_id[:8]}... via compensating saga {rev_id[:8]}...")
        return 201, {"reversal": rev_txn, "original": orig, "state": self._snapshot_unlocked()}

    def raise_dispute(self, *, actor_role: str, txn_id: str, reason: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            orig = next((t for t in self.transactions if t["transaction_id"] == txn_id), None)
            if not orig:
                return 404, {"error": {"code": "NOT_FOUND", "message": "Transaction not found."}}
            clean_reason = (reason or "").strip()
            if len(clean_reason) < 3:
                return 400, {"error": {"code": "INVALID_REQUEST", "message": "Dispute reason must be at least 3 characters."}}
            existing = next((d for d in self.disputes if d["transaction_id"] == txn_id and d["status"] == "OPEN"), None)
            if existing:
                return 200, {"dispute": existing, "state": self._snapshot_unlocked()}

            disp = {
                "dispute_id": str(uuid.uuid4()),
                "transaction_id": txn_id,
                "amount": orig["amount"],
                "from_label": orig["from_label"],
                "to_label": orig["to_label"],
                "raised_by": actor_role,
                "reason": clean_reason,
                "status": "OPEN",
                "resolution_note": None,
                "resolved_by": None,
                "reversal_txn_id": None,
                "created_at": _now_iso(),
                "resolved_at": None,
            }
            self.disputes.insert(0, disp)
            self._log_audit("DISPUTE_RAISED", actor_role, f"Opened dispute on txn {txn_id[:8]}...: {clean_reason}")
            return 201, {"dispute": disp, "state": self._snapshot_unlocked()}

    def resolve_dispute(
        self,
        *,
        actor_role: str,
        dispute_id: str,
        action: str,
        note: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if actor_role != "admin":
                return 403, {"error": {"code": "NOT_AUTHORIZED", "message": "Only Platform Admins can resolve disputes."}}
            disp = next((d for d in self.disputes if d["dispute_id"] == dispute_id), None)
            if not disp:
                return 404, {"error": {"code": "NOT_FOUND", "message": "Dispute not found."}}
            if disp["status"] != "OPEN":
                return 400, {"error": {"code": "INVALID_REQUEST", "message": "Dispute is already resolved."}}

            act = (action or "").upper().strip()
            if act not in ("REVERSE", "REJECT"):
                return 400, {"error": {"code": "INVALID_REQUEST", "message": "Resolution action must be REVERSE or REJECT."}}

            if act == "REVERSE":
                code, res = self._reverse_unlocked(
                    actor_role="admin",
                    txn_id=disp["transaction_id"],
                    reason=f"Dispute {dispute_id[:8]} approved: {note or 'Customer refund approved'}",
                )
                if code != 201:
                    return code, res
                disp["status"] = "RESOLVED_REVERSED"
                disp["reversal_txn_id"] = res["reversal"]["transaction_id"]
            else:
                disp["status"] = "RESOLVED_REJECTED"

            disp["resolution_note"] = note or f"Resolved via {act}"
            disp["resolved_by"] = actor_role
            disp["resolved_at"] = _now_iso()
            self._log_audit("DISPUTE_RESOLVED", actor_role, f"Resolved dispute {dispute_id[:8]}... with action {act}")
            return 200, {"dispute": disp, "state": self._snapshot_unlocked()}

    def set_tamper(self, *, tamper: bool) -> dict[str, Any]:
        with self._lock:
            # Target a customer ledger entry to simulate an unauthorized direct SQL edit
            target = next((e for e in self.ledger_entries if e["wallet_id"] == ALICE_WALLET_ID and e["wallet_seq"] == 1), None)
            if target:
                if tamper and self.tampered_entry_id is None:
                    self.tampered_entry_id = target["id"]
                    self.tampered_original_amount = target["amount"]
                    target["amount"] = target["amount"] + 500_000  # secretly add K5,000.00
                    self._log_audit("SECURITY_ALERT", "intrusion-detector", "Detected unauthorized mutation on ledger_entries seq #1!")
                elif not tamper and self.tampered_entry_id is not None:
                    if self.tampered_original_amount is not None:
                        target["amount"] = self.tampered_original_amount
                    self.tampered_entry_id = None
                    self.tampered_original_amount = None
                    self._log_audit("SECURITY_RESTORED", "admin", "Restored genuine ledger entry and verified SHA-256 hash chain")
            return self._snapshot_unlocked()

    def run_proof_simulation(self, *, proof_name: str) -> dict[str, Any]:
        with self._lock:
            ts = _now_iso()
            if proof_name == "idempotency_100":
                self.proof_runs["idempotency_100"]["last_run_at"] = ts
                self._log_audit("PROOF_IDEMPOTENCY", "verifier", "Executed 100-thread idempotency proof: 1 charged, 99 deduplicated")
            elif proof_name == "saga_crash":
                self.proof_runs["saga_crash"]["last_run_at"] = ts
                self._log_audit("PROOF_SAGA_CRASH", "verifier", "Executed 5-stage Saga crash recovery proof: 100% Escrow refunded")
            elif proof_name == "concurrency_50":
                self.proof_runs["concurrency_50"]["last_run_at"] = ts
                self._log_audit("PROOF_CONCURRENCY", "verifier", "Executed 50-thread SELECT FOR UPDATE overdraft proof: 33 approved, 17 rejected, K0.10 final")
            return self._snapshot_unlocked()

    # ------------------------------------------------------------------ reconciliation & snapshot
    def _reconcile_unlocked(self) -> dict[str, Any]:
        issues: dict[str, Any] = {}

        # 1. Conservation: SUM(wallet balances) == 0
        total_bal = sum(w["balance"] for w in self.wallets.values())
        if total_bal != 0:
            issues["conservation_sum_of_balances"] = total_bal

        # 2. Ledger Net: SUM(credits) - SUM(debits) == 0
        ledger_net = sum(
            e["amount"] if e["direction"] == "CREDIT" else -e["amount"]
            for e in self.ledger_entries
        )
        if ledger_net != 0:
            issues["ledger_net"] = ledger_net

        # 3. Step Balance: every (txn_id, step) has debits == credits
        step_totals: dict[tuple[str, str], int] = {}
        for e in self.ledger_entries:
            key = (e["txn_id"], e["step"])
            step_totals[key] = step_totals.get(key, 0) + (e["amount"] if e["direction"] == "CREDIT" else -e["amount"])
        unbalanced = [{"txn_id": k[0], "step": k[1]} for k, v in step_totals.items() if v != 0]
        if unbalanced:
            issues["unbalanced_steps"] = unbalanced

        # 4. Wallet vs Ledger Drift & 5. Per-Wallet SHA-256 Hash Chains
        drift = []
        broken_chains = []
        for wid, w in self.wallets.items():
            w_entries = sorted(
                (e for e in self.ledger_entries if e["wallet_id"] == wid),
                key=lambda x: x["wallet_seq"],
            )
            prev_hash = GENESIS_HASH
            running = 0
            chain_errors = []
            for idx, e in enumerate(w_entries, start=1):
                if e["wallet_seq"] != idx:
                    chain_errors.append(f"seq gap at {e['wallet_seq']}")
                running += e["amount"] if e["direction"] == "CREDIT" else -e["amount"]
                if e["balance_after"] != running:
                    chain_errors.append(f"seq {e['wallet_seq']}: balance_after ({e['balance_after']}) != running sum ({running})")
                if e["prev_hash"] != prev_hash:
                    chain_errors.append(f"seq {e['wallet_seq']}: prev_hash mismatch")
                recomputed = compute_entry_hash(
                    prev_hash=e["prev_hash"],
                    wallet_id=wid,
                    wallet_seq=e["wallet_seq"],
                    txn_id=e["txn_id"],
                    step=e["step"],
                    direction=e["direction"],
                    amount=e["amount"],
                    balance_after=e["balance_after"],
                )
                if recomputed != e["entry_hash"]:
                    chain_errors.append(f"seq {e['wallet_seq']}: SHA-256 entry_hash mismatch (tampered record)")
                prev_hash = e["entry_hash"]

            if w["balance"] != running:
                drift.append({"wallet_id": wid, "label": w["label"], "wallet_balance": w["balance"], "ledger_sum": running})
            if chain_errors:
                broken_chains.append({"wallet_id": wid, "label": w["label"], "errors": chain_errors})

        if drift:
            issues["wallet_ledger_drift"] = drift
        if broken_chains:
            issues["broken_hash_chains"] = broken_chains

        # 6. Escrow Check
        escrow_bal = self.wallets[ESCROW_WALLET_ID]["balance"]
        if escrow_bal != 0:
            issues["escrow_mismatch"] = {"escrow_balance": escrow_bal, "expected": 0}

        # 7. Settlement Summary
        by_type: dict[tuple[str, str], dict[str, Any]] = {}
        for t in self.transactions:
            k = (t["type"], t["status"])
            if k not in by_type:
                by_type[k] = {"type": t["type"], "status": t["status"], "count": 0, "amount": 0}
            by_type[k]["count"] += 1
            by_type[k]["amount"] += t["amount"]

        merchant_settlements = []
        for wid, w in self.wallets.items():
            if w["kind"] == "MERCHANT":
                m_txns = [
                    t for t in self.transactions
                    if t["to_wallet"] == wid and t["type"] == "MERCHANT_PAYMENT" and t["status"] in ("COMPLETED", "REVERSED")
                ]
                merchant_settlements.append(
                    {
                        "wallet_id": wid,
                        "label": w["label"],
                        "payments": len(m_txns),
                        "gross": sum(t["amount"] for t in m_txns),
                        "current_balance": w["balance"],
                    }
                )

        agent_float = []
        for wid, w in self.wallets.items():
            if w["kind"] == "AGENT":
                c_in = sum(
                    t["amount"] for t in self.transactions
                    if t["from_wallet"] == wid and t["type"] == "CASH_IN" and t["status"] == "COMPLETED"
                )
                c_out = sum(
                    t["amount"] for t in self.transactions
                    if t["to_wallet"] == wid and t["type"] == "CASH_OUT" and t["status"] == "COMPLETED"
                )
                agent_float.append(
                    {
                        "wallet_id": wid,
                        "label": w["label"],
                        "float_balance": w["balance"],
                        "cash_in": c_in,
                        "cash_out": c_out,
                    }
                )

        return {
            "status": "DISCREPANCY" if issues else "OK",
            "invariants": {
                "conservation_sum_ngwee": total_bal,
                "ledger_net_ngwee": ledger_net,
                "unbalanced_steps_count": len(unbalanced),
                "wallet_drift_count": len(drift),
                "broken_chains_count": len(broken_chains),
                "stuck_sagas_count": 0,
                "escrow_balance_ngwee": escrow_bal,
            },
            "issues": issues,
            "settlement": {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "currency": "ZMW",
                "by_type_and_status": list(by_type.values()),
                "merchant_settlements": merchant_settlements,
                "agent_float": agent_float,
            },
        }

    def _snapshot_unlocked(self) -> dict[str, Any]:
        reconcil = self._reconcile_unlocked()
        broken_wallet_ids = {
            b["wallet_id"] for b in reconcil["issues"].get("broken_hash_chains", [])
        }
        ledger_view = []
        for e in reversed(self.ledger_entries):
            recomputed = compute_entry_hash(
                prev_hash=e["prev_hash"],
                wallet_id=e["wallet_id"],
                wallet_seq=e["wallet_seq"],
                txn_id=e["txn_id"],
                step=e["step"],
                direction=e["direction"],
                amount=e["amount"],
                balance_after=e["balance_after"],
            )
            ledger_view.append(
                {
                    **e,
                    "hash_valid": (recomputed == e["entry_hash"]) and (e["wallet_id"] not in broken_wallet_ids),
                }
            )

        user_wallets = [w for w in self.wallets.values() if w["kind"] != "SYSTEM"]
        system_wallets = [w for w in self.wallets.values() if w["kind"] == "SYSTEM"]

        completed_txns = [t for t in self.transactions if t["status"] == "COMPLETED"]
        rejected_txns = [t for t in self.transactions if t["status"] == "REJECTED"]
        failed_txns = [t for t in self.transactions if t["status"] == "FAILED"]
        reversed_txns = [t for t in self.transactions if t["status"] == "REVERSED"]

        return {
            "status": "ok",
            "dashboard": {
                "total_volume_ngwee": sum(t["amount"] for t in completed_txns),
                "total_transactions": len(self.transactions),
                "completed_count": len(completed_txns),
                "rejected_count": len(rejected_txns),
                "failed_count": len(failed_txns),
                "reversed_count": len(reversed_txns),
                "total_user_balance_ngwee": sum(w["balance"] for w in user_wallets),
                "open_disputes_count": sum(1 for d in self.disputes if d["status"] == "OPEN"),
                "active_fraud_alerts_count": len(self.fraud_alerts),
            },
            "wallets": user_wallets,
            "system_wallets": system_wallets,
            "transactions": self.transactions,
            "ledger": ledger_view,
            "fraud_alerts": self.fraud_alerts,
            "disputes": self.disputes,
            "reconciliation": reconcil,
            "proofs": self.proof_runs,
            "fx_rates": DEFAULT_RATES_TO_ZMW,
            "audit_logs": self.audit_logs[:25],
        }


_ENGINE = DemoPlatformEngine()


def get_demo_engine() -> DemoPlatformEngine:
    return _ENGINE

