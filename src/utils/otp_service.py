"""OTP orchestration: poll the mailbox, read the code, hand it to the bot.

This is the ONLY module the bot calls for OTP. It glues together:

    otp_email.py   — IMAP: find the fresh OTP email, return body + image
    otp_openai.py  — OpenAI vision: read the code out of the image

Flow: poll the mailbox until an email matching the search text arrives AFTER
the sign-in timestamp (a stale OTP from an earlier run is useless), then try
the cheap path first — digits in the plain-text body — and only call OpenAI
on the image when the body doesn't contain the code.

Configuration ([otp] in config.ini; enable per route with "otp": true in
config/routes/<ROUTE>.json):
    imap_host       = mail.example.com
    imap_port       = 993
    search_text     = The OTP for your application with VFS Global is
    timeout_seconds = 120    ; total time to wait for the email
    poll_seconds    = 5      ; mailbox poll interval
    otp_length      = 6      ; expected number of digits
    read_attempts   = 3      ; OpenAI image-read retries before giving up

The mailbox login is NOT configured here — it reuses the active VFS
credential's email/password (accounts rotate hourly; each account's email is
a mailbox on imap_host).
"""

import logging
import re
import time

from src.utils import otp_email, otp_openai
from src.utils.config_reader import get_config_value

DEFAULT_SEARCH_TEXT = "The OTP for your application with VFS Global is"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_POLL_SECONDS = 5
DEFAULT_OTP_LENGTH = 6
DEFAULT_READ_ATTEMPTS = 3   # OpenAI image-read tries before giving up on a code


class OtpError(Exception):
    """OTP could not be obtained (no email in time, unreadable image, etc.)."""


def _int_config(key: str, default: int) -> int:
    try:
        return int(str(get_config_value("otp", key, str(default))).strip())
    except ValueError:
        return default


def get_otp(email_user: str, email_password: str, since_epoch: float) -> str:
    """
    Waits for the OTP email and returns the validated OTP digits.

    Args:
        email_user / email_password: the ACTIVE VFS credential — also the
            mailbox login on the configured IMAP host.
        since_epoch: when Sign In was clicked; only emails received after this
            moment count (protects against reading a previous run's OTP).

    Raises:
        OtpError: if the [otp] config is missing, no matching email arrives
        within the timeout, or the code can't be read/validated.
    """
    host = get_config_value("otp", "imap_host")
    if not host:
        raise OtpError("[otp] imap_host is not configured — cannot fetch OTP.")
    port = _int_config("imap_port", 993)
    search_text = (
        get_config_value("otp", "search_text", DEFAULT_SEARCH_TEXT)
        or DEFAULT_SEARCH_TEXT
    )
    timeout_s = _int_config("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    poll_s = max(1, _int_config("poll_seconds", DEFAULT_POLL_SECONDS))
    otp_len = _int_config("otp_length", DEFAULT_OTP_LENGTH)
    read_attempts = max(1, _int_config("read_attempts", DEFAULT_READ_ATTEMPTS))

    logging.info(
        f"Waiting for OTP email on {host} for {email_user} "
        f"(up to {timeout_s}s, polling every {poll_s}s)..."
    )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        mail = otp_email.fetch_latest_otp_mail(
            host, port, email_user, email_password, search_text, since_epoch
        )
        if mail is not None:
            logging.info("OTP email arrived — extracting the code.")
            return _extract_code(mail, otp_len, read_attempts)
        time.sleep(poll_s)

    raise OtpError(
        f"No OTP email matching '{search_text}' arrived for {email_user} "
        f"within {timeout_s}s."
    )


def _extract_code(mail: otp_email.OtpMail, otp_len: int,
                  read_attempts: int = 1) -> str:
    """
    Pulls the OTP out of a fetched email: plain-text body first (free), then
    the image attachment via OpenAI. Raises OtpError if neither yields a code.

    The image read is retried up to `read_attempts` times on the SAME image
    before giving up — a transient OCR miss (e.g. a 7-digit misread of a 6-digit
    code) is then recovered in-place instead of costing a whole browser relaunch.
    Retries raise the temperature so a re-read can differ from the first answer.
    """
    # Cheap path: the code is sometimes right in the body text.
    code = _find_digits(mail.body_text, otp_len)
    if code:
        logging.info(f"OTP found in the email body: {code}")
        return code

    if not mail.image:
        raise OtpError(
            "OTP email has no code in its text body and no image attachment."
        )

    attempts = max(1, read_attempts)
    last_text = ""
    for attempt in range(1, attempts + 1):
        # First read deterministic; retries get a nudge of temperature so the
        # model can produce a different (hopefully correct) answer rather than
        # repeating the same misread.
        temperature = 0.0 if attempt == 1 else 0.4
        last_text = otp_openai.read_otp_image(
            mail.image, mail.image_mime or "image/png",
            expected_len=otp_len, temperature=temperature,
        )
        code = _find_digits(last_text, otp_len)
        if code:
            where = f" on attempt {attempt}" if attempt > 1 else ""
            logging.info(f"OTP read from the email image{where}: {code}")
            return code
        logging.warning(
            f"OTP image read attempt {attempt}/{attempts} gave no "
            f"{otp_len}-digit code (got '{last_text}')."
            + (" Retrying..." if attempt < attempts else "")
        )

    raise OtpError(
        f"OpenAI reply did not contain a {otp_len}-digit code after "
        f"{attempts} attempt(s); last reply: '{last_text}'"
    )


def _find_digits(text: str, otp_len: int) -> str:
    """
    Returns the first standalone run of exactly `otp_len` digits in `text`,
    or ''. Standalone means not part of a longer number (dates, phone numbers
    and years in the boilerplate must not match).
    """
    if not text:
        return ""
    m = re.search(rf"(?<!\d)(\d{{{otp_len}}})(?!\d)", text)
    return m.group(1) if m else ""


if __name__ == "__main__":
    # Manual end-to-end test WITHOUT the browser: fetches the newest OTP email
    # from the last hour for the given mailbox and prints the extracted code.
    #
    #   python -m src.utils.otp_service you@example.com 'mailbox-password'
    import sys

    from src.utils.config_reader import initialize_config

    initialize_config()
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    if len(sys.argv) != 3:
        print("Usage: python -m src.utils.otp_service <email> <password>")
        sys.exit(2)
    print("OTP:", get_otp(sys.argv[1], sys.argv[2], since_epoch=time.time() - 3600))
