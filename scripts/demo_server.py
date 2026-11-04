#!/usr/bin/env python3
"""Interactive Demo Interface server for the Mobile Money Platform presentation.

Serves the public landing page and role-specific static UI previews from `demo/` at
http://127.0.0.1:5050, and exposes live JSON endpoints (`/health` and `/api/status`).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import ssl
import urllib.request
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
AWS_ALB_HEALTH = "https://momo-dev-alb-5850337.us-east-1.elb.amazonaws.com/health"


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
        if parsed.path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
            return
        if parsed.path == "/health":
            self._json({"status": _check_aws_health()})
            return
        if parsed.path == "/api/status":
            proof_file = ROOT / "evidence-out" / "proof-results.json"
            proofs = json.loads(proof_file.read_text(encoding="utf-8")) if proof_file.exists() else {}
            self._json({"status": "ok", "proofs": proofs, "costs": _load_cost_data()})
            return
        super().do_GET()

    def _json(self, data: dict, code: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # pyrefly: ignore[bad-override-param-name]
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
