"""Free-text search (`q`) over rows an endpoint already returns.

WHY THIS IS CENTRAL AND NOT THREE LOCAL HELPERS
-----------------------------------------------
`q` exists so the dashboard can find one record among hundreds without paging.
It is the kind of convenience that quietly becomes a disclosure bug: the
obvious implementation is "match the needle against every value in the record",
and several of those values — passport number, date of birth, email, phone —
are masked precisely because the API will not show them. A `q` that matched a
masked value would let a caller confirm a guess one request at a time, which is
strictly worse than the masking it bypasses: masking at least makes the leak
visible.

So every caller must pass an explicit `fields` tuple, and the rule for that
tuple is one line long:

    a field may be searchable only if the SAME response already returns it.

`matches` cannot check that for the caller, so each call site names its fields
next to a comment saying which values it is deliberately leaving out. Three
copies of this function would mean three places for that discipline to lapse.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

#: A needle longer than this is not a search, and hashing it against every row
#: is work an unauthenticated-ish caller should not be able to ask for. The
#: routers also cap it via `max_length`; this is the backstop for direct calls.
MAX_TERMS = 8


def terms(q: Optional[str]) -> List[str]:
    """Split a raw query into lowercase terms, or [] when there is no query.

    An empty result means "no search" — callers should skip filtering entirely
    rather than treating it as "match nothing", so that `?q=` behaves like an
    omitted parameter instead of silently emptying the list.
    """
    return (q or "").strip().lower().split()[:MAX_TERMS]


def matches(row: Dict[str, Any], needle: Sequence[str],
            fields: Iterable[str]) -> bool:
    """True when EVERY term appears somewhere in the row's searchable fields.

    All terms must match, but each may match a different field — "ahmed che"
    finds Ahmed on AE-CHE and not Ahmed on AE-DEU, which is how someone types a
    search without knowing the field names. List values (combos, matched
    clients) are flattened in.

    Only `fields` are consulted. Anything not named is invisible to search,
    including nested values under a named key.
    """
    if not needle:
        return True
    haystack = " ".join(_flatten(row.get(name) for name in fields)).lower()
    return all(term in haystack for term in needle)


def _flatten(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value is None or isinstance(value, bool):
            # Booleans stringify to "True"/"False", which would make the term
            # "true" match half the fleet for no useful reason.
            continue
        if isinstance(value, (list, tuple, set)):
            out.extend(str(item) for item in value if item is not None)
        elif isinstance(value, dict):
            # Keys only: a dict's values are a different record's fields and
            # have not been cleared for search here.
            out.extend(str(key) for key in value)
        else:
            out.append(str(value))
    return out
