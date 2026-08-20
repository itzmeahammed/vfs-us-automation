"""Tests for outbound webhooks (Phase 4).

Delivery is exercised against a REAL local HTTP server rather than a mocked
urlopen: the signature has to be verifiable from the raw bytes that actually
arrive on the wire, and a mock would happily agree with a broken implementation.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils import webhook  # noqa: E402

SECRET = "test-webhook-secret-key-0123456789abcdef"


class _Handler(BaseHTTPRequestHandler):
    """Records what it receives; replies with the scripted status code."""

    received: List[dict] = []
    status_sequence: List[int] = []

    def do_POST(self) -> None:                      # noqa: N802 — stdlib API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # HTTP header names are case-insensitive, and urllib normalises them
        # to title case on the wire ('X-Vfs-Signature'). Lower-case the keys so
        # lookups here behave like a real web framework's header map.
        type(self).received.append({
            "body": body,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        status = (type(self).status_sequence.pop(0)
                  if type(self).status_sequence else 200)
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args) -> None:           # noqa: A003 — silence stdlib logging
        pass


@pytest.fixture()
def server():
    """A throwaway HTTP server on a free port."""
    _Handler.received = []
    _Handler.status_sequence = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_port}/webhooks/vfs"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Keep the retry tests quick — the schedule itself is not under test."""
    monkeypatch.setattr(webhook, "_RETRY_DELAYS", (0.01, 0.01, 0.01))


@pytest.fixture(autouse=True)
def _isolated_deadletter(tmp_path, monkeypatch):
    """Never append to the repo's real dead-letter file during tests."""
    monkeypatch.setattr(webhook, "DEADLETTER_PATH",
                        str(tmp_path / "deadletter.jsonl"))


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def test_signature_round_trips():
    body = b'{"event":"test"}'
    signature = webhook.sign(body, SECRET)
    assert signature.startswith("sha256=")
    assert webhook.verify_signature(body, signature, SECRET) is True


def test_signature_rejects_a_tampered_body():
    """The whole point: a modified body must not verify."""
    signature = webhook.sign(b'{"amount":1}', SECRET)
    assert webhook.verify_signature(b'{"amount":999}', signature, SECRET) is False


def test_signature_rejects_the_wrong_secret():
    body = b'{"event":"test"}'
    signature = webhook.sign(body, SECRET)
    assert webhook.verify_signature(body, signature, "not-the-secret") is False


def test_signature_rejects_empty_input():
    assert webhook.verify_signature(b"{}", "", SECRET) is False
    assert webhook.verify_signature(b"{}", "sha256=abc", "") is False


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def test_delivers_and_signs_over_the_wire(server):
    """End to end: the receiver can verify the signature it actually got."""
    result = webhook.send("test.ping", {"hello": "world"},
                          url=server.url, secret=SECRET)
    assert result.delivered is True
    assert result.status_code == 200
    assert len(_Handler.received) == 1

    got = _Handler.received[0]
    signature = got["headers"][webhook.SIGNATURE_HEADER.lower()]
    assert webhook.verify_signature(got["body"], signature, SECRET) is True, (
        "the receiving app could not verify the signature we sent")


def test_payload_envelope_shape(server):
    webhook.send("test.ping", {"hello": "world"}, url=server.url, secret=SECRET)
    payload = json.loads(_Handler.received[0]["body"])
    assert payload["version"] == webhook.PAYLOAD_VERSION
    assert payload["event"] == "test.ping"
    assert payload["data"] == {"hello": "world"}
    assert isinstance(payload["sequence"], int)
    assert payload["sent_at"]


def test_event_header_matches_the_payload(server):
    """The app should be able to branch on the header without parsing."""
    webhook.send(webhook.EVENT_REGISTRATION_SUCCEEDED, {},
                 url=server.url, secret=SECRET)
    headers = _Handler.received[0]["headers"]
    assert headers[webhook.EVENT_HEADER.lower()] == webhook.EVENT_REGISTRATION_SUCCEEDED


def test_sequence_increases(server):
    """Ordering: the app must be able to spot out-of-order deliveries."""
    webhook.send("test.ping", {}, url=server.url, secret=SECRET)
    webhook.send("test.ping", {}, url=server.url, secret=SECRET)
    first = json.loads(_Handler.received[0]["body"])["sequence"]
    second = json.loads(_Handler.received[1]["body"])["sequence"]
    assert second > first


def test_skipped_when_not_configured():
    """No URL/secret is normal operation, not a failure."""
    result = webhook.send("test.ping", {})
    assert result.skipped is True
    assert result.delivered is False
    assert result.dead_lettered is False


# --------------------------------------------------------------------------
# Retries and dead-lettering
# --------------------------------------------------------------------------


def test_retries_a_500_then_succeeds(server):
    """A transient server error must not lose the event."""
    _Handler.status_sequence = [500, 200]
    result = webhook.send("test.ping", {}, url=server.url, secret=SECRET)
    assert result.delivered is True
    assert result.attempts == 2
    assert len(_Handler.received) == 2


def test_gives_up_and_dead_letters(server):
    """Exhausted retries must land in the dead-letter log, never be dropped."""
    _Handler.status_sequence = [500, 500, 500, 500]
    result = webhook.send("test.ping", {"important": "data"},
                          url=server.url, secret=SECRET)
    assert result.delivered is False
    assert result.dead_lettered is True

    assert webhook.deadletter_count() == 1
    with open(webhook.DEADLETTER_PATH, encoding="utf-8") as fh:
        row = json.loads(fh.readline())
    assert row["payload"]["data"] == {"important": "data"}
    assert row["error"]


