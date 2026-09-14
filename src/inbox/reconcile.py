"""Settle uncertain journal rows using VFS's own confirmation emails.

THE PROBLEM THIS SOLVES
-----------------------
register.py writes a 'pending' row, fsyncs it, and only then clicks the
committing button. If the page dies between the click and reading the
confirmation, the run ends 'unknown': a submit that MAY have landed. That row
then blocks the client from being retried — correctly, because a duplicate
registration is worse than a delayed one — until a human logs into the portal
and looks.

But VFS already told us the answer. "Successfully Added to Waitlist" arrives at
the account mailbox carrying the applicant's name and the Unique Reference
Number. That is an independent second source for exactly the fact the browser
failed to capture.

So this module reads those emails and settles the rows the browser could not:

    unknown/pending  + a matching confirmation email  ->  success (with the ref)
    success, no ref  + a matching confirmation email  ->  reference backfilled

VALUE INDEPENDENT OF BOOKING
----------------------------
This is worth running even if the post-invitation booking flow is never built.
It converts a class of "a human must go and check the portal" into "already
answered", using mail that arrives whether or not anyone is watching for it.

MATCHING IS CONSERVATIVE, AND STAYS THAT WAY
--------------------------------------------
A reconciliation writes a SUCCESS row, and a wrong one would mark a client as
registered when they are not — they would then never be retried, and would
silently miss their appointment. That is a worse failure than leaving the row
dangling for a human. So:

  * route must match, and the email must be a 'confirmation'
  * the client is identified by their name in the greeting, normalised
  * AMBIGUITY NEVER RESOLVES. If a confirmation could belong to two dangling
    clients, it settles neither and says so.
  * a confirmation carrying no usable name settles nothing

Nothing here contacts VFS or opens a browser: it reads mail and the journal, and
appends to the journal. Proposals are computed separately from being applied
(`plan()` vs `apply()`) so `--dry-run` shows exactly what would happen.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.waitlist.result import Status, WaitlistResult

log = logging.getLogger(__name__)

#: Words that are not part of a name and would otherwise defeat a comparison.
_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"})


# --------------------------------------------------------------------------- #
# Name normalisation                                                           #
# --------------------------------------------------------------------------- #

def normalise_name(name: str) -> str:
    """A name reduced to a comparable form: sorted, lowercase, ASCII tokens.

    Sorting the tokens is the important part. VFS greets 'Dear IRINA
    KONOVALOVA'; a client file may store first_name/last_name in either order,
    and some portals render 'SURNAME Given'. Sorting makes all of those the same
    string, so ordering can never cause a missed match.

    Diacritics are folded (KONOVÁLOVÁ -> konovalova) because mail encoding and
    hand-typed client data disagree about accents more often than not.

    Returns '' for anything with no usable tokens — callers treat that as
    'cannot compare', never as 'matches everything'.

    NOTE: this deliberately duplicates none of journal._normalise, which is
    about whitespace in combo labels. It will move to src/booking/identity.py
    when the booking flow needs confidence scoring on top of it; the shape here
    is the base case.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))

    # Apostrophes are DELETED, not spaced. They sit inside a single name
    # (O'BRIEN, D'SOUZA), so replacing one with a space would split it into two
    # tokens and stop it matching the same name typed without the apostrophe —
    # which is how client files usually hold it. Every other separator becomes a
    # space, because a hyphen genuinely does join two name parts (AL-FARSI, and
    # double-barrelled surnames) and either half may be dropped by a portal.
    without_apostrophes = re.sub(r"['’ʼ`]", "", ascii_only)
    cleaned = re.sub(r"[^A-Za-z\s]", " ", without_apostrophes).lower()

    tokens = [t for t in cleaned.split() if t and t not in _HONORIFICS]
    return " ".join(sorted(tokens))


def names_match(left: str, right: str) -> bool:
    """Whether two names refer to the same person, conservatively.

    Exact match on the normalised form only. Nothing fuzzy: no initials, no
    substrings, no edit distance. This decision writes a 'registered' record,
    and a false positive means a client silently never gets retried.
    """
    a, b = normalise_name(left), normalise_name(right)
    return bool(a) and a == b


def client_name(registrant) -> str:
    """A client's full name from their file, for comparison against an email."""
    for combined in ("full_name", "name"):
        value = registrant.get(combined)
        if value:
            return str(value)
    first = registrant.get("first_name") or ""
    last = registrant.get("last_name") or ""
    return f"{first} {last}".strip()


