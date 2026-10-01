"""Notifications: the outbound webhook to the web app, and Telegram.

    POST /v1/notifications/webhook/test          send a test event
    GET  /v1/notifications/webhook/deadletters   events that could not be delivered
    POST /v1/notifications/telegram/test         send a test message to the testing-bot chat
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from src.api.modules.notifications import handlers as handlers
from src.api.core.errors import ERROR_RESPONSES, ApiError
from src.api.core.security import require_token

router = APIRouter(prefix="/notifications", tags=["notifications"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)

router.post("/webhook/test", summary="Send a test webhook")(handlers.test_webhook)
router.get("/webhook/deadletters", summary="Undelivered webhook events")(handlers.get_deadletters)


@router.post("/telegram/test", summary="Send a test Telegram message")
def telegram_test() -> Dict[str, Any]:
    """Proves the chat that booking alerts go to is reachable."""
    from src.utils import telegram

    if not telegram.is_error_configured():
        raise ApiError(503, "The testing-bot chat is not configured ([telegram] "
                            "TELEGRAM_SUMMARY_*).", "telegram_not_configured")
    sent = telegram.send_error("🔔 Test message from the VFS API — booking "
                               "alerts will arrive in this chat.")
    if not sent:
        raise ApiError(502, "Telegram did not accept the message; see the API log.",
                       "telegram_failed")
    return {"sent": True}
