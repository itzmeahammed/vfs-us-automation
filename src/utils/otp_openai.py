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
import re
import urllib.request

from src.utils.config_reader import get_config_value

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
# Kept SHORT on purpose: a slow/hung OpenAI request must fail fast so the caller's
# in-session read_attempts loop can re-read the SAME email image — instead of a
# long hang forcing a whole browser relaunch (fresh login + new OTP ~ 8-10 MB).
DEFAULT_TIMEOUT_SECONDS = 18

# VFS's OTP image is a deliberate anti-OCR captcha, not a plain screenshot. What
# it actually contains (verified against a real sample):
#   * THREE horizontal rows of digits. Only ONE is the OTP; the other two are
#     decoys of the same length and style.
#   * A green ribbon on the left, labelled 'OTP', ending in a right-pointing
#     arrow. The arrow is vertically centred on the OTP row — that alignment is
#     the ONLY thing that identifies the real row.
#   * Digits recoloured individually (green / navy / purple / magenta) and set
#     at slightly different sizes and baselines, so one row LOOKS like several
#     separate numbers.
#   * Decorative curves crossing the digits, plus speckle noise.
#   * A translucent pale panel over part of the image that washes out the
#     contrast of the digits underneath it.
# The old prompt described only the ribbon, said nothing about the decoy rows,
# and so let the model pick a plausible-looking wrong row. Every instruction
# below maps to one of the traps above.
_BASE = (
    "This image is a deliberate anti-OCR captcha containing THREE horizontal "
    "rows of digits. Exactly ONE row is the OTP; the other two are decoys.\n"
    "Identify the correct row like this:\n"
    "1. Find the green ribbon on the left labelled 'OTP'. It ends in a "
    "right-pointing arrow.\n"
    "2. The OTP row is the one the arrow points at — the row whose vertical "
    "centre lines up with the arrow tip. Rows above or below it are decoys and "
    "must be ignored completely.\n"
    "Read that row left to right and transcribe EVERY digit in it.\n"
    "Traps to be aware of:\n"
    "- Digits in the row are individually recoloured and sized. A colour or "
    "size change does NOT start a new number — the whole row is one number.\n"
    "- Curved decorative lines and speckles cross the digits; they are not "
    "digits and must not be read as 1, 7 or 0.\n"
    "- A translucent panel dims part of the image. Digits under it are still "
    "part of the number — read them, do not skip them.\n"
    "- Watch the confusable pairs carefully: 3/8, 5/6, 1/7, 0/8, 9/4, 2/7."
)

# Retry framings. Varying the PROMPT is what actually changes a vision model's
# answer here — raising temperature does not: on a short, confident digit
# read the argmax tokens barely move, which is why the old 0.0/0.4/0.8 ladder
# returned the identical code three times in a row (see logs 2026-08-18).
_STYLES = (
    # Pass 1 — straight read.
    "",
    # Pass 2 — force a fresh look at the glyph shapes rather than the gestalt.
    "\nWork strictly one glyph at a time. For each digit in the OTP row, state "
    "its position and its colour before naming it. Do not guess the number as a "
    "whole first and then justify it.",
    # Pass 3 — re-derive which row is correct, in case the wrong row was picked.
    "\nFirst describe all three digit rows and say which one the arrow tip is "
    "vertically aligned with, and why. Then read ONLY that row. If a previous "
    "answer was rejected, the most likely cause is that the wrong row was read.",
)

_FORMAT = (
    "\n\nAnswer in exactly this form:\n"
    "DIGITS: <one line, the digits separated by spaces, in order>\n"
    "OTP: <the same digits with no spaces>"
)


class OpenAiError(Exception):
    """The OpenAI request failed (config missing, HTTP error, bad response)."""


def is_configured() -> bool:
    return bool(get_config_value("openai", "api_key"))


