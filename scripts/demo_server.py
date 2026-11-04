#!/usr/bin/env python3
"""Interactive Demo Interface server for the Mobile Money Platform presentation.

Serves the public landing page, role-specific UI previews, and operations console from
`demo/` at http://127.0.0.1:5050. Role-based pages and the operations console proxy
their API requests to the Flask backend configured by MOMO_API_BASE_URL. The separate
`/api/*` routes remain an in-memory demo simulation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import sys
import urllib.error
import urllib.request
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
DEFAULT_API_BASE_URL = "http://127.0.0.1:8080"

# Ensure `src` is in sys.path
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from momo.demo_state import get_demo_engine
except ImportError:
    get_demo_engine = None


def _load_cost_data() -> dict:
    cost_path = ROOT / "scripts" / "cost_model.py"
    if not cost_path.exists():
        return {}
    spec = importlib.util.spec_from_file_location("cost_model", cost_path)
    if spec is None or spec.loader is None:
        return {}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {k: mod.model(**v) for k, v in mod.SCENARIOS.items()}


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if self._is_api_request("GET", path):
            self._proxy_api_request()
            return
        page_routes = {
            "/login": "/login.html",
            "/register": "/register.html",
            "/login/customer": "/login-customer.html",
            "/login/agent": "/login-agent.html",
            "/login/admin": "/login-admin.html",
            "/customer": "/pages/customer/dashboard.html",
            "/customer/dashboard": "/pages/customer/dashboard.html",
            "/customer/send-money": "/pages/customer/send-money.html",
            "/customer/merchant-payment": "/pages/customer/merchant-payment.html",
            "/customer/cash-out": "/pages/customer/cash-out.html",
            "/customer/transactions": "/pages/customer/transactions.html",
            "/customer/profile": "/pages/customer/profile.html",
            "/agent": "/pages/agent/dashboard.html",
            "/agent/dashboard": "/pages/agent/dashboard.html",
            "/agent/cash-in": "/pages/agent/cash-in.html",
            "/agent/cash-out": "/pages/agent/cash-out.html",
            "/agent/transactions": "/pages/agent/transactions.html",
            "/agent/profile": "/pages/agent/profile.html",
            "/admin": "/pages/admin/dashboard.html",
            "/admin/dashboard": "/pages/admin/dashboard.html",
            "/admin/users": "/pages/admin/users.html",
            "/admin/transactions": "/pages/admin/transactions.html",
            "/admin/analytics": "/pages/admin/analytics.html",
            "/admin/profile": "/pages/admin/profile.html",
        }
        if parsed.path in page_routes:
            self.path = page_routes[parsed.path]
            super().do_GET()
            return
        if parsed.path in ("/demo", "/console"):
            self.path = "/console.html"
            super().do_GET()
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
            return
        if path == "/api/status":
            proof_file = ROOT / "evidence-out" / "proof-results.json"
            proofs = json.loads(proof_file.read_text(encoding="utf-8")) if proof_file.exists() else {}
            self._json({"status": "ok", "proofs": proofs, "costs": _load_cost_data()})
            return
        if path == "/api/state":
            if get_demo_engine:
                st = get_demo_engine().get_state()
                st["costs"] = _load_cost_data()
                self._json(st)
            else:
                self._json({"status": "engine_unavailable"}, 500)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if self._is_api_request("POST", path):
            self._proxy_api_request()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                body = {}

        if not get_demo_engine:
            self._json({"error": {"message": "Demo engine unavailable"}}, 500)
            return

        engine = get_demo_engine()

        if path == "/api/reset":
            st = engine.reset()
            st["costs"] = _load_cost_data()
            self._json(st)
            return

        if path == "/api/wallets/create":
            code, res = engine.create_wallet(
                actor_role=body.get("role", "customer"),
                label=body.get("label", ""),
                kind=body.get("kind", "CUSTOMER"),
                currency=body.get("currency", "ZMW"),
            )
            self._json(res, code)
            return

        if path.startswith("/api/wallets/") and path.endswith("/toggle-status"):
            parts = path.split("/")
            wid = parts[3]
            code, res = engine.toggle_wallet_status(actor_role=body.get("role", "admin"), wallet_id=wid)
            self._json(res, code)
            return

        if path == "/api/transactions/submit":
            code, res = engine.submit_transaction(
                actor_role=body.get("role", "customer"),
                txn_type=body.get("type", "TRANSFER"),
                from_wallet=body.get("from_wallet", ""),
                to_wallet=body.get("to_wallet", ""),
                amount_ngwee=int(body.get("amount_ngwee", 0)),
                idempotency_key=body.get("idempotency_key", ""),
                reason=body.get("reason"),
                fault=body.get("fault"),
                simulate_geo_jump=bool(body.get("simulate_geo_jump")),
                simulate_velocity_spike=bool(body.get("simulate_velocity_spike")),
            )
            self._json(res, code)
            return

        if path.startswith("/api/transactions/") and path.endswith("/reverse"):
            parts = path.split("/")
            txn_id = parts[3]
            code, res = engine.reverse_transaction(
                actor_role=body.get("role", "admin"),
                txn_id=txn_id,
                reason=body.get("reason"),
            )
            self._json(res, code)
            return

        if path == "/api/disputes/raise":
            code, res = engine.raise_dispute(
                actor_role=body.get("role", "customer"),
                txn_id=body.get("txn_id", ""),
                reason=body.get("reason", ""),
            )
            self._json(res, code)
            return

        if path.startswith("/api/disputes/") and path.endswith("/resolve"):
            parts = path.split("/")
            disp_id = parts[3]
            code, res = engine.resolve_dispute(
                actor_role=body.get("role", "admin"),
                dispute_id=disp_id,
                action=body.get("action", "REVERSE"),
                note=body.get("note"),
            )
            self._json(res, code)
            return

        if path == "/api/tamper":
            res = engine.set_tamper(tamper=bool(body.get("tamper")))
            self._json(res)
            return

        if path.startswith("/api/proofs/"):
            proof_name = path.split("/")[-1]
            res = engine.run_proof_simulation(proof_name=proof_name)
            self._json(res)
            return

        self._json({"error": {"code": "NOT_FOUND", "message": "Unknown endpoint"}}, 404)

    @staticmethod
    def _is_api_request(method: str, path: str) -> bool:
        if method == "GET":
            return (
                path in {
                    "/health", "/auth/me", "/wallets", "/transactions", "/operations",
                    "/disputes", "/admin/integrity", "/admin/fraud-alerts",
                    "/admin/settlement-reports", "/fx/rates", "/recurring-payments",
                }
                or path.startswith(("/wallets/", "/transactions/", "/disputes/"))
            )
        if method != "POST":
            return False
        return (
            path in {
                "/auth/register", "/auth/login/customer", "/auth/login/agent", "/auth/login/admin",
                "/auth/logout", "/wallets", "/transfers", "/merchant-payments", "/agents/cash-in",
                "/agents/cash-out", "/admin/float-topups", "/admin/recurring-payments/run-due",
                "/fx/quotes", "/recurring-payments",
            }
            or path.startswith(("/transactions/", "/disputes/", "/recurring-payments/"))
        )

    def _proxy_api_request(self) -> None:
        api_base = os.environ.get("MOMO_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
        parsed_base = urlsplit(api_base)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.username
            or parsed_base.password
            or parsed_base.path not in {"", "/"}
            or parsed_base.query
            or parsed_base.fragment
        ):
            self._json({"error": {"message": "MOMO_API_BASE_URL must be an HTTP(S) origin without credentials or a path."}}, 500)
            return

        request_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length", "transfer-encoding", "accept-encoding"}
        }
        request_body = None
        if self.command != "GET":
            length = int(self.headers.get("Content-Length", 0))
            request_body = self.rfile.read(length) if length else b""
        request = urllib.request.Request(
            f"{api_base}{self.path}",
            data=request_body,
            headers=request_headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                response_headers = response.headers
                response_body = response.read()
        except urllib.error.HTTPError as response:
            status = response.code
            response_headers = response.headers
            response_body = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"API proxy request failed: {error}", file=sys.stderr)
            self._json({"error": {"message": "Backend API is unavailable. Start the API or check MOMO_API_BASE_URL."}}, 502)
            return

        self.send_response(status)
        for key in ("Content-Type", "Cache-Control", "X-Correlation-ID", "WWW-Authenticate"):
            value = response_headers.get(key)
            if value:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def _json(self, data: dict, code: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        return


def _pick_port(preferred: int = 5050) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main() -> None:
    port = _pick_port(5050)
    url = f"http://127.0.0.1:{port}"
    server = ReusableHTTPServer(("127.0.0.1", port), DemoHandler)
    print("\n============================================================")
    print("  MoMo Platform — Interactive Demo Interface is LIVE!")
    print(f"  Open in browser: {url}")
    print(f"  Flask API:       {os.environ.get('MOMO_API_BASE_URL', DEFAULT_API_BASE_URL)}")
    print(f"  Direct file:     {DEMO_DIR / 'index.html'}")
    print("  Press Ctrl+C to stop.")
    print("============================================================\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Demo Interface server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