def test_4xx_is_not_retried(server):
    """A request the app actively refused will not succeed on retry."""
    _Handler.status_sequence = [400, 200]
    result = webhook.send("test.ping", {}, url=server.url, secret=SECRET)
    assert result.delivered is False
    assert len(_Handler.received) == 1, "a permanent rejection must not be retried"
    assert result.dead_lettered is True


def test_429_is_retried(server):
    """429 explicitly invites a retry, unlike other 4xx."""
    _Handler.status_sequence = [429, 200]
    result = webhook.send("test.ping", {}, url=server.url, secret=SECRET)
    assert result.delivered is True
    assert len(_Handler.received) == 2


def test_unreachable_host_is_dead_lettered():
    """A connection failure must not raise into the caller."""
    result = webhook.send("test.ping", {},
                          url="http://127.0.0.1:1/nope", secret=SECRET,
                          timeout=1)
    assert result.delivered is False
    assert result.dead_lettered is True
    assert webhook.deadletter_count() == 1


# --------------------------------------------------------------------------
# Event routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,expected_event", [
    ("success", webhook.EVENT_REGISTRATION_SUCCEEDED),
    ("failed", webhook.EVENT_REGISTRATION_FAILED),
    ("skipped", webhook.EVENT_REGISTRATION_FAILED),
    ("dry_run", webhook.EVENT_REGISTRATION_FAILED),
    ("pending", webhook.EVENT_REGISTRATION_NEEDS_ATTENTION),
    ("unknown", webhook.EVENT_REGISTRATION_NEEDS_ATTENTION),
])
def test_registration_status_routes_to_the_right_event(monkeypatch, status,
                                                       expected_event):
    """pending/unknown must NOT look like an ordinary success or failure."""
    captured = {}
    monkeypatch.setattr(
        webhook, "send",
        lambda event, data, **kw: captured.update(event=event)
        or webhook.DeliveryResult(delivered=True, event=event))

    webhook.notify_registration({"status": status})
    assert captured["event"] == expected_event


def test_notify_registration_picks_the_event(monkeypatch):
    """Verify the routing logic directly, without a server."""
    captured = {}

    def fake_send(event, data, **kwargs):
        captured["event"] = event
        return webhook.DeliveryResult(delivered=True, event=event)

    monkeypatch.setattr(webhook, "send", fake_send)

    webhook.notify_registration({"status": "success"})
    assert captured["event"] == webhook.EVENT_REGISTRATION_SUCCEEDED

    webhook.notify_registration({"status": "unknown"})
    assert captured["event"] == webhook.EVENT_REGISTRATION_NEEDS_ATTENTION

    webhook.notify_registration({"status": "pending"})
    assert captured["event"] == webhook.EVENT_REGISTRATION_NEEDS_ATTENTION

    webhook.notify_registration({"status": "failed"})
    assert captured["event"] == webhook.EVENT_REGISTRATION_FAILED


# --------------------------------------------------------------------------
# The run must survive a broken webhook
# --------------------------------------------------------------------------


def test_notify_registered_survives_a_dead_endpoint(monkeypatch):
    """A down callback must never fail a registration that already happened."""
    from src.waitlist import notify
    from src.waitlist.result import Status, WaitlistResult

    monkeypatch.setattr(webhook, "is_configured", lambda: True)
    monkeypatch.setattr(
        webhook, "notify_registration",
        lambda data: (_ for _ in ()).throw(RuntimeError("endpoint exploded")))

    result = WaitlistResult(route="AE-CHE", combo="Dubai - SCHENGEN",
                            registrant_id="x", status=Status.SUCCESS)
    # Must not raise.
    notify.notify_registered(result)


# --------------------------------------------------------------------------
# PII must not leave the machine
# --------------------------------------------------------------------------


def test_pii_is_scrubbed_before_sending(server):
    """A passport number in a reason string must never reach the web app.

    This is the case redaction exists for: the payload leaves the machine, so
    the logging filter cannot help. Verified against what actually arrives on
    the wire, not against the pre-send dict.
    """
    from src.waitlist import redaction

    passport = "Z9998887"
    email = "leaky.client@example.com"
    redaction.add_values([passport, email])
    try:
        webhook.send(
            webhook.EVENT_REGISTRATION_SUCCEEDED,
            {
                "registrant_id": "someone",
                "vfs_reference": "WL-12345",
                "reason": f"registered {passport} for {email}",
            },
            url=server.url, secret=SECRET,
        )
    finally:
        redaction.clear()

    delivered = _Handler.received[0]["body"].decode("utf-8")
    assert passport not in delivered, "a passport number left the machine"
    assert email not in delivered, "a client email left the machine"
    # The genuinely useful field must survive scrubbing.
    assert "WL-12345" in delivered


def test_scrubbing_preserves_the_envelope(server):
    """Redaction must not break the JSON structure the app parses."""
    from src.waitlist import redaction

    redaction.add_values(["SECRET-VALUE"])
    try:
        webhook.send("test.ping", {"note": "contains SECRET-VALUE here"},
                     url=server.url, secret=SECRET)
    finally:
        redaction.clear()

    payload = json.loads(_Handler.received[0]["body"])
    assert payload["event"] == "test.ping"
    assert "SECRET-VALUE" not in json.dumps(payload)
    assert isinstance(payload["sequence"], int)
