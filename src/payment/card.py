"""The company payment card: the environment first, then a local card file.

    ═══════════════════════════════ THE SOURCES ═══════════════════════════════

Two, checked in this order — the first one that supplies a field wins:

    1. the environment          VFS_CARD_* (below)
    2. a local card file        config/card.local.txt, or $VFS_CARD_FILE

The environment is still the safer of the two and is still the right answer for
a scheduled task: it is per-process, it does not survive a `git add .`, and an
operator can set it for one run and have it gone afterwards.

The file exists because a human has to type the card in somewhere, and the
alternative to a guarded file is not "the environment" — it is a shell history
entry, a Task Scheduler argument, or a sticky note. A file can at least be
permission-checked, gitignore-checked and deleted. This module does all three
before it will read one.

    ═════════════════════ WHAT THE FILE IS GUARDED AGAINST ════════════════════

A card number in a committed file is a card number in `git log` forever, and a
secret committed once is not removed by deleting it later. So `load()` REFUSES
to read a card file that:

    * git does not ignore          (`git check-ignore` says it is committable)
    * other users can read         (POSIX mode with any group/other bit set)

Both refusals are hard errors, not warnings. A warning about a card file that
is about to be committed is a warning nobody reads until it is in the history.

    ══════════════════════ WHAT THIS MODULE REFUSES TO DO ══════════════════════

It does not persist the card, cache it to disk, put it in a BookingRun, or log
it. It never writes the card file, only reads it. `Card.__repr__` and
`Card.__str__` are overridden precisely because the default ones would print
the number the first time a card reached an exception traceback — which is
exactly when someone is copying output into a chat window.

No error raised here quotes a value. The operator knows what they typed; the
log does not need a copy.

The card values are registered with the redaction filter on load, so any code
that logs one anyway gets `[redacted]` rather than a PAN in `logs/booking.log`.

    ═══════════════════════════════ VARIABLES ═════════════════════════════════

    VFS_CARD_NUMBER     digits; spaces and dashes are accepted and stripped
    VFS_CARD_EXPIRY     MM/YY or MM/YYYY
    VFS_CARD_CVN        3 or 4 digits
    VFS_CARD_TYPE       optional: visa | mastercard | amex (inferred if unset)
    VFS_CARD_NAME       optional: the name printed on the card
    VFS_CARD_FILE       optional: a card file somewhere other than the default

    ══════════════════════════════ THE CARD FILE ══════════════════════════════

`KEY = value` lines, `#` comments, blank lines ignored. Keys are the variable
names above, with or without the `VFS_CARD_` prefix:

    number = 4111 1111 1111 1111
    expiry = 11/29
    cvn    = 123
    name   = COMPANY NAME

See `config/card.local.txt.example` for the tracked template.

`load()` returns None when no number is configured in either source, which is
the ordinary "payment is not configured on this machine" case and not an error
— the caller decides whether that is fatal. Anything present but malformed IS
an error, and is raised offline, before a browser starts: discovering a typo'd
expiry date on a live gateway means a client's booking is sitting on an
abandoned payment page.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


ENV_NUMBER = "VFS_CARD_NUMBER"
ENV_EXPIRY = "VFS_CARD_EXPIRY"
ENV_CVN = "VFS_CARD_CVN"
ENV_TYPE = "VFS_CARD_TYPE"
ENV_NAME = "VFS_CARD_NAME"
ENV_FILE = "VFS_CARD_FILE"

#: Where a card file lives unless VFS_CARD_FILE says otherwise. Under config/
#: because that is where an operator looks, and `.local.` because that is this
#: project's existing convention for "gitignored, holds real secrets".
DEFAULT_CARD_FILE = os.path.join("config", "card.local.txt")

#: The fields a card file may set, keyed by every spelling accepted for each.
#: Aliases exist because the operator typing this file is reading it off a
#: plastic card, not off this module — `cvv` and `cvc` are what is printed on
#: the card, and `card_number`/`card_cvn` match the pre-commit hook's key names.
_FILE_KEYS: Dict[str, str] = {
    "number": ENV_NUMBER, "card_number": ENV_NUMBER, "pan": ENV_NUMBER,
    "expiry": ENV_EXPIRY, "card_expiry": ENV_EXPIRY,
    "expiry_date": ENV_EXPIRY, "exp": ENV_EXPIRY,
    "cvn": ENV_CVN, "card_cvn": ENV_CVN, "cvv": ENV_CVN, "cvc": ENV_CVN,
    "security_code": ENV_CVN,
    "type": ENV_TYPE, "card_type": ENV_TYPE, "brand": ENV_TYPE,
    "name": ENV_NAME, "card_name": ENV_NAME, "cardholder": ENV_NAME,
    "cardholder_name": ENV_NAME, "name_on_card": ENV_NAME,
}
#: ...plus the full variable names, so one set of docs covers both sources.
_FILE_KEYS.update({v.lower(): v for v in _FILE_KEYS.values()})

#: `load(card_file=...)` default: work the path out from the environment.
#: Distinct from None, which means "do not read a file at all".
_DISCOVER = object()

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


# --------------------------------------------------------------------------- #
# The card file                                                                #
# --------------------------------------------------------------------------- #

def _git_ignores(path: str) -> Optional[bool]:
    """True/False if git has an opinion on `path`, None if it cannot be asked.

    None covers every "git is not the authority here" case — git is not
    installed, the file lives outside any work tree, the repo is unreadable.
    Those are fine: a card file kept outside the repo is the BEST case, and it
    must not be refused just because `git check-ignore` had nothing to say.
    """
    folder = os.path.dirname(os.path.abspath(path)) or "."
    # The bot runs headless under Task Scheduler, where a plain subprocess
    # flashes a console window on every slot check. Suppress it on Windows.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--", os.path.abspath(path)],
            cwd=folder, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=flags,
        )
    except Exception:                                           # noqa: BLE001
        return None
    # 0 = ignored, 1 = not ignored, 128 = not a work tree / bad path.
    return {0: True, 1: False}.get(proc.returncode)


def _check_committable(path: str) -> None:
    """Refuse a card file that git would let someone commit.

    This is the whole reason a file is tolerable at all. A PAN that reaches
    `git log` is not deleted by deleting the file afterwards — it survives in
    the object store and on every clone that ever fetched it. Catching it at
    read time is the last moment the problem is still cheap.
    """
    if _git_ignores(path) is False:
        raise CardError(
            f"{path} is NOT gitignored — committing it would put a card "
            "number in git history permanently, where deleting the file "
            "later does not remove it.\n"
            f"Add it to .gitignore (or move the file outside the repo and "
            f"point {ENV_FILE} at it), then try again.")


def _check_permissions(path: str) -> None:
    """Refuse a card file other users on the machine can read.

    POSIX only: on Windows `st_mode` reports a fiction (0o666 for anything
    writable) and the real answer lives in an ACL that `os.stat` cannot see,
    so a check there would be theatre. Windows operators get the gitignore
    check and the file-in-a-user-profile default, not this.
    """
    if os.name != "posix":
        return
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise CardError(
            f"{path} is readable by other users on this machine "
            f"(mode {mode:04o}). Run: chmod 600 {path}")


def _parse_card_file(text: str, path: str) -> Dict[str, str]:
    """`KEY = value` lines into the same shape as the environment.

    Errors name the KEY and the line number and never the value — a malformed
    card file is exactly the situation where someone pastes the error into a
    chat window asking what it means.
    """
    values: Dict[str, str] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue

        # Split on whichever of = or : comes first, so both `cvn = 123` and
        # `cvn: 123` work and an expiry's own slash is never involved.
        positions = [line.find(c) for c in "=:" if line.find(c) >= 0]
        if not positions:
            raise CardError(
                f"{path} line {lineno}: expected `key = value`, found a line "
                "with no separator. (The line is not shown here on purpose.)")
        split_at = min(positions)

        key = line[:split_at].strip().lower().replace("-", "_").replace(
            " ", "_")
        value = line[split_at + 1:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        field = _FILE_KEYS.get(key)
        if field is None:
            raise CardError(
                f"{path} line {lineno}: unknown key {key!r}. Valid keys are "
                "number, expiry, cvn, type, name.")
        if field in values:
            raise CardError(
                f"{path} line {lineno}: {key!r} is set twice. Which card is "
                "meant is not something this should guess at.")
        if value:
            values[field] = value

    return values


def read_card_file(path: str, required: bool = True) -> Dict[str, str]:
    """A card file's contents, as a VFS_CARD_*-keyed dict.

    `required=False` turns a missing file into `{}` — that is the default-path
    case, where "no card file here" is an ordinary state. An operator who
    named a path explicitly gets an error instead, because a typo'd
    VFS_CARD_FILE must not look identical to no card at all.
    """
    if not os.path.isfile(path):
        if required:
            raise CardError(
                f"No card file at {path}. Create it (see "
                "config/card.local.txt.example) or unset "
                f"{ENV_FILE} to fall back to the {ENV_NUMBER} variables.")
        return {}

    # Both guards run BEFORE the read: if the file is exposed, the fix is to
    # change that, not to use it once more and fix it afterwards.
    _check_committable(path)
    _check_permissions(path)

    try:
        # utf-8-sig: Notepad writes a BOM, and a BOM on the first key turns
        # `number` into `﻿number` and the error into a riddle.
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise CardError(f"Could not read {path}: {exc}") from exc

    return _parse_card_file(text, path)


def resolve_card_file(env: dict, explicit_env: bool) -> Optional[str]:
    """Which card file `load()` should read, or None for "do not read one".

    The default path is only discovered for the REAL environment. When a
    caller passes an env dict it is saying "this mapping is the configuration"
    — a test that does so must not start depending on whether the machine
    running it happens to have a card file on disk.
    """
    named = str(env.get(ENV_FILE, "")).strip()
    if named:
        return named
    return None if explicit_env else DEFAULT_CARD_FILE


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def load(env: Optional[dict] = None, today: Optional[date] = None,
         card_file: Any = _DISCOVER) -> Optional[Card]:
    """The configured card, or None when none is set.

    None means "payment is not configured here" — an ordinary state on a
    developer machine. A card that IS set but malformed raises CardError,
    because silently proceeding without it would leave a client's booking
    abandoned on a payment page.

    Fields come from the environment first and the card file second, merged
    per field: a VFS_CARD_CVN exported for one run overrides the file's CVN
    without the operator having to edit or restore the file around it.

        env         a mapping to use INSTEAD of os.environ. Passing one also
                    turns off card-file discovery (see resolve_card_file).
        card_file   an explicit path, or None for "no file, environment only".
    """
    explicit_env = env is not None
    env = os.environ if env is None else env

    if card_file is _DISCOVER:
        card_file = resolve_card_file(env, explicit_env)
        # A file found by discovery may simply not be there; one the operator
        # named through VFS_CARD_FILE must be.
        required = bool(str(env.get(ENV_FILE, "")).strip())
    else:
        required = card_file is not None

    values: Dict[str, str] = {}
    if card_file:
        values.update(read_card_file(str(card_file), required=required))
        if values:
            log.debug(f"card fields read from {card_file}")

    for key in (ENV_NUMBER, ENV_EXPIRY, ENV_CVN, ENV_TYPE, ENV_NAME):
        from_env = env.get(key)
        if from_env is not None and str(from_env).strip():
            values[key] = str(from_env)

    raw_number = values.get(ENV_NUMBER, "")
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

    # Where to tell the operator to look. Naming only the variables would send
    # someone who filled in a card file off editing the wrong thing.
    where = f" (checked the environment and {card_file})" if card_file else ""

    expiry_raw = values.get(ENV_EXPIRY, "")
    if not str(expiry_raw).strip():
        raise CardError(
            f"{ENV_EXPIRY} is not set, but {ENV_NUMBER} is.{where}")
    month, year = _parse_expiry(str(expiry_raw))

    cvn = _digits(str(values.get(ENV_CVN, "")))
    if not cvn:
        raise CardError(f"{ENV_CVN} is not set, but {ENV_NUMBER} is.{where}")
    if len(cvn) not in (3, 4):
        raise CardError(f"{ENV_CVN}: expected 3 or 4 digits, got {len(cvn)}.")

    card_type = (str(values.get(ENV_TYPE, "")).strip().lower()
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
                card_type=card_type,
                name=str(values.get(ENV_NAME, "")).strip())

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
