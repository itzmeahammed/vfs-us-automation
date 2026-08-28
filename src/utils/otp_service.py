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
    mail = wait_for_otp_mail(email_user, email_password, since_epoch)
    read_attempts = max(1, _int_config("read_attempts", DEFAULT_READ_ATTEMPTS))
    return extract_code(mail, otp_length(), read_attempts)


def otp_length() -> int:
    """Configured OTP digit count ([otp] otp_length)."""
    return _int_config("otp_length", DEFAULT_OTP_LENGTH)


def wait_for_otp_mail(email_user: str, email_password: str,
                      since_epoch: float) -> otp_email.OtpMail:
    """
    Polls the mailbox and returns the fresh OTP email (OtpMail) WITHOUT reading
    the code — so the caller can read AND RE-READ the image on demand (e.g. to
    recover after VFS rejects a misread code with 'Please enter a valid OTP').

    Raises OtpError if [otp] is misconfigured or no matching email arrives in time.
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
            logging.info("OTP email arrived.")
            return mail
        time.sleep(poll_s)

    raise OtpError(
        f"No OTP email matching '{search_text}' arrived for {email_user} "
        f"within {timeout_s}s."
    )


def extract_code(mail: otp_email.OtpMail, otp_len: int, read_attempts: int = 1,
                 min_temperature: float = 0.0, exclude=None) -> str:
    """
    Reads the OTP out of a fetched email: plain-text body first (free), then the
    image attachment via OpenAI. Raises OtpError if neither yields a usable code.

    The image read is retried up to `read_attempts` times on the SAME image so a
    misread (e.g. reading a decoy row of VFS's anti-OCR captcha) is recovered
    in-place instead of costing a browser relaunch. Each retry uses a DIFFERENT
    PROMPT rather than a higher temperature: on a short digit read the model's
    argmax barely moves with temperature, so the old ladder just repeated the
    same wrong answer three times.

    `exclude` is a set of codes already REJECTED by VFS. They are both filtered
    out of the result AND named in the prompt, so the model is told which
    readings were wrong instead of being re-asked the identical question.

    `min_temperature` is passed through unchanged (no longer escalated); keep it
    at 0 for a deterministic first read.
    """
    exclude = exclude or set()

    # Cheap path: the code is sometimes right in the body text.
    code = _find_digits(mail.body_text, otp_len)
    if code and code not in exclude:
        logging.info(f"OTP found in the email body: {code}")
        return code

    if not mail.image:
        raise OtpError(
            "OTP email has no code in its text body and no image attachment."
        )

    attempts = max(1, read_attempts)
    last_text = ""
    # Codes the model must not return again: those VFS already refused, PLUS any
    # it produces in this loop. Both are named in the prompt — previously the
    # retry prompt was byte-identical, so the model had no reason to change its
    # answer and returned the same rejected code every pass (logs 2026-08-18).
    tried = set(exclude)
    for attempt in range(1, attempts + 1):
        last_text = otp_openai.read_otp_image(
            mail.image, mail.image_mime or "image/png",
            expected_len=otp_len, temperature=min_temperature,
            rejected=sorted(tried), style=attempt - 1,
        )
        code = _find_digits(last_text, otp_len)
        if code and code not in exclude:
            where = f" on attempt {attempt}" if attempt > 1 else ""
            logging.info(f"OTP read from the email image{where}: {code}")
            return code
        if code:
            tried.add(code)
        why = (f"already-rejected code {code}" if code and code in exclude
               else f"no {otp_len}-digit code (raw '{last_text}')")
        logging.warning(
            f"OTP image read attempt {attempt}/{attempts}: {why}."
            + (" Retrying with a different prompt..." if attempt < attempts else "")
        )

    raise OtpError(
        f"OpenAI could not produce a usable {otp_len}-digit code after "
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
