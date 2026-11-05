"""wallet-api: the HTTP service (Flask on gunicorn, ECS Fargate, stateless).

Stateless: no session, no local files, no in-process caches that matter for correctness. Identity
comes from a Cognito JWT on every request; all state lives in Aurora. Any task can serve any
request, which is what lets the ECS service scale out and in freely.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import time

from flask import Flask, Response, g, jsonify, request, send_from_directory

from .. import service
from ..common import metrics
from ..common.db import transaction
from ..common.errors import InvalidRequest, MomoError, NotAuthenticated, NotAuthorized
from ..common.logging_setup import configure_logging, set_correlation_id, set_trace_id
from ..common.tracing import get_recorder, tracing_enabled
from ..ledger.core import MINT_WALLET_ID
from ..saga import faults
from . import user_store
from .auth import JwtVerifier, User, user_from_header

_CID_RE = re.compile(r"^[A-Za-z0-9\-_.:]{8,100}$")
_DEMO_API_ENDPOINTS = {
    "api_state",
    "api_reset",
    "api_create_wallet",
    "api_toggle_wallet_status",
    "api_submit_transaction",
    "api_reverse_transaction",
    "api_raise_dispute",
    "api_resolve_dispute",
    "api_tamper",
    "api_proofs",
}


def create_app(verifier: JwtVerifier | None = None, runner=None) -> Flask:
    if os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"):
        jwt_secret = os.environ.get("JWT_SECRET", "")
        if len(jwt_secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "JWT_SECRET must contain at least 32 bytes when database-backed authentication is enabled"
            )
    configure_logging("wallet-api")
    log = logging.getLogger("wallet-api")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.config["ENABLE_DEMO_API"] = os.environ.get("MOMO_DEMO_API_ENABLED", "false").lower() == "true"
    app.json.sort_keys = False
    state = {"verifier": verifier}
    cors_origins = {
        origin.strip().rstrip("/")
        for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }

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
        if request.endpoint in _DEMO_API_ENDPOINTS and not app.config["ENABLE_DEMO_API"]:
            return jsonify(
                {"error": {"code": "NOT_FOUND", "message": "no such route", "correlation_id": g.cid}}
            ), 404

    @app.after_request
    def _end(resp):
        resp.headers["X-Correlation-ID"] = g.get("cid", "")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin in cors_origins:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Idempotency-Key, X-Correlation-ID"
            )
            resp.headers["Access-Control-Max-Age"] = "600"
            resp.vary.add("Origin")
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
        if (
            g.user.local_token
            and g.user.token_id
            and user_store.is_token_revoked(g.user.token_id)
        ):
            raise NotAuthenticated("invalid or expired token")
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

    def page_args() -> tuple[int, int]:
        try:
            limit = int(request.args.get("limit", "50"))
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            raise InvalidRequest("limit and offset must be integers") from None
        if not 1 <= limit <= 200 or offset < 0:
            raise InvalidRequest("limit must be 1-200 and offset must be non-negative")
        return limit, offset

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
    demo_dir = pathlib.Path(__file__).resolve().parents[3] / "demo"

    @app.get("/")
    def landing_page():
        return send_from_directory(demo_dir, "index.html")

    @app.get("/demo")
    @app.get("/console")
    def demo_ui():
        return send_from_directory(demo_dir, "console.html")

    # ------------------------------------------------------------------ demo / management api
    @app.get("/api/state")
    def api_state():
        from ..demo_state import get_demo_engine
        return jsonify(get_demo_engine().get_state())

    @app.post("/api/reset")
    def api_reset():
        from ..demo_state import get_demo_engine
        return jsonify(get_demo_engine().reset())

    @app.post("/api/wallets/create")
    def api_create_wallet():
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().create_wallet(
            actor_role=data.get("role", "customer"),
            label=data.get("label", ""),
            kind=data.get("kind", "CUSTOMER"),
            currency=data.get("currency", "ZMW"),
        )
        return jsonify(res), code

    @app.post("/api/wallets/<wallet_id>/toggle-status")
    def api_toggle_wallet_status(wallet_id):
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().toggle_wallet_status(
            actor_role=data.get("role", "admin"),
            wallet_id=wallet_id,
        )
        return jsonify(res), code

    @app.post("/api/transactions/submit")
    def api_submit_transaction():
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().submit_transaction(
            actor_role=data.get("role", "customer"),
            txn_type=data.get("type", "TRANSFER"),
            from_wallet=data.get("from_wallet", ""),
            to_wallet=data.get("to_wallet", ""),
            amount_ngwee=int(data.get("amount_ngwee", 0)),
            idempotency_key=data.get("idempotency_key", ""),
            reason=data.get("reason"),
            fault=data.get("fault"),
            simulate_geo_jump=bool(data.get("simulate_geo_jump")),
            simulate_velocity_spike=bool(data.get("simulate_velocity_spike")),
        )
        return jsonify(res), code

    @app.post("/api/transactions/<txn_id>/reverse")
    def api_reverse_transaction(txn_id):
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().reverse_transaction(
            actor_role=data.get("role", "admin"),
            txn_id=txn_id,
            reason=data.get("reason"),
        )
        return jsonify(res), code

    @app.post("/api/disputes/raise")
    def api_raise_dispute():
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().raise_dispute(
            actor_role=data.get("role", "customer"),
            txn_id=data.get("txn_id", ""),
            reason=data.get("reason", ""),
        )
        return jsonify(res), code

    @app.post("/api/disputes/<dispute_id>/resolve")
    def api_resolve_dispute(dispute_id):
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        code, res = get_demo_engine().resolve_dispute(
            actor_role=data.get("role", "admin"),
            dispute_id=dispute_id,
            action=data.get("action", "REVERSE"),
            note=data.get("note"),
        )
        return jsonify(res), code

    @app.post("/api/tamper")
    def api_tamper():
        from ..demo_state import get_demo_engine
        data = request.get_json(silent=True) or {}
        return jsonify(get_demo_engine().set_tamper(tamper=bool(data.get("tamper"))))

    @app.post("/api/proofs/<proof_name>")
    def api_proofs(proof_name):
        from ..demo_state import get_demo_engine
        return jsonify(get_demo_engine().run_proof_simulation(proof_name=proof_name))

    @app.get("/login")
    @app.get("/login.html")
    @app.get("/auth")
    def login_portal():
        html_path = pathlib.Path(__file__).resolve().parents[3] / "demo" / "login.html"
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/login/customer")
    def login_customer_page():
        html_path = pathlib.Path(__file__).resolve().parents[3] / "demo" / "login-customer.html"
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/login/agent")
    def login_agent_page():
        html_path = pathlib.Path(__file__).resolve().parents[3] / "demo" / "login-agent.html"
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/login/admin")
    def login_admin_page():
        html_path = pathlib.Path(__file__).resolve().parents[3] / "demo" / "login-admin.html"
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/login/merchant")
    def login_merchant_page():
        html_path = pathlib.Path(__file__).resolve().parents[3] / "demo" / "login-merchant.html"
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/register")
    @app.get("/register.html")
    def register_page():
        return send_from_directory(demo_dir, "register.html")

    @app.get("/auth-session.js")
    def auth_session_script():
        return send_from_directory(demo_dir, "auth-session.js")

    @app.get("/tokens.css")
    @app.get("/auth.css")
    @app.get("/components.js")
    @app.get("/portal.css")
    @app.get("/auth-pages.css")
    @app.get("/portal.js")
    @app.get("/register.js")
    @app.get("/login.js")
    @app.get("/console.css")
    @app.get("/console.js")
    def demo_asset():
        return send_from_directory(demo_dir, request.path.lstrip("/"))

    @app.get("/customer")
    @app.get("/customer/dashboard")
    def customer_dashboard():
        return send_from_directory(demo_dir / "pages" / "customer", "dashboard.html")

    @app.get("/customer/send-money")
    def customer_send_money_page():
        return send_from_directory(demo_dir / "pages" / "customer", "send-money.html")

    @app.get("/customer/merchant-payment")
    def customer_merchant_payment_page():
        return send_from_directory(demo_dir / "pages" / "customer", "merchant-payment.html")

    @app.get("/customer/cash-out")
    def customer_cash_out_page():
        return send_from_directory(demo_dir / "pages" / "customer", "cash-out.html")

    @app.get("/customer/transactions")
    def customer_transactions_page():
        return send_from_directory(demo_dir / "pages" / "customer", "transactions.html")

    @app.get("/customer/profile")
    def customer_profile_page():
        return send_from_directory(demo_dir / "pages" / "customer", "profile.html")

    @app.get("/agent")
    @app.get("/agent/dashboard")
    def agent_dashboard():
        return send_from_directory(demo_dir / "pages" / "agent", "dashboard.html")

    @app.get("/agent/cash-in")
    def agent_cash_in_page():
        return send_from_directory(demo_dir / "pages" / "agent", "cash-in.html")

    @app.get("/agent/cash-out")
    def agent_cash_out_page():
        return send_from_directory(demo_dir / "pages" / "agent", "cash-out.html")

    @app.get("/agent/transactions")
    def agent_transactions_page():
        return send_from_directory(demo_dir / "pages" / "agent", "transactions.html")

    @app.get("/agent/profile")
    def agent_profile_page():
        return send_from_directory(demo_dir / "pages" / "agent", "profile.html")

    @app.get("/admin")
    @app.get("/admin/dashboard")
    def admin_dashboard():
        return send_from_directory(demo_dir / "pages" / "admin", "dashboard.html")

    @app.get("/merchant")
    @app.get("/merchant/dashboard")
    def merchant_dashboard():
        return send_from_directory(demo_dir, "console.html")

    @app.get("/admin/users")
    def admin_users_page():
        return send_from_directory(demo_dir / "pages" / "admin", "users.html")

    @app.get("/admin/transactions")
    def admin_transactions_page():
        return send_from_directory(demo_dir / "pages" / "admin", "transactions.html")

    @app.get("/admin/analytics")
    def admin_analytics_page():
        return send_from_directory(demo_dir / "pages" / "admin", "analytics.html")

    @app.get("/admin/profile")
    def admin_profile_page():
        return send_from_directory(demo_dir / "pages" / "admin", "profile.html")

    # ------------------------------------------------------------------ auth API endpoints
    @app.post("/auth/register")
    def auth_register():
        data = body()
        res = user_store.register_customer(
            phone=data.get("phone", ""),
            password=data.get("password", ""),
            full_name=data.get("full_name", ""),
            currency=data.get("currency", "ZMW"),
        )
        return jsonify(res), 201

    @app.post("/auth/login/customer")
    def auth_login_customer():
        data = body()
        return jsonify(user_store.authenticate_user(
            identifier=data.get("phone") or data.get("username", ""),
            password=data.get("password", ""),
            expected_role="customer",
        ))

    @app.post("/auth/login/agent")
    def auth_login_agent():
        data = body()
        return jsonify(user_store.authenticate_user(
            identifier=data.get("agent_id") or data.get("phone") or data.get("username", ""),
            password=data.get("password", ""),
            expected_role="agent",
        ))

    @app.post("/auth/login/admin")
    def auth_login_admin():
        data = body()
        return jsonify(user_store.authenticate_user(
            identifier=data.get("username") or data.get("email", ""),
            password=data.get("password", ""),
            expected_role="admin",
        ))

    @app.post("/auth/login/merchant")
    def auth_login_merchant():
        data = body()
        return jsonify(user_store.authenticate_user(
            identifier=data.get("merchant_id") or data.get("phone") or data.get("username", ""),
            password=data.get("password", ""),
            expected_role="merchant",
        ))

    @app.get("/auth/me")
    def auth_me():
        user = current_user()
        return jsonify({
            "user": {
                "id": user.sub,
                "username": user.username or user.sub,
                "groups": list(user.groups),
                "role": user.role,
                "is_admin": user.is_admin,
            }
        })

    @app.post("/auth/logout")
    def auth_logout():
        user = current_user()
        if user.local_token and user.token_id and user.token_expires_at:
            user_store.revoke_token(user.token_id, user.token_expires_at)
        return jsonify({"status": "ok", "message": "Logged out successfully"})

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

    @app.get("/wallets")
    def list_wallets():
        user = current_user()
        limit, offset = page_args()
        kind = request.args.get("kind")
        status = request.args.get("status")
        currency = request.args.get("currency")
        search = request.args.get("search", "").strip()
        if len(search) > 100:
            raise InvalidRequest("search must be at most 100 characters")
        if kind and kind not in {"CUSTOMER", "MERCHANT", "AGENT"}:
            raise InvalidRequest("unsupported wallet kind")
        if status and status not in {"ACTIVE", "FROZEN", "CLOSED"}:
            raise InvalidRequest("unsupported wallet status")
        if currency and (len(currency) != 3 or not currency.isalpha()):
            raise InvalidRequest("currency must be a three-letter code")
        result = service.list_wallets(
            user.sub, is_admin=user.is_admin, kind=kind, status=status,
            currency=currency.upper() if currency else None, search=search or None,
            limit=limit, offset=offset,
        )
        return jsonify(result)

    @app.post("/wallets")
    def create_wallet():
        user = current_user()
        data = body()
        kind = data.get("kind", "CUSTOMER")
        if kind == "MERCHANT":
            user.require_group("merchants")
        elif kind == "AGENT":
            user.require_group("agents")
        elif kind == "CUSTOMER":
            user.require_group("customers")
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

    @app.get("/transactions")
    def list_transactions():
        user = current_user()
        limit, offset = page_args()
        txn_type = request.args.get("type")
        status = request.args.get("status")
        wallet_id = request.args.get("wallet_id")
        search = request.args.get("search", "").strip()
        if txn_type and txn_type not in {
            "TRANSFER", "MERCHANT_PAYMENT", "CASH_IN", "CASH_OUT", "FLOAT_TOPUP", "REVERSAL",
        }:
            raise InvalidRequest("unsupported transaction type")
        if status and status not in {"PENDING", "COMPLETED", "REJECTED", "FAILED", "REVERSED"}:
            raise InvalidRequest("unsupported transaction status")
        if len(search) > 100:
            raise InvalidRequest("search must be at most 100 characters")
        if wallet_id:
            wallet = service.get_wallet(wallet_id)
            if not (owns(user, wallet) or user.is_admin):
                raise NotAuthorized("not your wallet")
        return jsonify(service.list_transactions(
            user.sub, is_admin=user.is_admin, txn_type=txn_type, status=status,
            wallet_id=wallet_id, search=search or None, limit=limit, offset=offset,
        ))

    @app.get("/operations")
    def get_operation():
        user = current_user()
        txn_type = request.args.get("type", "")
        key = request.args.get("key", "")
        return jsonify(service.lookup_operation(user.sub, txn_type, key))

    @app.get("/wallets/<wallet_id>/ledger")
    def wallet_ledger(wallet_id):
        user = current_user()
        wallet = service.get_wallet(wallet_id)
        if not (owns(user, wallet) or user.is_admin):
            raise NotAuthorized("not your wallet")
        limit = request.args.get("limit", default=50, type=int)
        return jsonify({"wallet_id": wallet["id"], "entries": service.get_ledger(wallet_id, limit)})

    @app.get("/transactions/<txn_id>/ledger")
    def transaction_ledger(txn_id):
        user = current_user()
        txn = service.get_transaction(txn_id)
        owners = {
            service.get_wallet(txn["from_wallet"])["owner_id"],
            service.get_wallet(txn["to_wallet"])["owner_id"],
        }
        if not user.is_admin and user.sub not in owners:
            raise NotAuthorized("not your transaction")
        return jsonify({
            "transaction_id": txn["transaction_id"],
            "entries": service.get_transaction_ledger(txn_id),
        })

    @app.post("/transfers")
    def transfer():
        user = current_user()
        if not (user.is_customer or "merchants" in user.groups or user.is_admin):
            raise NotAuthorized("transfers require a customer or merchant account")
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
        if not user.is_customer and not user.is_admin:
            raise NotAuthorized("merchant payments require a customer account")
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
        if not user.is_customer and not user.is_admin:
            raise NotAuthorized("cash-out requires a customer account")
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

    @app.get("/disputes")
    def list_disputes():
        user = current_user()
        limit, offset = page_args()
        return jsonify(service.list_disputes(
            user.sub, is_admin=user.is_admin, limit=limit, offset=offset,
        ))

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

    @app.get("/admin/fraud-alerts")
    def list_fraud_alerts():
        current_user().require_group("admins")
        limit, offset = page_args()
        return jsonify(service.list_fraud_alerts(limit=limit, offset=offset))

    @app.get("/admin/settlement-reports")
    def list_settlement_reports():
        current_user().require_group("admins")
        limit, offset = page_args()
        return jsonify(service.list_settlement_reports(limit=limit, offset=offset))

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
        if not (user.is_customer or "merchants" in user.groups or user.is_admin):
            raise NotAuthorized("recurring payments require a customer or merchant account")
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
