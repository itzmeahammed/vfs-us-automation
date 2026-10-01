"""Webhook management endpoints.

The outbound webhook layer (src/utils/webhook.py) is fully implemented but
OFF by default. These endpoints let the web app verify the wiring and
inspect delivery failures without SSH access.
"""

from __future__ import annotations

import logging

from fastapi import Depends

from src.api.modules.notifications.schemas import WebhookDeadletterResponse, WebhookTestResponse
from src.api.core.security import require_token

log = logging.getLogger("vfs.api.webhooks")


def test_webhook() -> WebhookTestResponse:
    """Send a test ping to verify the outbound webhook works end to end.

    Returns the delivery result — delivered, skipped (not configured), or
    failed with the error and attempt count.
    """
    from src.utils.webhook import send_test_ping

    result = send_test_ping()
    return WebhookTestResponse(
        delivered=result.delivered,
        event=result.event,
        attempts=result.attempts,
        status_code=result.status_code,
        error=result.error,
        skipped=result.skipped,
    )


def get_deadletters() -> WebhookDeadletterResponse:
    """How many webhook deliveries failed and are waiting in the dead-letter log."""
    from src.utils.webhook import deadletter_count, is_configured

    return WebhookDeadletterResponse(
        count=deadletter_count(),
        configured=is_configured(),
    )
