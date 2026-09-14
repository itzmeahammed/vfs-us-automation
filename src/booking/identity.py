"""Deciding WHICH client an invitation and a dashboard row refer to.

THE STAKES
----------
Getting this wrong books one client's appointment under another client's
passport. That is unrecoverable, costs a real person a real slot, and may burn
the VFS account. Against that, missing an invitation is a bad day.

The code encodes that asymmetry everywhere: **when in doubt, do nothing.** An
ambiguous match resolves to nothing and says so; it never picks a winner.

THE GOOD NEWS: THE JOIN IS EXACT
--------------------------------
This module was designed pessimistically, for a world where names were all we
had. Then the dashboard turned out to show the reference number, and it is the
SAME value the registration confirmation email carries (confirmed for Greece,
2026-09-02):

    confirmation email   "Unique Reference Number is GRC127086415238"
    dashboard row        "Group Reference Number - GRC127086415238"

So the real chain is:

    1. registration  -> store the reference on the journal row
    2. invitation    -> names a client; look up THEIR stored reference
    3. dashboard     -> select the row whose reference equals it   EXACT

Name matching therefore does a much smaller job than first feared: it picks a
client out of the handful on one account, and is then CONFIRMED by an exact
reference comparison. It is a narrowing step and a cross-check, not the thing
standing between us and a mis-booking.

Where names still matter on their own: a client registered before the reference
was captured, or a portal that hides it. Those fall back to name-only matching,
which is exactly when `require_unique` must refuse.

EVERYTHING HERE IS PURE
-----------------------
Plain strings in, plain values out. No page, no network, no journal. The
consequence is that the decision that must not be wrong can be exhaustively
tested offline — including the adversarial cases (two clients, one surname)
that are hard to produce against a live portal and unaffordable to get wrong.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Words that are not part of a name and would otherwise defeat a comparison.
_HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "mx",
})


# --------------------------------------------------------------------------- #
# Confidence                                                                   #
# --------------------------------------------------------------------------- #

class Confidence:
    """How sure we are that two identities are the same person.

    Ordered, so a policy can say "at least STRONG" rather than enumerating.
    """

    EXACT = "exact"
    """The reference numbers are equal, or every normalised name token matches.
    The only level safe to act on without a further check."""

    STRONG = "strong"
    """Surname plus first initial, and no other candidate fits. Enough to
    NARROW; not enough to commit without confirming against the reference."""

    WEAK = "weak"
    """A partial overlap. Reportable, never actionable."""

    NONE = "none"
    """No usable match."""

    _ORDER = {NONE: 0, WEAK: 1, STRONG: 2, EXACT: 3}

    @classmethod
    def rank(cls, level: str) -> int:
        return cls._ORDER.get(level, 0)

    @classmethod
    def at_least(cls, level: str, minimum: str) -> bool:
        return cls.rank(level) >= cls.rank(minimum)


# --------------------------------------------------------------------------- #
# Normalisation                                                                #
# --------------------------------------------------------------------------- #

def normalise_name(name: str) -> str:
    """A name reduced to a comparable form: sorted, lowercase, ASCII tokens.

    Four decisions, each from a real observed case:

      TOKEN SORTING. VFS greets 'Dear IRINA KONOVALOVA'; a client file stores
      first_name/last_name separately and some portals render 'SURNAME Given'.
      Sorting makes every ordering the same string, so word order can never
      cause a missed match.

      DIACRITIC FOLDING. Mail encoding and hand-typed client data disagree
      about accents more often than they agree.

      APOSTROPHES DELETED, HYPHENS SPLIT. An apostrophe sits INSIDE one name
      (O'BRIEN), so spacing it would split the name in two and stop it matching
      the same name typed plainly. A hyphen genuinely joins two parts (AL-FARSI)
      which portals render as one token, two, or with the hyphen dropped — so
      splitting is the form most likely to compare equal. (A real bug: the first
      version spaced both, and O'BRIEN never matched OBrien.)

      HONORIFICS DROPPED. 'Mr Ahmed Khan' and 'Ahmed Khan' are one person.

    Returns '' for anything with no usable tokens. Callers must treat that as
    'cannot compare' — never as 'matches everything'.
    """
    if not name:
        return ""

    folded = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    without_apostrophes = re.sub(r"['’ʼ`]", "", ascii_only)
    cleaned = re.sub(r"[^A-Za-z\s]", " ", without_apostrophes).lower()

    tokens = [t for t in cleaned.split() if t and t not in _HONORIFICS]
    return " ".join(sorted(tokens))


def name_tokens(name: str) -> List[str]:
    """The normalised tokens of a name, sorted."""
    normalised = normalise_name(name)
    return normalised.split() if normalised else []


def normalise_reference(reference: str) -> str:
    """A reference number reduced to a comparable form.

    Uppercased, with spaces and hyphens stripped. Three real formats exist —
    GRC127086415238, ITD125298020335, SWDB79923880977 — plus a hand-recorded
    WL-77231, and the dashboard may render one with spacing the email does not.
    Comparing raw strings would make those unequal.
    """
    if not reference:
        return ""
    return re.sub(r"[\s\-]", "", str(reference)).upper()


def references_match(left: str, right: str) -> bool:
    """Whether two reference numbers identify the same application."""
    a, b = normalise_reference(left), normalise_reference(right)
    return bool(a) and a == b


# --------------------------------------------------------------------------- #
# Scoring one pair                                                             #
# --------------------------------------------------------------------------- #

def score_names(left: str, right: str) -> str:
    """How confident we are that two NAMES are the same person.

    Deliberately conservative. In particular there is no fuzzy distance and no
    substring rule: 'Ahmed Khan' vs 'Ahmed Khan Ali' scores WEAK, not STRONG,
    because a middle name we have never seen might belong to a different person
    on the same account.
    """
    a_tokens, b_tokens = name_tokens(left), name_tokens(right)
    if not a_tokens or not b_tokens:
        return Confidence.NONE

    if a_tokens == b_tokens:
        return Confidence.EXACT

    a_set, b_set = set(a_tokens), set(b_tokens)
    shared = a_set & b_set
    if not shared:
        return Confidence.NONE

    # One side's tokens are wholly contained in the other's (a missing middle
    # name, typically). Suggestive, not conclusive.
    if a_set <= b_set or b_set <= a_set:
        return Confidence.STRONG if len(shared) >= 2 else Confidence.WEAK

    return Confidence.WEAK if len(shared) >= 1 else Confidence.NONE


# --------------------------------------------------------------------------- #
# Candidates and results                                                       #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Candidate:
    """One thing we might be looking at — a client, or a dashboard row.

    Plain data on purpose: a journal row, a client file and a scraped dashboard
    row all become this, so the matching logic never learns where any of them
    came from.
    """

    key: str
    """Whatever identifies this candidate to the caller — a registrant id, or a
    row index."""

    name: str = ""
    reference: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        """Short description for logs. Never includes the full name."""
        bits = [self.key]
        if self.reference:
            bits.append(self.reference)
        return " · ".join(bits)


@dataclass
class MatchResult:
    """The outcome of trying to identify one thing among candidates.

    `rejected` is populated even on success, so a log or an alert can say what
    else was considered and why it lost. A resolution nobody can explain after
    the fact is not one worth trusting.
    """

    candidate: Optional[Candidate] = None
    confidence: str = Confidence.NONE
    matched_on: str = ""
    reason: str = ""
    rejected: List[Candidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.candidate is not None

    def describe(self) -> str:
        if not self.resolved:
            return f"unresolved — {self.reason}"
        text = (f"{self.candidate.label()} "
                f"({self.confidence} on {self.matched_on})")
        if self.rejected:
            text += f"; {len(self.rejected)} other candidate(s) rejected"
        return text


# --------------------------------------------------------------------------- #
# Resolution                                                                   #
# --------------------------------------------------------------------------- #

def resolve(
    candidates: Sequence[Candidate],
    reference: str = "",
    name: str = "",
    min_confidence: str = Confidence.EXACT,
    require_unique: bool = True,
) -> MatchResult:
    """Identify ONE candidate, or refuse.

    Reference first, name second — a reference match is exact and needs no
    tie-breaking, so it short-circuits before any name is considered.

    `require_unique` is the safety rule and defaults ON: if two candidates tie
    at the best confidence, this returns NOTHING. Booking neither is a bad day;
    booking the wrong one is unrecoverable.

    `min_confidence` defaults to EXACT. Loosen it only where a wrong answer is
    cheap — narrowing a shortlist, say — never where a booking follows.
    """
    candidates = list(candidates or [])
    if not candidates:
        return MatchResult(reason="no candidates to match against")

    # --- reference: exact, and the moment it hits nothing else matters ----- #
    if reference:
        hits = [c for c in candidates if references_match(reference, c.reference)]
        if len(hits) == 1:
            return MatchResult(
                candidate=hits[0],
                confidence=Confidence.EXACT,
                matched_on="reference",
                reason=f"reference {normalise_reference(reference)} matched exactly",
                rejected=[c for c in candidates if c is not hits[0]],
            )
        if len(hits) > 1:
            # Two candidates carrying one reference is a data fault, not an
            # ordinary tie. Refuse loudly rather than picking.
            return MatchResult(
                reason=(f"reference {normalise_reference(reference)} matches "
                        f"{len(hits)} candidates — the data is inconsistent"),
                rejected=hits,
            )

    # --- name: scored, and required to be unambiguous ---------------------- #
    if not name:
        return MatchResult(
            reason=("no reference match and no name to fall back on"
                    if reference else "nothing to match on"),
            rejected=candidates,
        )

    scored = [(score_names(name, c.name), c) for c in candidates]
    usable = [(level, c) for level, c in scored
              if Confidence.at_least(level, min_confidence)]

    if not usable:
        best = max((Confidence.rank(level) for level, _ in scored), default=0)
        return MatchResult(
            reason=(f"no candidate reached '{min_confidence}' confidence "
                    f"(best was rank {best})"),
            rejected=candidates,
        )

    best_rank = max(Confidence.rank(level) for level, _ in usable)
    winners = [(level, c) for level, c in usable
               if Confidence.rank(level) == best_rank]

    if len(winners) > 1 and require_unique:
        # THE case this whole module exists for: several clients on one account
        # whose names are indistinguishable at this confidence.
        return MatchResult(
            reason=(f"{len(winners)} candidates tie at '{winners[0][0]}' "
                    f"confidence — refusing to guess"),
            rejected=[c for _, c in winners],
        )

    level, winner = winners[0]
    return MatchResult(
        candidate=winner,
        confidence=level,
        matched_on="name",
        reason=f"name matched at '{level}' confidence",
        rejected=[c for c in candidates if c is not winner],
    )


def verify(
    candidate: Candidate,
    reference: str = "",
    name: str = "",
    passport: str = "",
) -> MatchResult:
    """Confirm that an ALREADY-CHOSEN candidate really is who we think.

    The second half of "click, then check". Opening a dashboard row commits
    nothing, so a wrong click is free — provided it is DETECTED before the
    booking flow proceeds. This is that detection.

    Any ONE agreeing field is enough (portals show different things), but a
    field that is present on both sides and DISAGREES is fatal. Silence is
    tolerated; contradiction is not.
    """
    checks = (
        ("reference", reference, candidate.reference, references_match),
        ("name", name, candidate.name,
         lambda a, b: score_names(a, b) == Confidence.EXACT),
        ("passport", passport, candidate.extra.get("passport", ""),
         lambda a, b: normalise_reference(a) == normalise_reference(b)),
    )

    agreed: List[str] = []
    for label, expected, actual, compare in checks:
        if not expected or not actual:
            continue
        if compare(expected, actual):
            agreed.append(label)
        else:
            return MatchResult(
                confidence=Confidence.NONE,
                matched_on=label,
                reason=(f"{label} MISMATCH — expected and actual differ. "
                        f"This is not the right application."),
            )

    if not agreed:
        return MatchResult(
            confidence=Confidence.NONE,
            reason="nothing comparable: no shared field to verify against",
        )

    return MatchResult(
        candidate=candidate,
        confidence=Confidence.EXACT,
        matched_on="+".join(agreed),
        reason=f"verified on {', '.join(agreed)}",
    )
