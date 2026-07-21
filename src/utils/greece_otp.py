"""Text-based OTP extraction for the Greece route — NO AI / no OpenAI.

Unlike the image-OTP routes (Italy, etc.) where the code is baked into a PNG and
must be read by OpenAI vision, Greece's VFS OTP email delivers the code as plain
text in the body, e.g.:

    "The OTP for your application with VFS Global is 512157. The OTP will
     expire in 5 minutes."

So the code is simply a standalone run of digits in the email text — read it
directly and fill it in. This module deliberately NEVER calls the OpenAI reader.

A route opts in by setting "otp_mode": "text" (alongside "otp": true) in its
config/routes/<ROUTE>.json; otp_flow then routes it here instead of the AI path.
"""

import logging
import re

from src.utils.otp_service import OtpError


def extract_code(mail, otp_len: int) -> str:
    """
    Return the OTP digits found in the email TEXT (plain-text body first, then a
    tag-stripped HTML body). Never reads the image / calls AI.

    Raises OtpError if no standalone `otp_len`-digit code is present in the text.
    """
    body = mail.body_text or ""
    code = _find_digits(body, otp_len)
    if code:
        logging.info(f"Greece OTP read from email text: {code}")
        return code

    # Fallback: some VFS emails carry the code only in an HTML part. Strip tags
    # and try again (still no image / no AI).
    html = getattr(mail, "html_text", "") or ""
    if html:
        stripped = re.sub(r"<[^>]+>", " ", html)
        code = _find_digits(stripped, otp_len)
        if code:
            logging.info(f"Greece OTP read from email HTML: {code}")
            return code

    raise OtpError(
        f"Greece OTP: no standalone {otp_len}-digit code in the email text "
        f"(body={len(body)} chars, html={len(html)} chars). "
        "This route is text-only — no AI image fallback."
    )


def _find_digits(text: str, otp_len: int) -> str:
    """
    First standalone run of exactly `otp_len` digits in `text`, or ''. Standalone
    (not part of a longer number) so the '5' in 'expire in 5 minutes', years, and
    phone numbers in the boilerplate can never be mistaken for the code.
    """
    if not text:
        return ""
    m = re.search(rf"(?<!\d)(\d{{{otp_len}}})(?!\d)", text)
    return m.group(1) if m else ""
