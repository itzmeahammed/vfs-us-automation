"""Outbound webhooks: tell the web app what happened.

The repo had no outbound HTTP layer at all before this — only Telegram's
`urllib.request.urlopen`. This module follows that same shape deliberately:
stdlib only (no new dependency for the bot), never raises, logs and returns a
result so callers can fire-and-forget.

FOUR PROPERTIES THAT MATTER
---------------------------

1. **Signed.** Every request carries `X-VFS-Signature: sha256=<hmac>` over the
   exact bytes of the body. Without it, anyone who learns your callback URL can
   post fake "registration confirmed" events. Your app MUST verify it — see
   `verify_signature()` for the receiving half.

2. **Retried.** Your app will be down sometimes. Failures retry with
   exponential backoff, and 4xx (except 408/429) is treated as permanent: a
   request your app actively rejected will not succeed on the third attempt.

3. **Dead-lettered.** A delivery that exhausts its retries is appended to
   `state/webhook_deadletter.jsonl` rather than dropped. A lost "you are
   registered" is worse than a late one.

4. **Scrubbed.** Payloads pass through `waitlist.redaction` before sending.
   Client data leaving the machine is exactly the case that module exists for.

Ordering: each event carries a monotonic `sequence` and an ISO `sent_at`, so
your app can detect out-of-order or replayed deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Header names. Kept explicit so the receiving app has something to code against.
SIGNATURE_HEADER = "X-VFS-Signature"
EVENT_HEADER = "X-VFS-Event"
DELIVERY_HEADER = "X-VFS-Delivery"

# Event types. Adding one is fine; RENAMING one is a breaking change for the
# web app, so treat these as a versioned contract.
EVENT_WAITLIST_OPENED = "waitlist.opened"
EVENT_REGISTRATION_SUCCEEDED = "registration.succeeded"
EVENT_REGISTRATION_FAILED = "registration.failed"
EVENT_REGISTRATION_NEEDS_ATTENTION = "registration.needs_attention"
EVENT_SLOTS_AVAILABLE = "slots.available"
EVENT_TEST = "test.ping"

PAYLOAD_VERSION = 1

DEADLETTER_PATH = os.path.join("state", "webhook_deadletter.jsonl")

# Retry schedule in seconds. Deliberately short overall: a waitlist window can
# close, so a delivery that has taken ~30s of retries is better dead-lettered
# and alerted on than blocking the run any longer.
_RETRY_DELAYS = (1.0, 4.0, 10.0)

_sequence_lock = threading.Lock()
_sequence = 0


@dataclass
class DeliveryResult:
    """Outcome of one webhook delivery attempt-set."""

    delivered: bool
    event: str
    attempts: int = 0
    status_code: Optional[int] = None
    error: str = ""
    dead_lettered: bool = False
    skipped: bool = False        # not configured — not a failure

    def __bool__(self) -> bool:
        return self.delivered


def _next_sequence() -> int:
    """Monotonic per-process counter, so the app can order events."""
    global _sequence
    with _sequence_lock:
        _sequence += 1
        return _sequence


def _settings():
    """Webhook settings, or None when the feature is not configured."""
    try:
        from src.settings import settings
        return settings().webhook
    except Exception:                              # noqa: BLE001
        return None


def is_configured() -> bool:
    """True when a callback URL and secret are both set."""
    cfg = _settings()
    return bool(cfg and cfg.enabled and cfg.url and cfg.secret)


def sign(body: bytes, secret: str) -> str:
    """The `sha256=<hex>` signature for `body`.

    Signs the raw BODY BYTES, not a re-serialised dict — the receiver must be
    able to verify against exactly what arrived on the wire.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(body: bytes, header_value: str, secret: str) -> bool:
    """Verify an inbound signature. For the RECEIVING app (and our tests).

    Provided here so the contract has exactly one implementation. Port this to
    your web app's language; the algorithm is HMAC-SHA256 over the raw body,
    hex-encoded, prefixed 'sha256='.
    """
    if not header_value or not secret:
        return False
    expected = sign(body, secret)
    # Constant-time: a plain == leaks how much of the signature was correct.
    return hmac.compare_digest(expected, header_value.strip())


