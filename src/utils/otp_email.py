"""IMAP fetcher for VFS OTP emails.

Single responsibility: log into a mailbox, find the newest email matching the
OTP search text that arrived AFTER a given timestamp, and return its image
attachment (plus the plain-text body). Nothing here knows about OpenAI or the
browser — see otp_service.py for the orchestration.

The mailbox login reuses the SAME email/password as the active VFS credential
(accounts rotate hourly, and each VFS account's email is a real mailbox on the
configured IMAP host). Only host/port are configured globally ([otp] section).

Uses only the stdlib (imaplib + email) — no new dependencies.
"""

import email
import email.utils
import imaplib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# How far before Sign In an OTP email may be stamped and still count. Covers the
# mail server's clock lagging ours plus INTERNALDATE's 1s truncation, while staying
# well under the ~100s minimum gap between two logins on the same mailbox (so a
# previous route's OTP is still rejected as stale).
CLOCK_SKEW_GRACE_S = 30

# (mailbox, uid) pairs already reported as stale — logged once, not every poll.
_logged_skips = set()


@dataclass
class OtpMail:
    """The parts of an OTP email the caller cares about."""
    body_text: str          # plain-text body ('' if none)
    image: Optional[bytes]  # first image attachment's bytes (None if none)
    image_mime: str         # its content type, e.g. 'image/png'
    received_epoch: float   # when the server received it (epoch seconds)
    html_text: str = ""     # raw text/html body ('' if none) — used by text-OTP routes


def fetch_latest_otp_mail(
    host: str,
    port: int,
    user: str,
    password: str,
    search_text: str,
    since_epoch: float,
) -> Optional[OtpMail]:
    """
    Returns the newest email whose body contains `search_text` and which the
    server received AFTER `since_epoch` (less CLOCK_SKEW_GRACE_S), or None if
    there is no such email yet.

    One-shot (no polling — the caller polls). Any IMAP failure is logged and
    returned as None so a mail-server hiccup reads as 'not arrived yet' and the
    caller simply polls again.
    """
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as e:
        logging.warning(f"IMAP connect to {host}:{port} failed: {e}")
        return None

    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)  # read-only: never mark as seen

        # Server-side narrowing: body text + received-on-or-after the given DAY
        # (IMAP SINCE has day granularity; exact filtering happens below).
        since_day = (
            datetime.fromtimestamp(since_epoch) - timedelta(days=1)
        ).strftime("%d-%b-%Y")
        criteria = f'(SINCE {since_day} BODY "{search_text}")'
        status, data = imap.uid("SEARCH", None, criteria)
        if status != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()
        if not uids:
            return None

        # Walk candidates from newest UID down; the first one received after
        # the cutoff wins (UID order tracks arrival order). The cutoff sits
        # CLOCK_SKEW_GRACE_S before since_epoch: VFS delivers the OTP ~1s after
        # Sign In, while INTERNALDATE is whole-second and the mail server's
        # clock runs 1-2s behind ours — a strict `> since_epoch` dropped the
        # real OTP as 'stale' (logs 2026-09-17: sign-in 18:44:33.5, OTP
        # stamped 18:44:34, skipped for all 120s).
        cutoff = since_epoch - CLOCK_SKEW_GRACE_S
        for uid in sorted(uids, key=int, reverse=True):
            received = _internal_date(imap, uid)
            if received is None:
                logging.warning(f"OTP email UID {uid.decode()} has an unreadable "
                                "INTERNALDATE — skipping it.")
                continue
            if received <= cutoff:
                key = (user, uid)
                if key not in _logged_skips:
                    _logged_skips.add(key)
                    logging.info(
                        f"Newest matching OTP email for {user} was received "
                        f"{datetime.fromtimestamp(received):%H:%M:%S}, before sign-in "
                        f"{datetime.fromtimestamp(since_epoch):%H:%M:%S} "
                        f"(-{CLOCK_SKEW_GRACE_S}s grace) — treated as stale; "
                        "waiting for a newer one.")
                break  # older UIDs are older still

            status, msg_data = imap.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            return _parse(raw, received)
        return None
    except Exception as e:
        logging.warning(f"IMAP fetch failed: {e}")
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _internal_date(imap, uid) -> Optional[float]:
    """The server's INTERNALDATE for a message, as epoch seconds (or None)."""
    try:
        status, data = imap.uid("FETCH", uid, "(INTERNALDATE)")
        if status != "OK" or not data or data[0] is None:
            return None
        raw = data[0] if isinstance(data[0], bytes) else data[0]
        tt = imaplib.Internaldate2tuple(raw)
        return time.mktime(tt) if tt else None
    except Exception:
        return None


def _parse(raw: bytes, received_epoch: float) -> OtpMail:
    """Extracts the plain-text body and the first image attachment."""
    msg = email.message_from_bytes(raw)

    body_text = ""
    html_text = ""
    image = None
    image_mime = ""
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain" and not body_text:
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="replace")
            except Exception:
                pass
        elif ctype == "text/html" and not html_text:
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html_text = payload.decode(charset, errors="replace")
            except Exception:
                pass
        elif ctype.startswith("image/") and image is None:
            try:
                image = part.get_payload(decode=True)
                image_mime = ctype
            except Exception:
                pass

    logging.debug(
        f"OTP email parsed: body={len(body_text)} chars, "
        f"html={len(html_text)} chars, "
        f"image={'yes (' + image_mime + ')' if image else 'no'}."
    )
    return OtpMail(
        body_text=body_text, image=image, image_mime=image_mime,
        received_epoch=received_epoch, html_text=html_text,
    )
