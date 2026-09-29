"""The company payment card, read from the environment. Never from a file.

    ══════════════ WHY THE ENVIRONMENT, AND ONLY THE ENVIRONMENT ══════════════

A card number in a config file is a card number in `git log` forever. This
repository already holds client records under version control, so the obvious
place to put payment details is the worst one available: `config/` is committed,
and a secret committed once is not removed by deleting it later.

The environment has the properties this needs and files do not:

    * it is per-machine and per-process, so a developer checkout never has it
    * it does not survive a `git add .`
    * it is already how this project supplies its other credentials
    * an operator can set it for one run and have it gone afterwards

    ══════════════════════ WHAT THIS MODULE REFUSES TO DO ══════════════════════

It does not persist the card, cache it to disk, put it in a BookingRun, or log
it. `Card.__repr__` and `Card.__str__` are overridden precisely because the
default ones would print the number the first time a card reached an exception
traceback — which is exactly when someone is copying output into a chat window.

The card values are registered with the redaction filter on load, so any code
that logs one anyway gets `[redacted]` rather than a PAN in `logs/booking.log`.

    ═══════════════════════════════ VARIABLES ═════════════════════════════════

    VFS_CARD_NUMBER     digits; spaces and dashes are accepted and stripped
    VFS_CARD_EXPIRY     MM/YY or MM/YYYY
    VFS_CARD_CVN        3 or 4 digits
    VFS_CARD_TYPE       optional: visa | mastercard | amex (inferred if unset)
    VFS_CARD_NAME       optional: the name printed on the card

`load()` returns None when VFS_CARD_NUMBER is unset, which is the ordinary
"payment is not configured on this machine" case and not an error — the caller
decides whether that is fatal. Anything present but malformed IS an error, and
is raised offline, before a browser starts: discovering a typo'd expiry date on
a live gateway means a client's booking is sitting on an abandoned payment page.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

log = logging.getLogger(__name__)


ENV_NUMBER = "VFS_CARD_NUMBER"
ENV_EXPIRY = "VFS_CARD_EXPIRY"
ENV_CVN = "VFS_CARD_CVN"
ENV_TYPE = "VFS_CARD_TYPE"
ENV_NAME = "VFS_CARD_NAME"

#: Card-type detection, by leading digits. Only what VFS's gateway offers.
_PREFIXES = (
    ("amex", ("34", "37")),
    ("mastercard", ("51", "52", "53", "54", "55", "22", "23", "24", "25",
                    "26", "27")),
    ("visa", ("4",)),
)


class CardError(Exception):
    """The card is configured but unusable. Deliberately not a subclass of the
    booking errors: a bad card is an operator problem, not a VFS one, and it
    must not be swallowed by a handler that retries VFS failures."""


@dataclass(frozen=True)
class Card:
    """A payment card. Never logged, never serialised, never persisted."""

    number: str
    expiry_month: str          # "MM"
    expiry_year: str           # "YYYY"
    cvn: str
    card_type: str = ""
    name: str = ""

    # ----------------------------------------------------------------- #
    # Everything below exists to stop the card reaching a log or a repr. #
    # ----------------------------------------------------------------- #

    def __repr__(self) -> str:
        return f"<Card {self.masked}>"

    def __str__(self) -> str:
        return self.masked

    @property
    def masked(self) -> str:
        """What is safe to print: type and last four digits."""
        tail = self.number[-4:] if len(self.number) >= 4 else "?"
        return f"{self.card_type or 'card'} ****{tail}"

    @property
    def expiry_mm_yy(self) -> str:
        return f"{self.expiry_month}/{self.expiry_year[-2:]}"

    def is_expired(self, today: Optional[date] = None) -> bool:
        """A card is valid THROUGH the last day of its expiry month."""
        today = today or date.today()
        year, month = int(self.expiry_year), int(self.expiry_month)
        return (year, month) < (today.year, today.month)

    def secret_values(self) -> List[str]:
        """The values the redaction filter must scrub from every log line.

        The CVN is deliberately EXCLUDED: three digits would match ordinary
        numbers all over the log (counts, timings, HTTP statuses) and turn it
        into noise. It is never logged in the first place, and the filter has a
        MIN_LENGTH guard that would drop it anyway.
        """
        return [v for v in (self.number, self.name) if v and len(v) >= 5]


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value or "")


def _luhn_ok(number: str) -> bool:
    """The Luhn check digit. Catches a transposed or mistyped digit offline.

    This is not validation that the card WORKS — only the issuer knows that.
    It catches the specific, common, and otherwise invisible failure of a
    number typed one digit wrong, which on a live gateway costs a booking.
    """
    total, alternate = 0, False
    for char in reversed(number):
        digit = ord(char) - 48
        if alternate:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alternate = not alternate
    return total % 10 == 0


def _infer_type(number: str) -> str:
    for name, prefixes in _PREFIXES:
        if number.startswith(prefixes):
            return name
    return ""


def _parse_expiry(raw: str) -> tuple:
    """(MM, YYYY) from MM/YY or MM/YYYY. Raises CardError on anything else."""
    text = (raw or "").strip().replace("-", "/").replace(".", "/")
    match = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{2}|\d{4})", text)
    if not match:
        raise CardError(
            f"{ENV_EXPIRY}: expected MM/YY or MM/YYYY, got {raw!r}.")

    month, year = match.group(1), match.group(2)
    if not 1 <= int(month) <= 12:
        raise CardError(f"{ENV_EXPIRY}: {month!r} is not a month (01-12).")

    if len(year) == 2:
        # A two-digit year is this century. Cards are not issued with 19xx
        # expiries and will not outlive 2099.
        year = f"20{year}"
    return month.zfill(2), year


def load(env: Optional[dict] = None, today: Optional[date] = None
         ) -> Optional[Card]:
    """The configured card, or None when none is set.

    None means "payment is not configured here" — an ordinary state on a
    developer machine. A card that IS set but malformed raises CardError,
    because silently proceeding without it would leave a client's booking
    abandoned on a payment page.
    """
    env = os.environ if env is None else env

    raw_number = env.get(ENV_NUMBER, "")
    if not str(raw_number).strip():
        return None

    number = _digits(str(raw_number))
    if not 12 <= len(number) <= 19:
        raise CardError(
            f"{ENV_NUMBER}: a card number is 12-19 digits, got "
            f"{len(number)}. (The value is not shown here on purpose.)")
    if not _luhn_ok(number):
        raise CardError(
            f"{ENV_NUMBER}: fails the Luhn check — a digit is mistyped or "
            "transposed. Re-enter it. (The value is not shown here on "
            "purpose.)")

    expiry_raw = env.get(ENV_EXPIRY, "")
    if not str(expiry_raw).strip():
        raise CardError(f"{ENV_EXPIRY} is not set, but {ENV_NUMBER} is.")
    month, year = _parse_expiry(str(expiry_raw))

    cvn = _digits(str(env.get(ENV_CVN, "")))
    if not cvn:
        raise CardError(f"{ENV_CVN} is not set, but {ENV_NUMBER} is.")
    if len(cvn) not in (3, 4):
        raise CardError(f"{ENV_CVN}: expected 3 or 4 digits, got {len(cvn)}.")

    card_type = (str(env.get(ENV_TYPE, "")).strip().lower()
                 or _infer_type(number))
    if not card_type:
        raise CardError(
            f"Could not tell the card type from its number, and {ENV_TYPE} is "
            "not set. Set it to visa, mastercard or amex.")

    # amex CVNs are 4 digits, everyone else's are 3. Mismatched almost always
    # means the wrong card's CVN was pasted.
    expected_cvn = 4 if card_type == "amex" else 3
    if len(cvn) != expected_cvn:
        raise CardError(
            f"{ENV_CVN}: a {card_type} CVN is {expected_cvn} digits, got "
            f"{len(cvn)}. Check the CVN matches the card number.")

    card = Card(number=number, expiry_month=month, expiry_year=year, cvn=cvn,
                card_type=card_type, name=str(env.get(ENV_NAME, "")).strip())

    if card.is_expired(today):
        raise CardError(
            f"The card expired {card.expiry_mm_yy}. Set a current card in "
            f"{ENV_NUMBER}/{ENV_EXPIRY}/{ENV_CVN}.")

    return card


def install_redaction(card: Optional[Card]) -> None:
    """Register the card's values with the log redaction filter.

    Call IMMEDIATELY after load(), before anything else runs. This is a safety
    net for code that logs a card by accident — not a licence to log one.
    """
    if card is None:
        return
    try:
        from src.waitlist import redaction

        redaction.add_values(card.secret_values())
    except Exception as e:                                  # noqa: BLE001
        # Fail open, as the redaction module itself does: losing an error
        # message during a live payment is worse than the residual risk on a
        # machine that already holds the card in its environment.
        log.warning(f"Could not install card redaction: {e}")
