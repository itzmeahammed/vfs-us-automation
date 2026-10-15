"""Read inbound SMS from Relayion.

    ════════════════════════════ WHAT RELAYION IS ════════════════════════════

An Android device with a SIM in it, forwarding its SMS to an HTTP API. That
matters for one operational reason worth stating before any code:

    *** THE CODE GOES TO THE PHONE NUMBER REGISTERED ON THE CARD. ***

So the SIM that receives the 3-D Secure SMS has to be the one in the Relayion
device. No amount of correct code here helps if the bank is texting a handset
in somebody's pocket. `python -m src.otp_relay devices` lists what the relay
can actually see, and is the first thing to run.

    ═══════════════════════════════ THE API ═══════════════════════════════════

Confirmed against sms_inbox/server.py and sms_inbox/index.html, which are a
working console against this API rather than a guess at it:

    GET /api/v1/inbound?sortBy=receivedAt&sortOrder=desc&from=<iso>&to=<iso>
                        &senderNumber=<s>
    GET /api/v1/devices
    Authorization: Bearer <key>
    -> {"success": true, "data": [ {id, senderNumber, body, receivedAt,
                                    createdAt, simSlotIndex}, ... ]}
    -> {"success": false, "error": {"message": "...", "code": "..."}}

`urllib` rather than `requests`, matching src/utils/webhook.py and
sms_inbox/server.py — this project does not take an HTTP dependency for one
GET.

    ═══════════════════════════ THE KEY, AND LOGS ═══════════════════════════

The key is read from, in order: an explicit argument, RELAYION_API_KEY (what
sms_inbox/server.py already uses), then `[relayion] relayion_key` in
config/config.local.ini. It is registered with the log redaction filter on
first use, and no function here ever puts a message BODY in a log line — an
SMS body is the code.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.otp_relay.errors import OtpNotConfigured, OtpRelayUnavailable

log = logging.getLogger(__name__)

API_BASE = "https://api.relayion.com"
ENV_KEY = "RELAYION_API_KEY"
CONFIG_SECTION = "relayion"
CONFIG_KEY = "relayion_key"

#: Per-request ceiling. The payment path calls this inside a loop that must
#: stay responsive to the gateway handing back, so a hung relay may not block
#: it for long.
REQUEST_TIMEOUT_S = 15


@dataclass(frozen=True)
class Message:
    """One inbound SMS.

    `body` holds the code. __repr__ is overridden for the same reason
    Card.__repr__ is: the default one prints every field, and the first place
    that bites is an exception traceback being pasted into a chat window.
    """

    id: str
    sender: str
    body: str
    received_at: Optional[datetime] = None
    sim_slot: str = ""

    def __repr__(self) -> str:
        when = self.received_at.isoformat() if self.received_at else "?"
        return (f"<SMS {self.id} from {self.sender} at {when} "
                f"({len(self.body)} chars)>")

    __str__ = __repr__


def _redact(key: str) -> None:
    """Register the API key with the log filter. Fails open, as webhook.py
    does — losing a log line during a live payment is worse than the residual
    risk on a machine that already holds the key."""
    try:
        from src.waitlist import redaction

        redaction.add_values([key])
    except Exception as e:                                      # noqa: BLE001
        log.debug(f"could not install relayion key redaction: {e}")


def api_key(explicit: str = "") -> str:
    """The configured key. Raises OtpNotConfigured when there is none."""
    if explicit and explicit.strip():
        _redact(explicit.strip())
        return explicit.strip()

    from_env = os.environ.get(ENV_KEY, "").strip()
    if from_env:
        _redact(from_env)
        return from_env

    try:
        from src.utils.config_reader import (
            get_config_value,
            initialize_config,
        )

        # REQUIRED, not defensive. get_config_value reads a module-level
        # parser that is EMPTY until something initialises it, so without this
        # the key is invisible to any entry point that has not already called
        # it — and "configured: False" on a machine whose config.local.ini
        # plainly holds the key is a confusing thing to debug mid-payment.
        # initialize_config is idempotent, so a run that already loaded its
        # config keeps exactly the values it started with.
        initialize_config()
        value = (get_config_value(CONFIG_SECTION, CONFIG_KEY, "") or "").strip()
    except Exception as e:                                      # noqa: BLE001
        raise OtpNotConfigured(
            f"Could not read [{CONFIG_SECTION}] {CONFIG_KEY} from the "
            f"config: {e}") from e

    if not value:
        raise OtpNotConfigured(
            f"No Relayion key. Set {ENV_KEY}, or [{CONFIG_SECTION}] "
            f"{CONFIG_KEY} in config/config.local.ini.")
    _redact(value)
    return value


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO-8601 stamp into an AWARE datetime, or None.

    Always aware, always UTC. A naive datetime here would compare unequally
    against the armed anchor and silently reject every message — which on this
    path looks identical to "no SMS arrived" and would be debugged for an hour.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _request(path: str, key: str, timeout_s: int = REQUEST_TIMEOUT_S) -> Any:
    """GET `path` and return the unwrapped `data`. Raises on anything else."""
    req = urllib.request.Request(
        API_BASE + path,
        headers={"Authorization": f"Bearer {key}",
                 "Accept": "application/json"},
        method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # The body usually carries Relayion's own message, which is far more
        # useful than the status code alone ("Invalid API key").
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            detail = str((body.get("error") or {}).get("message") or "")
        except Exception:                                       # noqa: BLE001
            pass
        raise OtpRelayUnavailable(
            f"Relayion returned HTTP {exc.code}"
            f"{': ' + detail if detail else ''}") from exc
    except urllib.error.URLError as exc:
        raise OtpRelayUnavailable(
            f"Could not reach Relayion: {exc.reason}") from exc
    except (ValueError, TypeError) as exc:
        raise OtpRelayUnavailable(
            f"Relayion sent a response that is not JSON: {exc}") from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        message = ""
        if isinstance(payload, dict):
            message = str((payload.get("error") or {}).get("message") or "")
        raise OtpRelayUnavailable(
            f"Relayion refused the request{': ' + message if message else ''}")
    return payload.get("data")


def _message(raw: Dict[str, Any]) -> Message:
    return Message(
        id=str(raw.get("id") or ""),
        sender=str(raw.get("senderNumber") or ""),
        body=str(raw.get("body") or ""),
        received_at=(_parse_time(raw.get("receivedAt"))
                     or _parse_time(raw.get("createdAt"))),
        # `or ""` would blank SIM SLOT 0, which is a real slot and the
        # commonest one — 0 is falsy. Dual-SIM handsets are exactly where
        # knowing which SIM took the code matters.
        sim_slot=("" if raw.get("simSlotIndex") is None
                  else str(raw.get("simSlotIndex"))),
    )


def list_inbound(since: Optional[datetime] = None, sender: str = "",
                 key: str = "", timeout_s: int = REQUEST_TIMEOUT_S
                 ) -> List[Message]:
    """Inbound SMS, newest first.

    `since` is passed to the API as `from` AND re-checked by the caller. The
    server-side filter saves bandwidth; it is not trusted for correctness,
    because whether it is inclusive and what timezone it assumes are not
    things this code can verify, and "accidentally accepted an older message"
    is the one failure mode that matters here.
    """
    params = {"sortBy": "receivedAt", "sortOrder": "desc"}
    if since is not None:
        params["from"] = since.astimezone(timezone.utc).isoformat()
    if sender:
        params["senderNumber"] = sender

    data = _request("/api/v1/inbound?" + urllib.parse.urlencode(params),
                    api_key(key), timeout_s)
    if not isinstance(data, list):
        return []
    return [_message(raw) for raw in data if isinstance(raw, dict)]


def devices(key: str = "", timeout_s: int = REQUEST_TIMEOUT_S
            ) -> List[Dict[str, Any]]:
    """The relay's devices. The diagnostic that answers "is the SIM online"."""
    data = _request("/api/v1/devices", api_key(key), timeout_s)
    return [d for d in (data or []) if isinstance(d, dict)]