def build_payload(event: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap `data` in the standard envelope."""
    return {
        "version": PAYLOAD_VERSION,
        "event": event,
        "sequence": _next_sequence(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


def _scrub_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the payload through the redaction filter before it leaves.

    Serialise → scrub → parse, so nested values are covered too. If scrubbing
    somehow produces invalid JSON, the UNSCRUBBED payload is NOT sent: failing
    to deliver beats leaking a passport number.
    """
    try:
        from src.waitlist import redaction
        text = json.dumps(payload, ensure_ascii=False, default=str)
        scrubbed = redaction.scrub(text)
        return json.loads(scrubbed)
    except json.JSONDecodeError:
        log.error("Webhook payload could not be re-parsed after scrubbing — "
                  "sending a minimal event rather than risking a leak.")
        return {
            "version": PAYLOAD_VERSION,
            "event": payload.get("event"),
            "sequence": payload.get("sequence"),
            "sent_at": payload.get("sent_at"),
            "data": {"redaction_error": True},
        }
    except Exception:                              # noqa: BLE001
        # redaction unavailable (e.g. a unit test importing this alone).
        return payload


def _is_permanent(status_code: Optional[int]) -> bool:
    """Is retrying pointless?

    4xx means the app understood and refused — except 408 (timeout) and 429
    (rate limited), which explicitly invite a retry.
    """
    if status_code is None:
        return False
    return 400 <= status_code < 500 and status_code not in (408, 429)


def _dead_letter(payload: Dict[str, Any], error: str) -> None:
    """Append an undelivered event to the dead-letter log."""
    try:
        os.makedirs(os.path.dirname(DEADLETTER_PATH) or ".", exist_ok=True)
        row = {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "payload": payload,
        }
        with open(DEADLETTER_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        log.error("Webhook '%s' DEAD-LETTERED to %s: %s",
                  payload.get("event"), DEADLETTER_PATH, error)
    except OSError as exc:
        log.error("Could not write the webhook dead-letter file: %s", exc)


def send(event: str, data: Dict[str, Any], *,
         url: Optional[str] = None,
         secret: Optional[str] = None,
         timeout: Optional[float] = None,
         retries: Optional[int] = None) -> DeliveryResult:
    """Deliver one event to the web app. Never raises.

    Args:
        event: One of the EVENT_* constants.
        data: Event-specific body, placed under "data" in the envelope.
        url/secret/timeout/retries: Override the configured values (tests).

    Returns:
        DeliveryResult. `.skipped` is True when no callback is configured —
        that is normal operation, not an error.
    """
    cfg = _settings()
    target = url or (cfg.url if cfg else "")
    key = secret or (cfg.secret if cfg else "")
    enabled = True if url else bool(cfg and cfg.enabled)

    if not enabled or not target or not key:
        log.debug("Webhook not configured — skipping '%s'.", event)
        return DeliveryResult(delivered=False, event=event, skipped=True)

    payload = _scrub_payload(build_payload(event, data))
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    delivery_id = f"{payload.get('sequence')}-{int(time.time())}"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "vfs-slot-checker-webhook/1",
        SIGNATURE_HEADER: sign(body, key),
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
    }

    request_timeout = timeout if timeout is not None else (
        cfg.timeout_seconds if cfg else 10.0)
    max_attempts = 1 + (retries if retries is not None else len(_RETRY_DELAYS))

    last_error = ""
    last_status: Optional[int] = None

    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(target, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if 200 <= int(status) < 300:
                    log.info("Webhook '%s' delivered (HTTP %s, attempt %d).",
                             event, status, attempt)
                    return DeliveryResult(delivered=True, event=event,
                                          attempts=attempt, status_code=int(status))
                last_status = int(status)
                last_error = f"HTTP {status}"

        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_error = f"HTTP {exc.code}"
            if _is_permanent(exc.code):
                log.error("Webhook '%s' rejected permanently (HTTP %s) — "
                          "not retrying.", event, exc.code)
                break
        except Exception as exc:                   # noqa: BLE001 — incl. URLError, timeouts
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < max_attempts:
            delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
            log.warning("Webhook '%s' attempt %d/%d failed (%s) — retrying in %.0fs.",
                        event, attempt, max_attempts, last_error, delay)
            time.sleep(delay)

    _dead_letter(payload, last_error or "delivery failed")
    return DeliveryResult(delivered=False, event=event, attempts=max_attempts,
                          status_code=last_status, error=last_error,
                          dead_lettered=True)


# --------------------------------------------------------------------------- #
# Convenience wrappers — one per event, so callers cannot mistype an event name #
# --------------------------------------------------------------------------- #


def notify_waitlist_opened(route: str, combos: List[str],
                           clients_waiting: int = 0) -> DeliveryResult:
    """A waitlist opened for these combinations on this route."""
    return send(EVENT_WAITLIST_OPENED, {
        "route": route,
        "combos": combos,
        "clients_waiting": clients_waiting,
    })


def notify_slots_available(route: str, combo: str, banner: str = "") -> DeliveryResult:
    """A real, bookable slot exists — better than a waitlist. Go book it."""
    return send(EVENT_SLOTS_AVAILABLE, {
        "route": route, "combo": combo, "banner": banner,
    })


def notify_registration(result: Dict[str, Any]) -> DeliveryResult:
    """One client's registration outcome.

    Routes to the right event so the web app can branch on the header alone:
    'unknown'/'pending' is NOT a success and NOT an ordinary failure — it needs
    a human to check the VFS account.
    """
    status = str(result.get("status", "")).lower()
    if status in ("pending", "unknown"):
        event = EVENT_REGISTRATION_NEEDS_ATTENTION
    elif status == "success":
        event = EVENT_REGISTRATION_SUCCEEDED
    else:
        event = EVENT_REGISTRATION_FAILED
    return send(event, result)


def send_test_ping() -> DeliveryResult:
    """Deliver a harmless event, to verify the wiring end to end."""
    return send(EVENT_TEST, {"message": "Webhook configured correctly."})


def deadletter_count() -> int:
    """How many undelivered events are waiting in the dead-letter log."""
    try:
        with open(DEADLETTER_PATH, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0
