"""wallet-api: the HTTP service (Flask on gunicorn, ECS Fargate, stateless).

Stateless: no session, no local files, no in-process caches that matter for correctness. Identity
comes from a Cognito JWT on every request; all state lives in Aurora. Any task can serve any
request, which is what lets the ECS service scale out and in freely.
"""

from __future__ import annotations

import logging
import os
import re
import time

from flask import Flask, g, jsonify, request

from .. import service
from ..common import metrics
from ..common.db import transaction
from ..common.errors import InvalidRequest, MomoError, NotAuthorized
from ..common.logging_setup import configure_logging, set_correlation_id, set_trace_id
from ..common.tracing import get_recorder, tracing_enabled
from ..ledger.core import MINT_WALLET_ID
from ..saga import faults
from .auth import JwtVerifier, User, user_from_header

_CID_RE = re.compile(r"^[A-Za-z0-9\-_.:]{8,100}$")


def create_app(verifier: JwtVerifier | None = None, runner=None) -> Flask:
    configure_logging("wallet-api")
    log = logging.getLogger("wallet-api")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.json.sort_keys = False
    state = {"verifier": verifier}

    if tracing_enabled():  # pragma: no cover - needs the X-Ray daemon
        from aws_xray_sdk.ext.flask.middleware import XRayMiddleware

        XRayMiddleware(app, get_recorder())

    def get_verifier() -> JwtVerifier:
        if state["verifier"] is None:
            state["verifier"] = JwtVerifier.from_env()
        return state["verifier"]

    # ------------------------------------------------------------------ request plumbing
    @app.before_request
    def _begin():
        incoming = request.headers.get("X-Correlation-ID", "")
        g.cid = set_correlation_id(incoming if _CID_RE.match(incoming) else None)
        set_trace_id(request.headers.get("X-Amzn-Trace-Id", "").replace("Root=", "").split(";")[0] or None)
        g.t0 = time.perf_counter()
        g.user = None

    @app.after_request
    def _end(resp):
        resp.headers["X-Correlation-ID"] = g.get("cid", "")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.path != "/health":  # keep ALB health checks out of the access log / SLIs
            log.info(
                "request",
                extra={
                    "method": request.method, "path": request.path, "status": resp.status_code,
                    "latency_ms": round((time.perf_counter() - g.get("t0", time.perf_counter())) * 1000, 1),
                    "user": g.user.sub if g.get("user") else None,
                },
            )
        return resp

    @app.errorhandler(MomoError)
    def _momo_error(exc: MomoError):
        body = {"error": {"code": exc.code, "message": exc.message, "correlation_id": g.get("cid")}}
        if exc.http_status == 409 and exc.code == "REQUEST_IN_PROGRESS":
            return jsonify(body), 409, {"Retry-After": "1"}
        return jsonify(body), exc.http_status

    @app.errorhandler(404)
    def _not_found(_):
        return jsonify({"error": {"code": "NOT_FOUND", "message": "no such route", "correlation_id": g.get("cid")}}), 404

    @app.errorhandler(405)
    def _bad_method(_):
        return jsonify({"error": {"code": "METHOD_NOT_ALLOWED", "message": "method not allowed", "correlation_id": g.get("cid")}}), 405

    @app.errorhandler(413)
    def _too_large(_):
        return jsonify({"error": {"code": "INVALID_REQUEST", "message": "body too large", "correlation_id": g.get("cid")}}), 413

    @app.errorhandler(Exception)
    def _unexpected(exc):
        log.exception("unhandled error")
        metrics.emit("UnhandledError", 1, Service="wallet-api")
        return jsonify({"error": {"code": "INTERNAL_ERROR", "message": "internal error", "correlation_id": g.get("cid")}}), 500

    # ------------------------------------------------------------------ helpers
    def current_user() -> User:
        g.user = user_from_header(get_verifier(), request.headers.get("Authorization"))
        return g.user

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise InvalidRequest("a JSON object body is required")
        return data

    def owns(user: User, wallet: dict) -> bool:
        return wallet["owner_id"] == user.sub

    def respond(result: tuple[int, dict, bool]):
        code, payload, replayed = result
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return jsonify(payload), code, headers

    def idem_key() -> str | None:
        return request.headers.get("Idempotency-Key")

    def fault() -> str | None:
        return request.headers.get("X-Fault-Inject") if faults.enabled() else None

    def submit(txn_type: str, user: User, payload: dict, from_wallet: str | None = None):
        if from_wallet:
            payload = {**payload, "from_wallet": from_wallet}
        req = service.parse_txn_request(txn_type, payload)
        result = service.submit(
            user.sub, req, idem_key(), g.cid, fault=fault(), runner=runner
        )
        metrics.emit("TransactionRequest", 1, Outcome=result[1].get("status", "UNKNOWN"))
        return respond(result)

    # ------------------------------------------------------------------ routes
    @app.get("/health")
    def health():
        if os.environ.get("FORCE_UNHEALTHY", "false").lower() == "true":  # rollback demo hook
            return jsonify({"status": "unhealthy"}), 503
        try:
            with transaction() as conn:
                conn.execute("SELECT 1")
        except Exception:
            return jsonify({"status": "db_unreachable"}), 503
        return jsonify({"status": "ok"})

    @app.post("/wallets")
    def create_wallet():
        user = current_user()
        data = body()
        kind = data.get("kind", "CUSTOMER")
        if kind == "MERCHANT":
            user.require_group("merchants")
        elif kind == "AGENT":
            user.require_group("agents")
        elif kind != "CUSTOMER":
            raise InvalidRequest("kind must be CUSTOMER, MERCHANT or AGENT")
        return jsonify(service.create_wallet(user.sub, kind, data.get("currency"))), 201

    @app.get("/wallets/<wallet_id>")
    def get_wallet(wallet_id):
        user = current_user()
        wallet = service.get_wallet(wallet_id)
        if not (owns(user, wallet) or user.is_admin):
            raise NotAuthorized("not your wallet")
        return jsonify(wallet)

    @app.get("/wallets/<wallet_id>/ledger")
    def wallet_ledger(wallet_id):
        user = current_user()
        wallet = service.get_wallet(wallet_id)
        if not (owns(user, wallet) or user.is_admin):
            raise NotAuthorized("not your wallet")
        limit = request.args.get("limit", default=50, type=int)
        return jsonify({"wallet_id": wallet["id"], "entries": service.get_ledger(wallet_id, limit)})

    @app.post("/transfers")
    def transfer():
        user = current_user()
        data = body()
        src = service.get_wallet(data.get("from_wallet", ""))
        dst = service.get_wallet(data.get("to_wallet", ""))
        if not owns(user, src):
            raise NotAuthorized("you can only send from your own wallet")
        if src["kind"] not in ("CUSTOMER", "MERCHANT") or dst["kind"] not in ("CUSTOMER", "MERCHANT"):
            raise InvalidRequest("transfers are between customer/merchant wallets")
        return submit("TRANSFER", user, data)

    @app.post("/merchant-payments")
    def merchant_payment():
        user = current_user()
        data = body()
        src = service.get_wallet(data.get("from_wallet", ""))
        dst = service.get_wallet(data.get("to_wallet", ""))
        if not owns(user, src) or src["kind"] != "CUSTOMER":
            raise NotAuthorized("pay from your own customer wallet")
        if dst["kind"] != "MERCHANT":
            raise InvalidRequest("destination must be a merchant wallet")
        return submit("MERCHANT_PAYMENT", user, data)

    @app.post("/agents/cash-in")
    def cash_in():
        """Customer hands cash to an agent; the agent's e-float is credited to the customer."""
        user = current_user()
        user.require_group("agents")
        data = body()
        src = service.get_wallet(data.get("from_wallet", ""))
        dst = service.get_wallet(data.get("to_wallet", ""))
        if not owns(user, src) or src["kind"] != "AGENT":
            raise NotAuthorized("cash-in must be funded from your own agent float wallet")
        if dst["kind"] != "CUSTOMER":
            raise InvalidRequest("destination must be a customer wallet")
        return submit("CASH_IN", user, data)

    @app.post("/agents/cash-out")
    def cash_out():
        """Customer withdraws cash from an agent; e-money moves from the customer to agent float."""
        user = current_user()
        data = body()
        src = service.get_wallet(data.get("from_wallet", ""))
        dst = service.get_wallet(data.get("to_wallet", ""))
        if not owns(user, src) or src["kind"] != "CUSTOMER":
            raise NotAuthorized("cash-out must come from your own customer wallet")
        if dst["kind"] != "AGENT":
            raise InvalidRequest("destination must be an agent wallet")
        return submit("CASH_OUT", user, data)

    @app.post("/admin/float-topups")
    def float_topup():
        """Issue e-money to an agent's float (MINT -> agent). Admin only."""
        user = current_user()
        user.require_group("admins")
        data = body()
        dst = service.get_wallet(data.get("to_wallet", ""))
        if dst["kind"] != "AGENT":
            raise InvalidRequest("destination must be an agent wallet")
        return submit("FLOAT_TOPUP", user, data, from_wallet=MINT_WALLET_ID)

    @app.get("/admin/integrity")
    def integrity():
        """Global invariants: conservation, ledger balance, hash chains, stuck sagas, escrow."""
        current_user().require_group("admins")
        from ..reconciliation.report import check_integrity

        return jsonify(check_integrity())

    @app.get("/transactions/<txn_id>")
    def get_transaction(txn_id):
        user = current_user()
        txn = service.get_transaction(txn_id)
        if not user.is_admin:
            owners = {service.get_wallet(txn["from_wallet"])["owner_id"], service.get_wallet(txn["to_wallet"])["owner_id"]}
            if user.sub not in owners:
                raise NotAuthorized("not your transaction")
        return jsonify(txn)

    @app.post("/transactions/<txn_id>/reverse")
    def reverse(txn_id):
        user = current_user()
        user.require_group("admins")
        data = request.get_json(silent=True) or {}
        return respond(
            service.reverse_transaction(user.sub, txn_id, data.get("reason"), idem_key(), g.cid, runner=runner)
        )

    @app.post("/transactions/<txn_id>/disputes")
    def raise_dispute(txn_id):
        user = current_user()
        txn = service.get_transaction(txn_id)
        owners = {service.get_wallet(txn["from_wallet"])["owner_id"], service.get_wallet(txn["to_wallet"])["owner_id"]}
        if user.sub not in owners and not user.is_admin:
            raise NotAuthorized("only a party to the transaction can dispute it")
        return jsonify(service.raise_dispute(user.sub, txn_id, body().get("reason"))), 201

    @app.get("/disputes/<dispute_id>")
    def get_dispute(dispute_id):
        user = current_user()
        dispute = service.get_dispute(dispute_id)
        if dispute["raised_by"] != user.sub and not user.is_admin:
            raise NotAuthorized("not your dispute")
        return jsonify(dispute)

    @app.post("/disputes/<dispute_id>/resolve")
    def resolve_dispute(dispute_id):
        user = current_user()
        user.require_group("admins")
        data = body()
        return jsonify(
            service.resolve_dispute(user.sub, dispute_id, data.get("action"), data.get("note"), g.cid, runner=runner)
        )

    # ------------------------------------------------------------------ stretch goals: FX & Recurring
    @app.get("/fx/rates")
    def get_fx_rates():
        """Get supported FX currency conversion rates."""
        from ..fx.service import DEFAULT_RATES_TO_ZMW
        return jsonify({"base_currency": "ZMW", "rates": DEFAULT_RATES_TO_ZMW})

    @app.post("/fx/quotes")
    def get_fx_quote():
        """Request a cryptographic, guaranteed FX quote for cross-currency transfers."""
        current_user()  # requires authenticated user
        data = body()
        from ..fx.service import create_quote
        q = create_quote(
            from_currency=data.get("from_currency", "ZMW"),
            to_currency=data.get("to_currency", "ZMW"),
            from_amount=data.get("amount", 0),
        )
        return jsonify(q.to_dict()), 201

    @app.post("/recurring-payments")
    def create_recurring_payment():
        """Create a scheduled recurring transfer or standing order."""
        user = current_user()
        data = body()
        src = service.get_wallet(data.get("from_wallet", ""))
        if not owns(user, src):
            raise NotAuthorized("recurring payment must be funded from your own wallet")
        from ..recurring.service import get_recurring_registry
        sched = get_recurring_registry().create(
            owner_id=user.sub,
            from_wallet=data.get("from_wallet", ""),
            to_wallet=data.get("to_wallet", ""),
            amount=data.get("amount", 0),
            frequency=data.get("frequency", "MONTHLY"),
            currency=data.get("currency", "ZMW"),
        )
        return jsonify(sched.to_dict()), 201

    @app.get("/recurring-payments")
    def list_recurring_payments():
        """List active recurring payment schedules for the authenticated user."""
        user = current_user()
        from ..recurring.service import get_recurring_registry
        schedules = get_recurring_registry().list_by_owner(user.sub)
        return jsonify({"schedules": [s.to_dict() for s in schedules]})

    @app.post("/recurring-payments/<schedule_id>/cancel")
    def cancel_recurring_payment(schedule_id):
        """Cancel an active recurring payment schedule."""
        user = current_user()
        from ..recurring.service import get_recurring_registry
        sched = get_recurring_registry().cancel(schedule_id, owner_id=user.sub if not user.is_admin else None)
        return jsonify(sched.to_dict())

    @app.post("/admin/recurring-payments/run-due")
    def run_due_recurring_payments():
        """Trigger execution of all due recurring payments. Admin only."""
        current_user().require_group("admins")
        from ..recurring.service import process_due_recurring_transfers

        def _exec(idempotency_key, from_wallet, to_wallet, amount, currency):
            return service.submit_transaction(
                owner_id="system-recurring",
                txn_type="TRANSFER",
                from_wallet=from_wallet,
                to_wallet=to_wallet,
                amount=amount,
                currency=currency,
                idem_key=idempotency_key,
                correlation_id=g.cid,
                runner=runner,
            )

        res = process_due_recurring_transfers(transfer_executor=_exec)
        return jsonify({"processed": len(res), "results": res})

    return app
