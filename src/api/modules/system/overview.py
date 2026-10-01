"""Pipeline overview endpoint.

Returns the full pipeline state in one call — the same data the inbox panel
HTML page renders, but as JSON for the web app to consume.

This is the highest-value single endpoint for a dashboard: it answers
"what is everything doing right now?" without N+1 calls.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends

from src.api.modules.system.schemas import PipelineResponse
from src.api.core.security import require_token

log = logging.getLogger("vfs.api.pipeline")


def get_pipeline() -> PipelineResponse:
    """Full pipeline snapshot: clients, journal, routes, mailboxes, health.

    Reads local files only — no browser, no IMAP, no VFS contact. The data
    is what has already been recorded; refreshing it is a separate action.

    Declared `def` (sync): all I/O is file reads, and FastAPI runs sync
    handlers in a threadpool rather than blocking the event loop.
    """
    from src.inbox.panel import collect

    data = collect()
    return PipelineResponse(**data)
