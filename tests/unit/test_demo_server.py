import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.demo_server import DemoHandler


class BackendHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length).decode("utf-8") if length else ""
        response = json.dumps({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "body": payload,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("X-Correlation-ID", "test-correlation")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt, *args):
        return


def start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_proxy_forwards_authenticated_get_and_query(monkeypatch):
    backend, backend_url = start_server(BackendHandler)
    frontend, frontend_url = start_server(DemoHandler)
    monkeypatch.setenv("MOMO_API_BASE_URL", backend_url)
    try:
        request = Request(
            f"{frontend_url}/auth/me?source=portal",
            headers={"Authorization": "Bearer test-access-token"},
        )
        with urlopen(request, timeout=3) as response:
            data = json.loads(response.read())
            assert response.headers["X-Correlation-ID"] == "test-correlation"
        assert data == {
            "method": "GET",
            "path": "/auth/me?source=portal",
            "authorization": "Bearer test-access-token",
            "idempotency_key": None,
            "body": "",
        }
    finally:
        frontend.shutdown()
        frontend.server_close()
        backend.shutdown()
        backend.server_close()


def test_proxy_forwards_payment_body_and_idempotency_key(monkeypatch):
    backend, backend_url = start_server(BackendHandler)
    frontend, frontend_url = start_server(DemoHandler)
    monkeypatch.setenv("MOMO_API_BASE_URL", backend_url)
    try:
        payload = json.dumps({"from_wallet": "payer", "to_wallet": "payee", "amount": 500})
        request = Request(
            f"{frontend_url}/transfers",
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-access-token",
                "Idempotency-Key": "proxy-test-key",
            },
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            data = json.loads(response.read())
        assert data == {
            "method": "POST",
            "path": "/transfers",
            "authorization": "Bearer test-access-token",
            "idempotency_key": "proxy-test-key",
            "body": payload,
        }
    finally:
        frontend.shutdown()
        frontend.server_close()
        backend.shutdown()
        backend.server_close()


def test_proxy_returns_explicit_error_when_backend_is_unavailable(monkeypatch):
    frontend, frontend_url = start_server(DemoHandler)
    monkeypatch.setenv("MOMO_API_BASE_URL", "http://127.0.0.1:1")
    try:
        try:
            urlopen(f"{frontend_url}/health", timeout=3)
        except HTTPError as error:
            assert error.code == 502
            assert json.loads(error.read())["error"]["message"].startswith("Backend API is unavailable")
        else:
            raise AssertionError("Expected a 502 when the backend cannot be reached")
    finally:
        frontend.shutdown()
        frontend.server_close()


def test_frontend_and_simulation_routes_are_not_proxied():
    assert not DemoHandler._is_api_request("GET", "/login/admin")
    assert not DemoHandler._is_api_request("GET", "/admin/users")
    assert not DemoHandler._is_api_request("POST", "/api/transactions/submit")
    assert DemoHandler._is_api_request("GET", "/admin/integrity")
    assert DemoHandler._is_api_request("POST", "/transactions/txn-id/reverse")
