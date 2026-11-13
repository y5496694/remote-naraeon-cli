"""Authenticated, loopback-only multi-tablet monitor; Python stdlib only."""
import hmac
import json
import math
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fleet import Busy, FleetError

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 32 * 1024


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, fleet, port=0, interval=2):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise FleetError("관제 포트는 0~65535입니다.")
        if not math.isfinite(interval) or not 1 <= interval <= 30:
            raise FleetError("화면 갱신 간격은 1~30초입니다.")
        self.fleet, self.interval = fleet, interval
        self.nonce = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_address[1])

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    @property
    def url(self):
        # A short-lived local capability, NOT a remote MCP endpoint or token.
        return self.origin + "/#" + self.nonce

    def server_close(self):
        self.fleet.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass  # Do not log local session capabilities or screen contents.

    def reply(self, status, data, mime="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        try:
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            pass

    def permitted(self, private=False):
        expected = urlsplit(self.server.origin).netloc
        if self.headers.get("Host") != expected:
            self.reply(403, {"ok": False, "error": "허용하지 않은 Host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.reply(403, {"ok": False, "error": "다른 사이트의 요청은 허용하지 않습니다."})
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self.reply(403, {"ok": False, "error": "관제창 안에서 실행하세요."})
            return False
        if private and not hmac.compare_digest(self.headers.get("X-Narae-Session", "").encode("utf-8"), self.server.nonce.encode("ascii")):
            self.reply(403, {"ok": False, "error": "이 관제 세션의 인증이 필요합니다."})
            return False
        return True

    def do_GET(self):
        if not self.permitted():
            return
        route = self.path
        if route == "/api/devices":
            if self.permitted(private=True):
                self.reply(200, {"devices": list(self.server.fleet.devices), "interval": self.server.interval,
                                 "transport": "periodic-screenshots", "max_devices": 12})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if route not in assets:
            self.reply(404, {"ok": False})
            return
        name, mime = assets[route]
        self.reply(200, (WEB / name).read_bytes(), mime)

    def do_POST(self):
        if not self.permitted(private=True):
            return
        if self.path != "/api/device":
            self.reply(404, {"ok": False})
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise ValueError("JSON 요청이 필요합니다.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("요청 용량이 올바르지 않습니다.")
            body = json.loads(self.rfile.read(length))
            if (not isinstance(body, dict) or set(body) - {"device", "operation", "arguments"}
                    or not isinstance(body.get("device"), str) or not isinstance(body.get("operation"), str)
                    or not isinstance(body.get("arguments", {}), dict)):
                raise ValueError("잘못된 관제 요청입니다.")
            result = self.server.fleet.run(body["device"], body["operation"], body.get("arguments", {}))
            self.reply(200, {"ok": True, "result": result})
        except Busy as exc:
            self.reply(409, {"ok": False, "busy": True, "error": str(exc)})
        except (FleetError, ValueError) as exc:
            self.reply(400, {"ok": False, "error": str(exc)})
        except Exception:
            # Remote transport/OS details could contain profile secrets.
            self.reply(502, {"ok": False, "error": "기기 연결 또는 요청에 실패했습니다. 결과가 불명확한 조작은 반복하지 말고 상태를 확인하세요."})


def serve(fleet, port=0, interval=2, open_browser=True):
    with MonitorServer(fleet, port, interval) as server:
        print(json.dumps({"monitor_url": server.url, "devices": list(fleet.devices),
                          "local_only": True, "transport": "periodic-screenshots",
                          "interval_seconds": interval, "stop": "Ctrl+C"}, ensure_ascii=False), flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
