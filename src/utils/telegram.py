"""Telegram Bot API sender with two destinations.

Both channels live in the [telegram] section of config.ini:

  * SUCCESS channel  — slot reports. Uses `bot_token` + `TELEGRAM_chat_id`.
  * ERROR/SUMMARY    — failure alerts. Uses `TELEGRAM_SUMMARY_BOT_TOKEN` +
                       `TELEGRAM_SUMMARY_CHAT_ID` (falls back to `bot_token`
                       if the summary token is blank).

Sending is best-effort: any failure is logged and swallowed so it never breaks
the bot's main flow.
"""

import json
import logging
import urllib.parse
import urllib.request

from src.utils.config_reader import get_config_value


def _success_token() -> str:
    return get_config_value("telegram", "bot_token")


def _success_chat() -> str:
    # Renamed from `chat_id` -> `TELEGRAM_chat_id`; fall back to the old key so
    # older configs keep working. (configparser keys are case-insensitive.)
    return (
        get_config_value("telegram", "telegram_chat_id")
        or get_config_value("telegram", "chat_id")
    )


def _error_token() -> str:
    # The summary bot; reuse the success bot if no separate token is configured.
    return (
        get_config_value("telegram", "telegram_summary_bot_token")
        or _success_token()
    )


def _error_chat() -> str:
    return get_config_value("telegram", "telegram_summary_chat_id")


def is_configured() -> bool:
    """True if the SUCCESS channel (slot reports) is fully configured."""
    return bool(_success_token() and _success_chat())


def is_error_configured() -> bool:
    """True if the ERROR/summary channel (failure alerts) is fully configured."""
    return bool(_error_token() and _error_chat())


def send_message(text: str) -> bool:
    """Send a SUCCESS/slot message to the success chat (TELEGRAM_chat_id)."""
    return _send(_success_token(), _success_chat(), text, channel="success")


def send_error(text: str) -> bool:
    """Send an ERROR/failure message to the summary chat (TELEGRAM_SUMMARY_CHAT_ID)."""
    return _send(_error_token(), _error_chat(), text, channel="error")


def _send(token: str, chat_id: str, text: str, channel: str) -> bool:
    """
    Post `text` to `chat_id` via `token`. Returns True on success.

    Never raises — logs and returns False if the channel is unconfigured or the
    request fails, so callers can fire-and-forget.
    """
    if not token or not chat_id:
        logging.warning(
            f"Telegram {channel} channel not configured — skipping send."
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            logging.info(f"Telegram message sent ({channel} channel).")
            return True
        logging.warning(f"Telegram API returned not-ok ({channel}): {body}")
        return False
    except Exception as e:
        logging.warning(f"Failed to send Telegram message ({channel}): {e}")
        return False
