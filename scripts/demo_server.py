#!/usr/bin/env python3
"""Interactive Demo Interface server for the Mobile Money Platform presentation.

Serves the full visual Demo UI (`demo/index.html`) at http://127.0.0.1:5050 and exposes
live JSON endpoints (`/health`, `/api/state`, `/api/transactions/submit`, etc.) backed
by the authoritative double-entry platform engine.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import ssl
import sys
import urllib.request
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
AWS_ALB_HEALTH = "https://momo-dev-alb-5850337.us-east-1.elb.amazonaws.com/health"

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


def _check_aws_health() -> str:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(AWS_ALB_HEALTH, method="GET")
        with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
            if resp.status == 200:
                return "ok"
    except Exception:
        pass
    return "offline"


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in ("", "/demo", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
            return
        if path == "/health":
            self._json({"status": _check_aws_health()})
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
