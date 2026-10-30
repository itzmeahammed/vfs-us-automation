"""Inbox: the mailbox watcher that turns VFS emails into bookings.

    GET  /v1/inbox/status          is the watcher alive, what did the last pass
                                   find, what is queued (one call for a panel)
    GET  /v1/inbox/stream          the same, pushed as Server-Sent Events every
                                   few seconds — the "regular update" feed
    GET  /v1/inbox/confirmations   VFS appointment-confirmation emails and what
                                   each one settled
    POST /v1/inbox/pass            run one pass NOW (returns its summary)

The watcher itself runs as a thread inside this API process
(src/inbox/background.py). Switch its booking side with PATCH /v1/switches
{"invite_booking": true}; the thread picks that up at its next pass.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.core.errors import ERROR_RESPONSES
from src.api.core.pagination import PageParams, paginate
from src.api.core.security import require_token

router = APIRouter(prefix="/inbox", tags=["inbox"],
                   dependencies=[Depends(require_token)],
                   responses=ERROR_RESPONSES)

STREAM_MAX_SECONDS = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_view() -> Dict[str, Any]:
    """Everything a panel needs about the watcher, in one object. Never raises
    for a missing file: an unwritten heartbeat reads as 'never ran'."""
    from src.booking import confirmations, invites
    from src.inbox import background, heartbeat
    from src.settings import settings

    hb = heartbeat.read()
    age = heartbeat.age_seconds(hb)
    live = heartbeat.is_live(data=hb)
    sw = settings().switches
    cfg = settings().inbox

    try:
        from src.inbox import watcher
        # Full addresses, not masks. These are our own booking mailboxes, and
        # an operator told "the invitation is in a****t@travnook.com" cannot
        # act on it — there are several accounts and a mask collapses them.
        watched = [email for email, _ in watcher.mailbox_accounts()]
    except Exception as exc:                                # noqa: BLE001
        watched = []
        mailbox_error = str(exc)
    else:
        mailbox_error = ""

    inv_counts = Counter(i.get("status") for i in invites.list_all())
    confs = confirmations.list_all()
    conf_counts = Counter(c.get("status") for c in confs)

    verdict = ("running" if hb.get("state") == heartbeat.RUNNING
               else "live" if live
               else "disabled" if hb.get("state") == heartbeat.DISABLED
               else "never_ran" if not hb
               else "stale")

    return {
        "server_time": _now_iso(),
        "watcher": {
            "verdict": verdict,
            "live": live,
            "thread_in_this_process": background.is_running(),
            "age_seconds": None if age is None else round(age),
            **{k: v for k, v in hb.items() if k != "last_pass"},
        },
        "last_pass": hb.get("last_pass") or {},
        "switches": {
            "invite_booking": bool(sw.invite_booking),
            "test_booking": bool(sw.test_booking),
            "background_watch": bool(cfg.background_watch),
            "poll_seconds": int(cfg.poll_seconds),
        },
        "mailboxes": {"count": len(watched), "watched": watched,
                      "error": mailbox_error},
        "invitations": {s: inv_counts.get(s, 0) for s in
                        (invites.PENDING, invites.BOOKING, invites.BOOKED,
                         invites.NEEDS_ATTENTION, invites.MANUAL,
                         invites.EXPIRED, invites.DISMISSED)},
        "confirmations": {
            **{s: conf_counts.get(s, 0) for s in confirmations.STATUSES},
            "latest": [_conf_view(c) for c in confs[:5]],
        },
    }


def _conf_view(item: Dict[str, Any]) -> Dict[str, Any]:
    from src.booking import invites
    out = dict(item)
    out["account"] = invites._mask(item.get("account", ""))
    return out


@router.get("/status", summary="Is the inbox watcher alive, and what did it find")
def get_status() -> Dict[str, Any]:
    return status_view()


@router.get("/stream", summary="Watcher status as Server-Sent Events")
async def stream_status(interval: int = Query(default=10, ge=3, le=120,
                                              description="seconds between updates")
                        ) -> StreamingResponse:
    """Pushes a `status` event immediately and then every `interval` seconds,
    for up to an hour, so a page can show the watcher working without
    polling. Each event's data is the same object GET /v1/inbox/status returns."""
    async def gen():
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        loop = asyncio.get_running_loop()
        while True:
            try:
                view = await loop.run_in_executor(None, status_view)
                payload = json.dumps(view, ensure_ascii=False, default=str)
                yield f"event: status\ndata: {payload}\n\n"
            except Exception as exc:                        # noqa: BLE001
                yield ("event: error\ndata: "
                       + json.dumps({"detail": str(exc)}) + "\n\n")
            if time.monotonic() >= deadline:
                yield "event: end\ndata: {\"reason\": \"max duration reached\"}\n\n"
                return
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/confirmations", summary="VFS appointment-confirmation emails")
def list_confirmations(page: PageParams = Depends(),
                       status: Optional[str] = Query(
                           default=None, description="matched | unmatched | ambiguous"),
                       route: Optional[str] = None) -> Dict[str, Any]:
    from src.booking import confirmations

    items = [c for c in confirmations.list_all(status=status)
             if not route or c.get("route") == route.upper()]
    return paginate([_conf_view(c) for c in items], page)


@router.post("/pass", summary="Run one inbox pass now")
async def run_pass_now() -> JSONResponse:
    """Reads every mailbox once and acts on what it finds exactly as the
    background watcher would (booking only while invite_booking is on).
    409 when a pass is already running."""
    from src.inbox import service

    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(None, service.run_cycle, "api-manual")
    code = 409 if summary.get("skipped") else 200
    return JSONResponse(status_code=code, content=json.loads(
        json.dumps(summary, default=str)))
