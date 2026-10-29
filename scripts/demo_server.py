#!/usr/bin/env python3
"""Interactive Demo Interface server for the Mobile Money Platform presentation.

Serves the full visual Demo UI (`demo/index.html`) at http://127.0.0.1:5050 and exposes
live JSON endpoints backed by `scripts/cost_model.py`, `evidence-out/proof-results.json`,
and the `momo` domain modules (FX quotes, recurring payments, fraud rules, SHA-256 hashing).
"""
from __future__ import annotations

import json
import pathlib
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/demo", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if parsed.path == "/api/status":
            proof_file = ROOT / "evidence-out" / "proof-results.json"
            proofs = json.loads(proof_file.read_text()) if proof_file.exists() else {}
            import cost_model
            costs = {k: cost_model.model(**v) for k, v in cost_model.SCENARIOS.items()}
            return self._json({"status": "ok", "proofs": proofs, "costs": costs})
        return super().do_GET()

    def _json(self, data: dict, code: int = 200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass


def main():
    port = 5050
    url = f"http://127.0.0.1:{port}"
    server = HTTPServer(("127.0.0.1", port), DemoHandler)
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


if __name__ == "__main__":
    main()
