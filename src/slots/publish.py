"""Push the slot board to the web app's TV ingest API.

The board is built here and POSTed out, the same direction `tv_announce` already
sends slot finds. The web app stores the latest payload and serves it to its own
React page, so the browser never talks to this machine.

That direction is the whole point. This PC runs the bot on a schedule and is
asleep outside it, so a web page that ASKED this machine for the board would
time out every night and on every reboot. Pushing a snapshot means the page has
something to show whatever this machine is doing — and the board is honest about
its own age, because `generated_iso` and every card's "Seen 28m" / "Was open 6h
ago" travel with it. A stale board says so; it does not pretend.

One thing the receiver must NOT do: deduplicate. The announce endpoint suppresses
a repeated title for five minutes, which is right for an alert and wrong for a
snapshot. Every board POST should overwrite the stored one — newest wins.

Best-effort, like Telegram and the announcements: any failure is logged and
swallowed so it never breaks a run that worked. There is no retry queue on
purpose. A missed push is superseded by the next cycle half an hour later, and a
queue would add a failure mode worse than the one it fixes.
"""

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from typing import Optional

from src.slots import db, query, wall
from src.utils.config_reader import get_config_value

DEFAULT_URL = "https://www.travnooker.com/api/tv/ingest/board"

# Bumped when the payload shape changes in a way that would break a renderer.
# The web app should refuse a schema it does not know rather than render blanks.
SCHEMA = 1

_SECTION = "tv_board"


def _enabled() -> bool:
    value = get_config_value(_SECTION, "enabled", "false") or "false"
    return value.strip().lower() in ("1", "true", "yes", "on")


def _url() -> str:
    return get_config_value(_SECTION, "url", DEFAULT_URL) or DEFAULT_URL


def _api_key() -> str:
    """This section's key, falling back to the announcement key.

    Both go to the same host on the same credentials, so making someone paste
    the key twice into config.local.ini only creates a way for the two to drift.
    """
    own = get_config_value(_SECTION, "api_key", "") or ""
    if own.strip():
        return own.strip()
    return (get_config_value("tv_announce", "api_key", "") or "").strip()


def _timeout() -> float:
    try:
        return float(get_config_value(_SECTION, "timeout_seconds", "15") or 15)
    except ValueError:
        return 15.0


def _days() -> int:
    try:
        return int(get_config_value(_SECTION, "days", str(query.DEFAULT_DAYS))
                   or query.DEFAULT_DAYS)
    except ValueError:
        return query.DEFAULT_DAYS


def is_configured() -> bool:
    """True when the feature is on and has somewhere to send with a key."""
    return _enabled() and bool(_url() and _api_key())


def build_body(conn: sqlite3.Connection, days: Optional[int] = None) -> dict:
    """The request body: the board, wrapped in the ingest envelope.

    `kind` and `source` mirror the announcement body so the receiving side can
    route both through one handler if it wants to. Everything the page renders
    is under `board`.
    """
    board = wall.build_payload(conn, days=days if days is not None else _days())
    board["schema"] = SCHEMA
    return {
        "kind": "slot_board",
        "source": get_config_value("tv_announce", "source", "vfs-slot-checker")
                  or "vfs-slot-checker",
        "schema": SCHEMA,
        "generated_iso": board.get("generated_iso"),
        "board": board,
    }


def send(body: dict) -> bool:
    """POST one board. Returns True on success; logs and returns False otherwise."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key(),
        "User-Agent": "vfs-slot-checker-board/1",
    }
    try:
        req = urllib.request.Request(_url(), data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8") or "{}"
        result = json.loads(raw) if raw.strip().startswith("{") else {}
        # An empty or non-JSON 200 is taken as acceptance: the receiver's job is
        # to store a blob, and insisting on a body shape would make this fail
        # for a perfectly good endpoint.
        if result and result.get("ok") is False:
            logging.warning(f"Slot board push returned not-ok: {result}")
            return False
        logging.info(
            f"Slot board pushed ({len(data) // 1024} KB, "
            f"{len(body['board'].get('views', []))} view(s))."
        )
        return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        logging.warning(f"Slot board push failed (HTTP {e.code}): {detail}")
        return False
    except Exception as e:
        logging.warning(f"Failed to push the slot board: {e}")
        return False


def push_quietly(db_path: Optional[str] = None) -> bool:
    """Build the board and push it. Never raises — called at the end of a run.

    Returns True only when the server accepted it.
    """
    if not _enabled():
        return False
    if not is_configured():
        logging.warning(
            "Slot board push is on but not configured — skipping. Set "
            "[tv_board] url and an api_key (or [tv_announce] api_key) in "
            "config/config.local.ini."
        )
        return False
    try:
        from src.slots import store as store_mod
        # Read-only, like the page builders: pushing the board must never be the
        # thing that migrates or locks the database a run is still writing to.
        conn = db.connect(db_path or store_mod.db_path(), read_only=True)
        try:
            body = build_body(conn)
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Could not build the slot board to push: {e}")
        return False
    return send(body)
