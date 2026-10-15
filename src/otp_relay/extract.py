"""Pull the code out of an SMS body. PURE: text in, digits out.

No network, no clock, no files — so it can be tested against real SMS wording
instead of against a live bank, which is the only way this gets to be right
before a payment depends on it. Same discipline as src/inbox/matcher.py.

    ═══════════════════ THE PROBLEM IS NOT FINDING DIGITS ═══════════════════

A 3-D Secure SMS is mostly digits that are NOT the code:

    "AED 350.00 at VFS GLOBAL on 15/10/26. OTP 483920. Valid 5 min. Ref 7781."
     ^^^^^^                      ^^^^^^^^       ^^^^^^            ^^^^

An amount, a date, a reference, an expiry — and one code. Typing the wrong one
spends an attempt at the bank, and banks lock a card after three. So this
module is built to REFUSE rather than to guess:

    * a digit run is a candidate only if it is exactly the expected length and
      is not part of a longer number, a decimal, or a date
    * a candidate next to a code word ("OTP", "code", "password") wins
    * with NO code word and MORE THAN ONE candidate, it returns None

That last rule is the important one. Returning None costs a fallback to the
human. Returning the reference number costs a failed authentication on a
booking that is already paid for and cannot be re-paid.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

#: Words a bank puts next to the code. Lowercased, matched as whole words so
#: "encoded" does not count as "code".
CODE_WORDS: Tuple[str, ...] = (
    "otp", "code", "password", "passcode", "pin", "tac", "token",
    "verification", "authentication", "secure",
)

#: How far after a code word the code may sit and still be considered its.
#: "OTP for your transaction of AED 350 is 483920" is 30-odd characters, so
#: this is deliberately loose; precision comes from the candidate rules, not
#: from distance.
_NEAR = 40

_WORD = re.compile(r"[a-z]+")

#: Characters that make an adjacent digit run part of something else — a
#: decimal, a date, a thousands group. A TUPLE, not a string, and that is not
#: cosmetic: `"" in ".,/-:"` is True in Python, so a string here silently
#: rejected every code that started or ended a message ("Your OTP is 483920"
#: being the single commonest shape there is).
_SEPARATORS = (".", ",", "/", "-", ":")


def _code_word_positions(text: str, words: Sequence[str] = ()) -> List[int]:
    """Where each code word ENDS, in a lowercased body.

    Whole words only: "encoded" contains "code" and is not a code word.
    """
    wanted = tuple(w.lower() for w in words) or CODE_WORDS
    return [m.end() for m in _WORD.finditer(text) if m.group(0) in wanted]


def _candidates(text: str, digits: int) -> List[Tuple[int, str]]:
    """(position, value) for every digit run that could be a code.

    Rejects, in order, the four things that are reliably not codes:

        12345678   a longer run — the boundary check excludes it outright
        350.00     a decimal, either side of the point
        15/10/26   a date part, by its separator
        1,234      a thousands group
    """
    out: List[Tuple[int, str]] = []
    for match in re.finditer(r"\d+", text):
        run = match.group(0)
        if len(run) != digits:
            continue

        start, end = match.start(), match.end()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""

        # Attached to a separator that makes it part of a bigger number or a
        # date. A code is never written "350.00" or "15/10/26" or "1,234".
        if before in _SEPARATORS or after in _SEPARATORS:
            # A trailing full stop ends the sentence rather than a decimal:
            # "OTP 483920." is the normal shape. Only treat '.' as a decimal
            # point when a digit follows it.
            if after == "." and not (end + 1 < len(text)
                                     and text[end + 1].isdigit()):
                pass
            else:
                continue

        out.append((start, run))
    return out


def find_code(body: str, digits: int = 6,
              code_words: Sequence[str] = ()) -> Optional[str]:
    """The code in `body`, or None when it cannot be identified SAFELY.

    None is a legitimate, expected answer and the caller must handle it: an
    unparseable SMS falls back to a human, which is slow but correct. A guess
    is fast and wrong.

    `code_words` overrides CODE_WORDS for an issuer whose wording needs it,
    which is a line in a config file rather than a change here.
    """
    text = (body or "").lower()
    if not text:
        return None

    candidates = _candidates(text, digits)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    # More than one run of the right length. A code word has to break the tie.
    positions = _code_word_positions(text, code_words)
    if not positions:
        # AMBIGUOUS AND NOTHING TO DISAMBIGUATE WITH. Refuse. This is the
        # branch that protects the card from a reference number.
        return None

    # The candidate closest AFTER a code word. "after" matters: banks write
    # "OTP is 483920", not "483920 is OTP", and a reference number earlier in
    # the message must not win on raw distance.
    best, best_gap = None, None
    for pos, value in candidates:
        for word_end in positions:
            if pos < word_end:
                continue
            gap = pos - word_end
            if gap <= _NEAR and (best_gap is None or gap < best_gap):
                best, best_gap = value, gap

    return best


def looks_like_sender(sender: str, allowed: Sequence[str]) -> bool:
    """Is `sender` one the operator said to trust?

    An empty `allowed` trusts everyone, which is the sane default: a bank's
    sender ID is alphanumeric ("Mashreq", "VFS-GLOBAL"), varies by issuer and
    by country, and is not knowable before the first live run. The filter is
    there to be tightened once a route's real sender is observed.

    Matching is case-insensitive and substring-based, because an operator will
    write "mashreq" having seen "MashreqBK".
    """
    if not allowed:
        return True
    text = (sender or "").strip().lower()
    return any(a.strip().lower() in text for a in allowed if a.strip())
