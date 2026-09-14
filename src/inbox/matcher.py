"""Decide what a VFS email IS, and pull the few useful facts out of it.

PURE BY DESIGN. Every function here takes plain data and returns plain data: no
IMAP, no filesystem, no clock, no network. That is what lets the whole matching
layer be tested against saved .eml fixtures instead of against a live mailbox —
and matching is precisely the part that must be right before anything acts on
it. It is the same discipline slot_check.cascade_steps() holds for the dropdown
cascade.

WHAT A MATCHER IS
-----------------
A matcher is a JSON object in config/inbox/<ROUTE>.json:

    {
      "name": "waitlist_invitation",
      "classify": "invitation",
      "subject_contains": ["Slots available for booking"],
      "from_contains": ["vfshelpline.com"],
      "body_contains": ["are now available for booking"],
      "extract": { "applicant_name": "Dear\\\\s+([A-Z][A-Z\\\\s'-]+)," },
      "validity_hours": 48
    }

All the *_contains conditions given must hold (AND), and each is itself a list
of alternatives (OR). A condition that is absent is not tested — so a matcher
with only "subject_contains" is legal and matches on subject alone.

EXTRACTION IS ALWAYS OPTIONAL
-----------------------------
`extract` maps a field name to a regex whose FIRST GROUP is the value. A pattern
that does not match is not an error: it records `None` and the match still
stands. This matters more than it looks. The invitation carries no reference
number and only a greeting name, so identity is resolved later against the VFS
dashboard; nothing downstream may depend on an extraction having succeeded. A
country whose greeting reads "Dear Applicant" must still classify correctly.

WHY MATCHING IS DELIBERATELY DUMB
---------------------------------
Substring tests and a couple of regexes, no fuzzy scoring or heuristics. The
cost of a wrong classification is paid by a human reading a Telegram digest; the
cost of a clever rule nobody can predict is paid forever. When a country does
not fit, the answer is a line in its config file, not a smarter matcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Classifications                                                              #
# --------------------------------------------------------------------------- #

#: The client may now book. Carries a deadline (usually 48h). This is the one
#: that will eventually drive the booking flow.
INVITATION = "invitation"

#: A registration landed. Carries the Unique Reference Number, which is a second
#: independent source for a value the browser sometimes fails to capture.
CONFIRMATION = "confirmation"

#: Recognised as VFS mail, but not one of the above (newsletters, receipts,
#: password resets). Worth recording — an unmatched VFS mail in the digest is
#: how a new email type gets discovered.
OTHER = "other"

#: Nothing matched. Not necessarily VFS mail at all.
UNMATCHED = "unmatched"

#: Classifications a matcher may declare. UNMATCHED is produced by classify()
#: alone and can never be asked for.
VALID_CLASSIFICATIONS = frozenset({INVITATION, CONFIRMATION, OTHER})


class MatcherConfigError(ValueError):
    """A matcher definition is malformed. Raised at load time, never at match time."""


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Email:
    """One message, reduced to the parts matching cares about.

    Deliberately NOT an imaplib/email.Message wrapper: this is a plain value so
    a fixture, a hand-written dict in a test, and a real fetched message are all
    the same thing to everything downstream.

    `uid` and `mailbox` identify the message for the seen-state; `received_epoch`
    is the server's INTERNALDATE, used to date the 48-hour window rather than
    the moment we happened to read it.
    """

    subject: str = ""
    sender: str = ""
    body: str = ""            # plain text; HTML falls back to a tag-stripped form
    uid: str = ""
    mailbox: str = ""
    received_epoch: float = 0.0

    def haystack(self) -> str:
        """Subject + sender + body, for conditions that don't care which."""
        return f"{self.subject}\n{self.sender}\n{self.body}"


@dataclass(frozen=True)
class Match:
    """What one email turned out to be.

    `fields` holds whatever `extract` pulled out — always present as a dict,
    with a None value for any pattern that did not hit, so callers can read
    `match.fields["applicant_name"]` without first checking the key exists.
    """

    classification: str = UNMATCHED
    matcher_name: str = ""
    route: str = ""
    fields: Dict[str, Optional[str]] = field(default_factory=dict)
    validity_hours: Optional[int] = None

    @property
    def matched(self) -> bool:
        return self.classification != UNMATCHED

    @property
    def is_invitation(self) -> bool:
        return self.classification == INVITATION

    @property
    def is_confirmation(self) -> bool:
        return self.classification == CONFIRMATION

    def get(self, name: str) -> Optional[str]:
        """An extracted field, or None if absent or not extracted."""
        return self.fields.get(name)

    def summary(self) -> str:
        """One line for a log or a Telegram digest. Never includes the body."""
        if not self.matched:
            return "[unmatched]"
        head = f"[{self.classification}] {self.route or '?'} · {self.matcher_name}"
        shown = {k: v for k, v in self.fields.items() if v}
        if shown:
            head += " · " + ", ".join(f"{k}={v}" for k, v in sorted(shown.items()))
        return head


