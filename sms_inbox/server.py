"""Tiny SMS console for Relayion: serves index.html and proxies /api/v1/* to Relayion.

Relayion's API sends no Access-Control-Allow-Origin header, so a browser page
cannot call it directly. This proxy also keeps the API key off the page.

Usage:
    set RELAYION_API_KEY=rlyn_xxx        (PowerShell: $env:RELAYION_API_KEY="rlyn_xxx")
    python sms_inbox/server.py           then open http://localhost:8765
"""
import os
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_BASE = "https://api.relayion.com"
API_KEY = os.environ.get("RELAYION_API_KEY", "")
PORT = int(os.environ.get("PORT", "8765"))
HERE = Path(__file__).resolve().parent

# The Relayion endpoints the page may reach, per method. Nothing else is proxied.
GET_PREFIXES = ("/api/v1/inbound", "/api/v1/outbound", "/api/v1/conversations/", "/api/v1/devices")
POST_PATHS = ("/api/v1/outbound",)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        if self.path.startswith(GET_PREFIXES):
            return self.proxy("GET")
        if self.path.startswith("/api/"):
            return self.reply(404, b'{"success":false,"error":{"message":"Not proxied"}}')
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] in POST_PATHS:
            return self.proxy("POST", self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        return self.reply(404, b'{"success":false,"error":{"message":"Not proxied"}}')

    def proxy(self, method, body=None):
        if not API_KEY:
            return self.reply(500, b'{"success":false,"error":{"message":"RELAYION_API_KEY is not set on the server"}}')
        headers = {"Authorization": f"Bearer {API_KEY}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API_BASE + self.path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                self.reply(r.status, r.read())
        except urllib.error.HTTPError as e:
            self.reply(e.code, e.read())
        except urllib.error.URLError as e:
            self.reply(502, f'{{"success":false,"error":{{"message":"Upstream error: {e.reason}"}}}}'.encode())

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not API_KEY:
        print("warning: RELAYION_API_KEY is not set; API calls will fail")
    print(f"SMS console on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
