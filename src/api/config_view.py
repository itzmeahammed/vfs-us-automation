"""Read-only configuration view.

Exposes operational settings so the web app can show why something is off
without requiring SSH access. Secrets are stripped — only tunables and
switches are returned.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from src.api.schemas import ConfigResponse
from src.api.security import require_token

log = logging.getLogger("vfs.api.config_view")

router = APIRouter(tags=["config"])

# Keys to strip from any section before returning.
_SECRET_KEYS = frozenset({
    "secret", "secret_token", "password", "token", "api_key",
    "imap_host", "imap_port",
})


def _strip_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove keys that look like secrets."""
    return {k: v for k, v in data.items() if k.lower() not in _SECRET_KEYS}


@router.get(
    "/config",
    response_model=ConfigResponse,
    dependencies=[Depends(require_token)],
)
def get_config() -> ConfigResponse:
    """Read-only dump of operational settings, secrets stripped.

    Returns schedule, timeouts, retry, browser, safety thresholds, waitlist
    switches, webhook state, inbox tunables, and bandwidth config.
    """
    from src.settings import settings

    cfg = settings()
    return ConfigResponse(
        schedule=_strip_secrets(cfg.schedule.model_dump()),
        timeouts=_strip_secrets(cfg.timeouts.model_dump()),
        retry=_strip_secrets(cfg.retry.model_dump()),
        browser=_strip_secrets(cfg.browser.model_dump()),
        account_safety=_strip_secrets(cfg.account_safety.model_dump()),
        waitlist=_strip_secrets(cfg.waitlist.model_dump()),
        webhook=_strip_secrets(cfg.webhook.model_dump()),
        inbox=_strip_secrets(cfg.inbox.model_dump()),
        bandwidth=_strip_secrets(cfg.bandwidth.model_dump()),
    )