def is_online(device: Dict[str, Any]) -> bool:
    """Is this device reachable?

    Relayion reports `"status": "ONLINE"` — A STRING, not a boolean. An
    earlier cut of this read `isOnline`/`online`, got None for both, and
    reported a live paired handset as OFFLINE: a false negative on the one
    check an operator runs to decide whether the relay can be relied on. The
    boolean spellings are kept as fallbacks in case the field is ever added,
    but `status` is what the API actually sends.
    """
    status = str(device.get("status") or "").strip().upper()
    if status:
        return status == "ONLINE"
    flag = device.get("isOnline", device.get("online"))
    return bool(flag)


def sim_numbers(device: Dict[str, Any]) -> List[str]:
    """The phone numbers this device can receive on.

    The whole readiness question in one list: the bank texts the number
    registered on the card, so unless that number is here, no code arrives.
    """
    out: List[str] = []
    for sim in device.get("sims") or []:
        number = str((sim or {}).get("phoneNumber") or "").strip()
        if number and number not in out:
            out.append(number)
    return out


def is_configured() -> bool:
    """True when a key exists. For a readiness check that must not raise."""
    try:
        api_key()
        return True
    except OtpNotConfigured:
        return False


def senders_of(messages: Sequence[Message]) -> List[str]:
    """Distinct senders, for the diagnostic that tells an operator what to put
    in `otp_senders` once they have seen a real message arrive."""
    out: List[str] = []
    for m in messages:
        if m.sender and m.sender not in out:
            out.append(m.sender)
    return out
