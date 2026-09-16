"""TV announcements: push slot finds to the web app's TV pipeline.

Sent alongside the Telegram slot report, but only for the destinations listed
in [tv_announce] destinations (France and Italy by default). Settings live in
the [tv_announce] section of config.ini; the API key belongs in
config/config.local.ini, never in config.ini.

The endpoint suppresses repeats of the same title for 5 minutes, so a retried
route re-announcing the same find inside that window is silently dropped by
the server ("duplicate": true) — that is intended.

Sending is best-effort, like Telegram: any failure is logged and swallowed so
it never breaks the bot's main flow.
"""

import json
import logging
import urllib.error
import urllib.request

from src.utils import telegram_message
from src.utils.config_reader import get_config_value

DEFAULT_URL = "https://www.travnooker.com/api/tv/ingest/announce"
DEFAULT_DESTINATIONS = "FRA, ITA"


def _enabled() -> bool:
    value = get_config_value("tv_announce", "enabled", "true") or "true"
    return value.strip().lower() in ("1", "true", "yes", "on")


def _url() -> str:
    return get_config_value("tv_announce", "url", DEFAULT_URL) or DEFAULT_URL


def _api_key() -> str:
    return get_config_value("tv_announce", "api_key", "") or ""


def _timeout() -> float:
    try:
        return float(get_config_value("tv_announce", "timeout_seconds", "10") or 10)
    except ValueError:
        return 10.0


def is_configured() -> bool:
    """True when the feature is on and has an API key to send with."""
    return _enabled() and bool(_url() and _api_key())


def applies_to(dest_code: str) -> bool:
    """True if slot finds for this destination should be announced.

    Compared by country name, so 'FRA' in config also matches a route coded 'FR'.
    """
    raw = get_config_value("tv_announce", "destinations", DEFAULT_DESTINATIONS)
    wanted = {telegram_message._country(c.strip()) for c in (raw or "").split(",") if c.strip()}
    return telegram_message._country(dest_code) in wanted


def build_announcement(dest_code: str, entries: list) -> dict:
    """The request body for one route's slot finds.

    `entries` is the route's (combo_dict, message) list. Only combinations with
    a real slot are listed, one 'Country - City - Category' line each, with no
    date — the TV only needs to say where a slot opened.
    """
    lines = [telegram_message._report_label(combo, dest_code)
             for combo, message in entries if telegram_message._has_slot(message)]
    return {
        "kind": get_config_value("tv_announce", "kind", "urgent") or "urgent",
        "title": f"{telegram_message._country(dest_code)} Slot Update:",
        "message": "\n".join(lines),
        "source": get_config_value("tv_announce", "source", "vfs-slot-checker")
                  or "vfs-slot-checker",
    }


def announce_slots(dest_code: str, entries: list) -> bool:
    """Announce a route's slot finds on the TV if its destination qualifies.

    Returns True only when the server accepted it. Never raises.
    """
    if not applies_to(dest_code):
        return False
    payload = build_announcement(dest_code, entries)
    if not payload["message"]:
        return False                      # no combination has a slot
    if not is_configured():
        logging.warning(
            "TV announce not configured — skipping. "
            "Set [tv_announce] api_key in config/config.local.ini to enable it."
        )
        return False
    return send(payload)


def send(payload: dict) -> bool:
    """POST one announcement. Returns True on success; logs and returns False otherwise."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key(),
        "User-Agent": "vfs-slot-checker-tv/1",
    }
    try:
        req = urllib.request.Request(_url(), data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
        if not result.get("ok"):
            logging.warning(f"TV announce returned not-ok: {result}")
            return False
        if result.get("duplicate"):
            logging.info(f"TV announce accepted as a duplicate (suppressed): {payload['title']}")
        else:
            logging.info(f"TV announce sent: {payload['title']}")
        return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        logging.warning(f"TV announce failed (HTTP {e.code}): {detail}")
        return False
    except Exception as e:
        logging.warning(f"Failed to send TV announce: {e}")
        return False
