"""Mounting the assistant on a stdlib `http.server` app.

    from ai_assistant import AssistantHTTP
    ai = AssistantHTTP(assistant)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if ai.handle(self): return
            ...
        def do_POST(self):
            if ai.handle(self): return
            ...

and put `ai.script_tag()` in any page that should show the panel. The server
must be threaded (ThreadingHTTPServer / ThreadingTCPServer): an answer takes
seconds, and a single-threaded server would freeze every other page meanwhile.

Routes, under `config.route_prefix` (default /assistant):
    GET  /assistant/assistant.js, /assistant/assistant.css   the widget
    GET  /assistant/meta     title, suggestions, model, today's token use
    POST /assistant/ask      {"question": str, "history": [...]}  -> NDJSON stream

Other frameworks: call `Assistant.ask()` yourself and stream its events; the
wire format is one JSON object per line.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .engine import Assistant
from .guard import check_request

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
_MAX_BODY = 64 * 1024

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class AssistantHTTP:
    def __init__(self, assistant: Assistant, *, labels: Optional[dict] = None):
        self.assistant = assistant
        self.config = assistant.config
        self.prefix = self.config.route_prefix
        self.labels = labels or {}
        self._static = {}
        for name in ("assistant.js", "assistant.css"):
            with open(os.path.join(_STATIC, name), "rb") as f:
                self._static[name] = f.read()

    def script_tag(self) -> str:
        p = self.prefix
        return (f'<script src="{p}/assistant.js" data-endpoint="{p}" defer></script>')

    # ---- plumbing ----------------------------------------------------------
    def _send(self, h, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in {**_SECURITY_HEADERS, **(extra or {})}.items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _json(self, h, status: int, obj: dict):
        self._send(h, status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def handle(self, h) -> bool:
        """Serve the request if it belongs to the assistant; True when handled."""
        path = h.path.split("?", 1)[0]
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            return False
        sub = path[len(self.prefix):].lstrip("/")
        method = h.command

        refusal = check_request(method, h.headers, h.client_address[0],
                                allowed_hosts=self.config.allowed_hosts,
                                loopback_only=self.config.loopback_only)
        if refusal:
            self._drain(h)
            self._json(h, 403, {"error": refusal})
            return True

        if method == "GET" and sub in self._static:
            ext = os.path.splitext(sub)[1]
            self._send(h, 200, self._static[sub], _TYPES[ext])
            return True
        if method == "GET" and sub == "meta":
            st = self.assistant.status()
            self._json(h, 200, {"title": self.config.title,
                                "suggestions": list(self.config.suggestions),
                                "model": st["model"], "usage_today": st["usage_today"]})
            return True
        if method == "POST" and sub == "ask":
            self._ask(h)
            return True
        self._drain(h)
        self._json(h, 404, {"error": "not found"})
        return True

    @staticmethod
    def _drain(h):
        """Read a small unread body before answering early. Closing a socket with
        unread bytes makes Windows send RST, and the client then sees a network
        error instead of our status. Oversized bodies are not read — the
        connection is closed instead."""
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= _MAX_BODY:
            h.rfile.read(n)
        elif n > _MAX_BODY:
            h.close_connection = True

    def _ask(self, h):
        try:
            length = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > _MAX_BODY:
            h.close_connection = True
            self._json(h, 413 if length > _MAX_BODY else 400, {"error": "bad body size"})
            return
        try:
            body = json.loads(h.rfile.read(length).decode("utf-8"))
            question = body.get("question")
            history = body.get("history") or []
            if not isinstance(question, str) or not isinstance(history, list):
                raise ValueError
        except (ValueError, UnicodeDecodeError, AttributeError):
            self._json(h, 400, {"error": "expected {question: str, history: list}"})
            return

        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        for k, v in _SECURITY_HEADERS.items():
            h.send_header(k, v)
        h.end_headers()
        h.close_connection = True
        events = self.assistant.ask(question, history, labels=self.labels)
        try:
            for ev in events:
                h.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                         # browser hit Stop / closed the tab
        finally:
            events.close()               # stops the model call and frees the slot