def build_prompt(expected_len: int = None, rejected=None, style: int = 0) -> str:
    """The prompt for one read: base instructions + retry framing + any codes
    VFS has already refused. Separate from the request so it can be unit-tested
    without an API key."""
    parts = [_BASE, _STYLES[style % len(_STYLES)]]
    if expected_len:
        parts.append(
            f"\nThe OTP row contains exactly {expected_len} digits. If you count "
            f"more, you have included a digit from a neighbouring row or read a "
            f"decorative mark as a digit — recount and return exactly "
            f"{expected_len}."
        )
    rejected = [str(r) for r in (rejected or []) if str(r).strip()]
    if rejected:
        # The decisive addition: previously the model was re-asked with a
        # byte-identical prompt, so it had no reason to answer differently and
        # returned the same rejected code every time.
        parts.append(
            "\nIMPORTANT: earlier readings of THIS SAME image returned "
            + ", ".join(rejected)
            + " and the service rejected them as incorrect. Those readings are "
            "wrong. Do not return them again. Re-examine the image from "
            "scratch: check you are on the row the arrow points at, and "
            "re-check every confusable digit."
        )
    parts.append(_FORMAT)
    return "".join(parts)


def _answer_from(text: str, expected_len: int = None) -> str:
    """Pull the final answer out of a transcription reply.

    The model now emits a DIGITS: working line before its OTP: answer, so the
    raw reply holds several digit runs. Returning the whole thing would let the
    caller's 'first run of N digits' matcher lock onto the working line. Prefer
    the OTP: line; fall back to the last digit run; else the raw text.
    """
    m = re.search(r"OTP\s*[:=]\s*([\d\s]+)", text, re.IGNORECASE)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits and (not expected_len or len(digits) == expected_len):
            return digits
        if digits:
            return digits
    runs = re.findall(r"\d+", text)
    return runs[-1] if runs else text.strip()


def read_otp_image(image: bytes, mime: str = "image/png",
                   expected_len: int = None, temperature: float = 0.0,
                   rejected=None, style: int = 0) -> str:
    """
    Sends the image to the configured OpenAI vision model and returns the OTP
    digits it answered with (validation — digit count etc. — stays the caller's
    job, see otp_service.py).

    Args:
        expected_len: if given, the model is told the exact digit count — this
            directly prevents the common off-by-one misread (e.g. returning 7
            digits when the OTP is 6).
        temperature: 0 is deterministic. Kept at 0 by default: on this task
            temperature is a weak lever (see _STYLES), so retries vary `style`.
        rejected: codes VFS has already refused for this image. Naming them in
            the prompt is what stops the model repeating a rejected answer.
        style: which retry framing to use; cycles through _STYLES.

    Raises OpenAiError if the API key is missing or the request fails.
    """
    api_key = get_config_value("openai", "api_key")
    if not api_key:
        raise OpenAiError(
            "No [openai] api_key configured (config.local.ini) — cannot read "
            "the OTP image."
        )
    model = get_config_value("openai", "model", DEFAULT_MODEL) or DEFAULT_MODEL

    prompt = build_prompt(expected_len, rejected, style)

    data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {
        "model": model,
        # Room for the per-digit transcription pass before the answer. The old
        # 20-token ceiling made "look more carefully" impossible — the model had
        # to commit to an answer immediately with no space to work.
        "max_tokens": 300,
        "temperature": temperature,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
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
        timeout_s = int(str(get_config_value(
            "openai", "request_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)).strip())
    except (ValueError, TypeError):
        timeout_s = DEFAULT_TIMEOUT_SECONDS
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise OpenAiError(f"OpenAI request failed: {e}") from e

    try:
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise OpenAiError(f"Unexpected OpenAI response shape: {body}") from e

    answer = _answer_from(text, expected_len)
    logging.debug(
        f"OpenAI ({model}, style {style % len(_STYLES)}) answered '{answer}'; "
        f"full reply: {text!r}"
    )
    return answer