# --------------------------------------------------------------------------- #
# Proposals                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class Proposal:
    """One journal row a confirmation email could settle.

    Built even when it will NOT be applied (`blocked_reason` set), so the CLI
    can explain why a dangling row stayed dangling instead of silently doing
    nothing.
    """

    row: dict
    registrant_id: str
    route: str
    combo: str
    reference: Optional[str] = None
    new_status: str = ""
    action: str = ""              # human-readable: what would change
    blocked_reason: str = ""

    @property
    def will_apply(self) -> bool:
        return bool(self.new_status) and not self.blocked_reason

    def describe(self) -> str:
        head = f"{self.route} · {self.combo} · {self.registrant_id}"
        if self.blocked_reason:
            return f"[skip]    {head} — {self.blocked_reason}"
        return f"[{self.new_status:7}] {head} — {self.action}"


def _confirmations(observations) -> List:
    """Just the confirmation observations, newest first.

    Newest first because if a client somehow has two confirmations, the most
    recent is the one describing their current entry.
    """
    found = [o for o in observations if o.match.is_confirmation]
    return sorted(found, key=lambda o: o.email.received_epoch or 0, reverse=True)


def plan(observations, registrants=None) -> List[Proposal]:
    """Work out which journal rows the given emails could settle. Writes nothing.

    `observations` are inbox watcher Observations; `registrants` defaults to
    every configured client. Returns a proposal per row that a confirmation
    touches — including ones that cannot be applied, with the reason.
    """
    from src.waitlist import journal
    from src.waitlist import registrant as registrant_mod

    if registrants is None:
        registrants = registrant_mod.load_all(skip_invalid=True)

    by_id = {r.id: r for r in registrants}
    proposals: List[Proposal] = []

    # Latest row per (route, combo, client) — only the current state matters.
    latest: Dict[tuple, dict] = {}
    for row in journal.entries():
        key = (
            str(row.get("route") or "").upper(),
            " ".join(str(row.get("combo") or "").split()).lower(),
            str(row.get("registrant_id") or "").lower(),
        )
        latest[key] = row

    for observation in _confirmations(observations):
        email_name = observation.match.get("applicant_name")
        reference = observation.match.get("reference")
        route = (observation.route or "").upper()

        if not email_name:
            # 'Dear Applicant' — nothing to identify the client by. The email is
            # still logged by the digest; it just cannot settle anything.
            continue

        # Which configured clients does this name fit?
        candidates = [
            client for client in registrants
            if names_match(email_name, client_name(client))
            and (not route or (client.get("route") or "").upper() == route)
        ]

        if len(candidates) > 1:
            for client in candidates:
                proposals.append(Proposal(
                    row={}, registrant_id=client.id, route=route, combo="",
                    blocked_reason=(
                        f"'{email_name}' matches {len(candidates)} clients "
                        f"({', '.join(c.id for c in candidates)}) — ambiguous, "
                        "resolving none"
                    ),
                ))
            continue

        if not candidates:
            continue

        client = candidates[0]

        # Every row for this client on this route that a confirmation could settle.
        for (row_route, row_combo, row_client), row in latest.items():
            if row_client != client.id.lower():
                continue
            if route and row_route != route:
                continue

            status = row.get("status")
            has_reference = bool(row.get("vfs_reference"))

            if status in Status.NEEDS_ATTENTION:
                proposals.append(Proposal(
                    row=row, registrant_id=client.id,
                    route=row.get("route", ""), combo=row.get("combo", ""),
                    reference=reference, new_status=Status.SUCCESS,
                    action=(
                        f"'{status}' settled by VFS's confirmation email"
                        + (f" (ref {reference})" if reference else
                           " (email carried no reference)")
                    ),
                ))
            elif status == Status.SUCCESS and not has_reference and reference:
                proposals.append(Proposal(
                    row=row, registrant_id=client.id,
                    route=row.get("route", ""), combo=row.get("combo", ""),
                    reference=reference, new_status=Status.SUCCESS,
                    action=f"backfill the missing reference ({reference})",
                ))

    return proposals


def apply(proposals: List[Proposal]) -> int:
    """Appends a settling row for each applicable proposal. Returns how many.

    Append-only, like every other journal write: the original 'unknown' row
    stays, so the audit trail shows what happened and that a later email settled
    it. Readers take the latest row per triple, so appending is what changes the
    state.
    """
    from src.waitlist import journal

    applied = 0
    for proposal in proposals:
        if not proposal.will_apply:
            continue

        original = WaitlistResult.from_dict(proposal.row)
        original.status = proposal.new_status
        if proposal.reference:
            original.vfs_reference = proposal.reference
        original.reason = (
            f"reconciled from VFS confirmation email"
            + (f" (ref {proposal.reference})" if proposal.reference else "")
        )
        original.finish(proposal.new_status)

        journal.append(original)
        applied += 1
        log.info(f"Reconciled: {proposal.describe()}")

    return applied


def reconcile(observations, dry_run: bool = True, registrants=None) -> List[Proposal]:
    """Plan, and apply unless dry_run. Returns the proposals either way."""
    proposals = plan(observations, registrants=registrants)
    if not dry_run:
        apply(proposals)
    return proposals
