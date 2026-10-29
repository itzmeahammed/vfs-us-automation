"""Text-based OTP extraction — NO AI / no OpenAI.

Unlike the image-OTP routes (Italy, etc.) where the code is baked into a PNG and
must be read by OpenAI vision, some VFS OTP emails deliver the code as plain
text in the body, e.g.:

    "The OTP for your application with VFS Global is 512157. The OTP will
     expire in 5 minutes."

So the code is simply a standalone run of digits in the email text — read it
directly and fill it in. This module deliberately NEVER calls the OpenAI reader.

USED BY MORE THAN ONE ROUTE, which is why it is not named after a country. It
began as greece_otp.py for Greece's SIGN-IN OTP, and France's MID-BOOKING OTP
(src/booking/otp_step.py) sends the same shape of mail, so it reads it the same
way. Renamed 2026-10-29 after a France run logged "Greece OTP read from email
HTML" and the operator quite reasonably asked why Greece was involved in a
French booking.

Two ways in:
  * a sign-in OTP route sets "otp_mode": "text" alongside "otp": true in
    config/routes/<ROUTE>.json, and otp_flow routes here instead of the AI path;
  * a booking step of type "email_otp" sets "otp_mode": "text" in
    config/booking/<ROUTE>.json.
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
        logging.info(f"OTP read from the email body text: {code}")
        return code

    # Fallback: some VFS emails carry the code only in an HTML part. Strip tags
    # and try again (still no image / no AI).
    html = getattr(mail, "html_text", "") or ""
    if html:
        stripped = re.sub(r"<[^>]+>", " ", html)
        code = _find_digits(stripped, otp_len)
        if code:
            logging.info(f"OTP read from the email HTML: {code}")
            return code

    raise OtpError(
        f"No standalone {otp_len}-digit code in the email text "
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