# --------------------------------------------------------------------------- #
# Condition evaluation                                                         #
# --------------------------------------------------------------------------- #

def _as_list(value: Any) -> List[str]:
    """Accepts a bare string or a list; always yields a list of strings.

    Config written by hand will use both forms, and rejecting the scalar would
    be a pointless papercut.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise MatcherConfigError(
        f"Expected a string or a list of strings, got {type(value).__name__}."
    )


def _contains_any(haystack: str, needles: List[str]) -> bool:
    """Case-insensitive OR over substrings. An empty needle list is 'not tested'."""
    if not needles:
        return True
    low = haystack.lower()
    return any(n.lower() in low for n in needles if n)


def _normalise_ws(text: str) -> str:
    """Collapses every whitespace run to one space.

    Mail arrives with hard-wrapped lines, so a phrase the matcher is looking for
    may be split across a newline in the body but not in the config. Matching on
    a whitespace-normalised copy makes 'are now available\\nfor booking' and
    'are now available for booking' the same string. Mirrors journal._normalise's
    reasoning, minus the lowercasing (which _contains_any does itself).
    """
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def validate_matcher(matcher: Dict[str, Any], where: str = "") -> None:
    """Raises MatcherConfigError if a matcher definition is unusable.

    Called at LOAD time so a typo is reported once, at startup, with a filename —
    rather than silently matching nothing for weeks. A matcher that quietly never
    fires is indistinguishable from 'no such email arrived', which is the exact
    failure mode this package exists to avoid.
    """
    prefix = f"{where}: " if where else ""

    if not isinstance(matcher, dict):
        raise MatcherConfigError(f"{prefix}each matcher must be an object.")

    name = matcher.get("name")
    if not name:
        raise MatcherConfigError(f"{prefix}matcher is missing \"name\".")

    classification = matcher.get("classify")
    if not classification:
        raise MatcherConfigError(f"{prefix}matcher '{name}' is missing \"classify\".")
    if classification not in VALID_CLASSIFICATIONS:
        raise MatcherConfigError(
            f"{prefix}matcher '{name}' has \"classify\": '{classification}'. "
            f"Valid: {', '.join(sorted(VALID_CLASSIFICATIONS))}."
        )

    conditions = [
        _as_list(matcher.get(key))
        for key in ("subject_contains", "from_contains", "body_contains", "contains")
    ]
    if not any(conditions):
        # A matcher with no conditions matches EVERY email, which would classify
        # the whole mailbox as one thing. Always a mistake, never a shorthand.
        raise MatcherConfigError(
            f"{prefix}matcher '{name}' has no conditions — it would match every "
            "email. Give it at least one of: subject_contains, from_contains, "
            "body_contains, contains."
        )

    extract = matcher.get("extract") or {}
    if not isinstance(extract, dict):
        raise MatcherConfigError(f"{prefix}matcher '{name}': \"extract\" must be an object.")
    for field_name, pattern in extract.items():
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as e:
            raise MatcherConfigError(
                f"{prefix}matcher '{name}', extract '{field_name}': "
                f"invalid regex {pattern!r}: {e}"
            ) from e
        if compiled.groups < 1:
            # The value is always group 1, so a pattern without one can only
            # ever record None — a silent no-op that looks like it works.
            raise MatcherConfigError(
                f"{prefix}matcher '{name}', extract '{field_name}': the pattern "
                f"needs a capture group around the value, e.g. "
                f"\"Reference Number is\\\\s+([A-Z0-9]+)\"."
            )

    hours = matcher.get("validity_hours")
    if hours is not None and (not isinstance(hours, int) or hours <= 0):
        raise MatcherConfigError(
            f"{prefix}matcher '{name}': \"validity_hours\" must be a positive integer."
        )


def validate_matchers(matchers: List[Dict[str, Any]], where: str = "") -> None:
    """Validates a whole list, and rejects duplicate names within it."""
    if not isinstance(matchers, list):
        raise MatcherConfigError(f"{where or 'matchers'}: \"matchers\" must be a list.")
    seen = set()
    for matcher in matchers:
        validate_matcher(matcher, where)
        name = matcher.get("name")
        if name in seen:
            raise MatcherConfigError(f"{where or 'matchers'}: duplicate matcher name '{name}'.")
        seen.add(name)


# --------------------------------------------------------------------------- #
# Extraction                                                                   #
# --------------------------------------------------------------------------- #

def extract_fields(email: Email, extract: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Runs each extract pattern; group 1, stripped, or None.

    Never raises. A pattern that does not match records None and the caller
    carries on — see the module docstring on why extraction must stay optional.
    """
    out: Dict[str, Optional[str]] = {}
    if not extract:
        return out

    text = _normalise_ws(email.haystack())
    for name, pattern in extract.items():
        try:
            found = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        except re.error:
            # Unreachable for config that went through validate_matcher(), but a
            # caller may hand-build a matcher. Degrade, never explode.
            out[name] = None
            continue
        if not found:
            out[name] = None
            continue
        try:
            value = found.group(1)
        except IndexError:
            out[name] = None
            continue
        cleaned = _normalise_ws(value).strip(" ,.;:-")
        out[name] = cleaned or None
    return out


