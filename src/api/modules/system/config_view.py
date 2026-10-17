"""Read-only configuration view.

Exposes operational settings so the web app can show why something is off
without requiring SSH access. Secrets are stripped — only tunables and
switches are returned.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends

from src.api.modules.system.schemas import ConfigResponse
from src.api.core.security import require_token

log = logging.getLogger("vfs.api.config_view")


#: Substrings that mark a key as secret. Matched as fragments, NOT exact names.
#:
#: This was an exact-match blocklist, which happened to cover everything
#: reachable at the time — `Webhook.secret` matches "secret" exactly, and a
#: full check of the live response found no leak. But it would have passed any
#: future `signing_secret`, `account_password`, `openai_api_key` or
#: `client_token` straight through to the browser, silently. The proxy's own
#: scrubber already does it this way (`any(s in k.lower() for s in ...)`), and
#: this endpoint's docstring makes the strong claim ("secrets stripped") that
#: the UI repeats to the operator — so it should hold for keys nobody has
#: thought of yet.
_SECRET_FRAGMENTS = (
    "secret", "password", "token", "api_key", "apikey",
    "credential", "cookie", "private",
)

#: Not secrets, but infrastructure detail with no operational value here.
_EXACT_STRIP = frozenset({"imap_host", "imap_port"})


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return low in _EXACT_STRIP or any(f in low for f in _SECRET_FRAGMENTS)


def _redact_url(value: Any) -> Any:
    """Drop a URL's query string.

    A webhook URL is a common place to carry a bearer value as a query
    parameter, and the whole URL was returned verbatim. Empty in this
    deployment, so nothing leaked — but the shape invited one.
    """
    if not isinstance(value, str) or "?" not in value:
        return value
    from urllib.parse import urlsplit

    try:
        return urlsplit(value)._replace(query="", fragment="").geturl()
    except Exception:                                   # noqa: BLE001
        return ""


def _strip_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove keys that look like secrets, and defuse URLs that may carry one."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if _is_secret_key(key):
            continue
        out[key] = _redact_url(value) if "url" in str(key).lower() else value
    return out


def get_config() -> ConfigResponse:
    """Read-only dump of operational settings, secrets stripped.

    Returns schedule, timeouts, retry, browser, safety thresholds, waitlist
    switches, webhook state, inbox tunables, and bandwidth config.
    """
    from src.settings import settings

    cfg = settings()
    return ConfigResponse(
        # The four master switches belong here. Without them the panel showed
        # `register_enabled: false, dry_run: true` while `live_booking` was ON,
        # under a callout reading "these are the bot's own settings, shown so
        # you can see why it behaves as it does" — so an operator asking "is
        # this machine safe?" could reasonably conclude nothing can happen.
        switches=_strip_secrets(cfg.switches.model_dump()),
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
