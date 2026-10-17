"""OpenAI Chat Completions over the standard library — streaming, with tools.

No SDK dependency on purpose: the package must drop into a project that does
not have `openai` installed (this one doesn't), and the surface used here — one
POST, server-sent events back — is small and stable.

`ChatClient.stream()` yields events:
    ("text", str)              a piece of the answer
    ("tool_calls", [ {id, name, arguments} ])   once, when the model wants tools
    ("usage", {prompt_tokens, completion_tokens, total_tokens})
    ("finish", reason)

Retries: connection failures, 408/409/429 and 5xx are retried with exponential
backoff and jitter (honouring Retry-After) — but ONLY before the first byte of
a response was consumed, so a retry can never duplicate streamed text.
"""
from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Iterator, List, Optional, Tuple

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _friendly(status: int, body: str) -> str:
    try:
        err = json.loads(body).get("error") or {}
        detail = err.get("message") or ""
        code = err.get("code") or err.get("type") or ""
    except (ValueError, AttributeError):
        detail, code = body[:200], ""
    if status == 401:
        return "OpenAI rejected the API key (401). Check the key in the config."
    if status == 403:
        return f"OpenAI refused the request (403): {detail[:200]}"
    if status == 404:
        return f"Model or endpoint not found (404): {detail[:200]}"
    if status == 429 and code == "insufficient_quota":
        return "The OpenAI account is out of credit (insufficient_quota)."
    if status == 429:
        return "OpenAI rate limit hit (429). Try again in a minute."
    return f"OpenAI error {status}: {detail[:200] or code}"


class ChatClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1",
                 timeout_s: float = 60.0, max_retries: int = 3, user_agent: str = "ai-assistant/1"):
        self._key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.user_agent = user_agent

    def __repr__(self):
        return f"ChatClient(model={self.model!r}, url={self.url!r})"

    # ---- transport ---------------------------------------------------------
    def _open(self, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        attempt = 0
        while True:
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": self.user_agent,
            })
            try:
                return urllib.request.urlopen(req, timeout=self.timeout_s)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                retryable = e.code in _RETRY_STATUS and "insufficient_quota" not in body
                if not retryable or attempt >= self.max_retries:
                    raise LLMError(_friendly(e.code, body), e.code, retryable) from None
                wait = self._retry_after(e) or min(20.0, 1.5 * 2 ** attempt)
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
                if attempt >= self.max_retries:
                    reason = getattr(e, "reason", e)
                    raise LLMError(f"Could not reach OpenAI: {type(reason).__name__}",
                                   None, True) from None
                wait = min(20.0, 1.5 * 2 ** attempt)
            time.sleep(wait * (0.8 + 0.4 * random.random()))
            attempt += 1

    @staticmethod
    def _retry_after(e: urllib.error.HTTPError) -> Optional[float]:
        try:
            v = float(e.headers.get("Retry-After", ""))
            return min(v, 30.0) if v > 0 else None
        except (TypeError, ValueError):
            return None

    # ---- the call ----------------------------------------------------------
    def stream(self, messages: List[dict], *, tools: Optional[List[dict]] = None,
               tool_choice: Optional[str] = None, temperature: float = 0.2,
               max_tokens: int = 1500) -> Iterator[Tuple[str, object]]:
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True},
                   "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = True
            if tool_choice:
                payload["tool_choice"] = tool_choice
        resp = self._open(payload)
        calls: dict = {}
        finish = None
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"].get("message", "stream error"))[:200])
                if chunk.get("usage"):
                    yield "usage", chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield "text", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0),
                                                {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except (socket.timeout, TimeoutError):
            raise LLMError("OpenAI stopped responding mid-answer (timeout).", None, True) from None
        finally:
            resp.close()
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]
        yield "finish", finish or "stop"
