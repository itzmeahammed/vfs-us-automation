"""OpenAI vision client for reading an OTP out of an email image.

Single responsibility: given image bytes, ask an OpenAI vision model what OTP
the image shows and return the model's raw text answer. Validation (digit
count etc.) is the caller's job — see otp_service.py.

Configuration ([openai], secrets belong in config.local.ini):
    api_key = sk-...
    model   = gpt-4o-mini        ; any vision-capable chat model

Uses urllib from the stdlib (same approach as the Telegram sender) — no new
dependencies.
"""

import base64
import json
import logging
import urllib.request

from src.utils.config_reader import get_config_value

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"

PROMPT = (
    "Locate the green ribbon banner labeled 'OTP' with a right-pointing arrow. "
    "Read the Number the arrow points to. "
    "Output only the Number as a continuous string. "
    "If multiple numbers are visible, use only the number indicated by the OTP banner. "
    "Return digits only."
)


class OpenAiError(Exception):
    """The OpenAI request failed (config missing, HTTP error, bad response)."""


def is_configured() -> bool:
    return bool(get_config_value("openai", "api_key"))


def read_otp_image(image: bytes, mime: str = "image/png") -> str:
    """
    Sends the image to the configured OpenAI vision model and returns the
    model's text reply (expected to be the OTP digits, but NOT validated here).

    Raises OpenAiError if the API key is missing or the request fails.
    """
    api_key = get_config_value("openai", "api_key")
    if not api_key:
        raise OpenAiError(
            "No [openai] api_key configured (config.local.ini) — cannot read "
            "the OTP image."
        )
    model = get_config_value("openai", "model", DEFAULT_MODEL) or DEFAULT_MODEL

    data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {
        "model": model,
        "max_tokens": 20,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise OpenAiError(f"OpenAI request failed: {e}") from e

    try:
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise OpenAiError(f"Unexpected OpenAI response shape: {body}") from e

    logging.debug(f"OpenAI ({model}) read OTP image as: '{text}'")
    return text