# --------------------------------------------------------------------------- #
# The public entry point                                                       #
# --------------------------------------------------------------------------- #

def matches(email: Email, matcher: Dict[str, Any]) -> bool:
    """Whether one matcher's conditions all hold for this email."""
    subject = _normalise_ws(email.subject)
    sender = _normalise_ws(email.sender)
    body = _normalise_ws(email.body)
    everything = _normalise_ws(email.haystack())

    return (
        _contains_any(subject, _as_list(matcher.get("subject_contains")))
        and _contains_any(sender, _as_list(matcher.get("from_contains")))
        and _contains_any(body, _as_list(matcher.get("body_contains")))
        and _contains_any(everything, _as_list(matcher.get("contains")))
    )


def classify(email: Email, matchers: List[Dict[str, Any]], route: str = "") -> Match:
    """Classifies an email against an ORDERED list of matchers.

    FIRST MATCH WINS — order in the config file is meaningful. Put the specific
    matchers above the general ones: a broad "any VFS mail" catch-all listed
    first would swallow the invitation and the confirmation behind it.

    Returns an UNMATCHED Match rather than None when nothing fits, so callers
    never branch on None and an unmatched email still carries its route into the
    digest.
    """
    for matcher in matchers or []:
        if not matches(email, matcher):
            continue
        return Match(
            classification=matcher.get("classify", OTHER),
            matcher_name=matcher.get("name", ""),
            route=route,
            fields=extract_fields(email, matcher.get("extract") or {}),
            validity_hours=matcher.get("validity_hours"),
        )
    return Match(route=route)


def classify_all(
    email: Email, matchers_by_route: Dict[str, List[Dict[str, Any]]]
) -> Match:
    """Classifies against EVERY route's matchers, returning the best hit.

    A mailbox is not owned by one route — an account may hold waitlist entries
    for several countries, and the message itself is what says which portal it
    came from. So the watcher does not know the route in advance; it tries them
    all and lets the matching decide.

    TWO PASSES, AND THE ORDER IS THE WHOLE POINT
    --------------------------------------------
    Every route inherits the same generic catch-alls from _default (vfs_otp,
    vfs_other), which match ANY VFS mail regardless of country. A single pass in
    route order would let the alphabetically-first route's catch-all swallow a
    message that a later route matches SPECIFICALLY:

        an Italian invitation, tried against AE-GRC first, hits Greece's
        inherited 'vfs_other' and is reported as unrecognised Greek mail —
        never reaching AE-ITA's own waitlist_invitation, and never picking up
        Italy's 48-hour window instead of Greece's 36.

    That is silent and it applies the wrong deadline, so specific matchers must
    beat generic ones across ALL routes before any generic one is considered:

        pass 1   every route's SPECIFIC matchers  (classify != "other")
        pass 2   every route's catch-alls         (classify == "other")

    Within each pass, routes are tried in sorted order so a genuine tie resolves
    identically on every run and the digest stays reproducible.
    """
    def _pass(want_generic: bool) -> Optional[Match]:
        for route in sorted(matchers_by_route):
            subset = [
                m for m in matchers_by_route[route]
                if (m.get("classify") == OTHER) is want_generic
            ]
            found = classify(email, subset, route=route)
            if found.matched:
                return found
        return None

    return _pass(want_generic=False) or _pass(want_generic=True) or Match()
