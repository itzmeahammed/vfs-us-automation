"""The canonical list of what we check — built from `config/routes/*.json`.

`config/routes` is the source of truth. A combination's identity is the
normalised **(route, city, category, sub-category)**, deliberately NOT the
portal's display text, because VFS renames its dropdowns: the same real place
has arrived as 'Dubai', 'Netherlands Visa application center- Dubai' and
'Norway Visa Application Center - Dubai'. Keying on that text would put one
centre in the database three times and split its history three ways — exactly
the duplication this module exists to prevent.

City comes from `telegram_message._city`, which already knows how to pull
'Abu Dhabi' out of every spelling the portals use. Reusing it means the
dashboard and the Telegram reports can never disagree about what a centre is.

Labels still matter for reading old logs, where only the display text was
written. `label_aliases` maps every spelling we've seen back to one combo:
seeded from the route files, and extended by `resolve()` when it can work out
an unseen label with certainty. When it cannot, the label is parked in
`unmapped_labels` for a human — inventing a combo would be the duplication bug
wearing a different hat.
"""

import glob
import json
import logging
import os
import re
import sqlite3
from typing import Dict, List, Optional

from src.utils.telegram_message import DESTINATION_NAMES, _city

ROUTES_DIR = os.path.join("config", "routes")

# Route files that describe shared behaviour, not a portal.
_NON_ROUTE_KEYS = ("_default",)


def _norm(text: str) -> str:
    """Identity key for one field: lowercase, alphanumerics only.

    'Short Stay', 'short stay' and 'ShortStay' all collapse to 'shortstay', so a
    cosmetic change on the portal doesn't fork a combination's history.
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def combo_key(route: str, city: str, category: str, sub_category: str) -> str:
    return "|".join([
        (route or "").upper(),
        _norm(city),
        _norm(category),
        _norm(sub_category),
    ])


# What a combination is FOR. The portals never agree on wording, so this is
# derived from the category/sub-category text once, here, and stored.
TOURIST = "tourist"
BUSINESS = "business"
ANY_PURPOSE = "any"

_BUSINESS_WORDS = ("business", "corporate")
_TOURIST_WORDS = ("tourist", "tourism", "leisure", "visit")


def purpose(category: str, sub_category: str) -> str:
    """Classifies a combination as tourist, business, or any-purpose.

    The third group is the one that matters most and is easy to get wrong.
    'SCHENGEN', 'Short Stay', 'Schengen Visa', 'ShortStay', 'General
    Appointment', 'Prime Time' and 'Short Stay (any purpose)' are single
    appointment types that serve BOTH a tourist and a business applicant — VFS
    simply doesn't split them. They are not a third kind of client, they are
    availability that counts for either.

    That is why they are marked `any` rather than being forced into one side:
    Sweden, Switzerland, Germany, Greece, Italy and the Netherlands currently
    have nothing BUT any-purpose combinations, so filing them under 'tourist'
    would hide them from business clients, and filing them under neither would
    empty both tabs of the countries most worth selling.
    """
    text = f"{category} {sub_category}".lower()
    if any(word in text for word in _BUSINESS_WORDS):
        return BUSINESS
    if any(word in text for word in _TOURIST_WORDS):
        return TOURIST
    return ANY_PURPOSE


def visa_type(category: str, sub_category: str) -> str:
    """'Short Stay' + 'Tourist' -> 'Short Stay - Tourist'; drops a repeat.

    Several routes set category and sub-category to the same thing (Czechia
    'Tourism'/'Tourism', Sweden 'Short Stay'/'ShortStay'), which would otherwise
    read as 'Tourism - Tourism' on the dashboard.
    """
    parts: List[str] = []
    seen = set()
    for value in (category, sub_category):
        value = (value or "").strip()
        key = _norm(value)
        if not key or key in seen:
            continue
        seen.add(key)
        parts.append(value)
    return " - ".join(parts)


class ConfigCombo(dict):
    """One combination from a route file, in database shape.

    A dict subclass rather than a dataclass so it drops straight into sqlite3's
    named parameters, matching how the rest of this codebase passes records.
    """

    @property
    def key(self) -> str:
        return self["combo_key"]


def _route_codes(route: str):
    source, _, dest = route.partition("-")
    return source.upper(), dest.upper()


def load_combos(routes_dir: str = ROUTES_DIR) -> List[ConfigCombo]:
    """Reads every route file and returns its slot-check combinations.

    Disabled combinations are INCLUDED (with `enabled = 0`): they have history
    worth keeping, and one is often re-enabled later. A combination is skipped
    only when its centre is an unfilled placeholder — AE-DEU currently ships two
    entries whose centre reads 'TODO: the real Dubai centre option text', and
    storing those would put a fake centre on an agent's screen.
    """
    combos: List[ConfigCombo] = []
    seen: Dict[str, ConfigCombo] = {}
    order: Dict[str, int] = {}

    for path in sorted(glob.glob(os.path.join(routes_dir, "*.json"))):
        route = os.path.splitext(os.path.basename(path))[0]
        if route in _NON_ROUTE_KEYS:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logging.error(f"Slot registry: cannot read route file '{path}': {e}")
            continue

        source_code, dest_code = _route_codes(route)
        for entry in schema.get("slot_check", {}).get("combinations", []):
            centre = (entry.get("centre") or "").strip()
            if centre.upper().startswith("TODO"):
                logging.debug(
                    f"Slot registry: skipping placeholder combination in {route}: {centre!r}"
                )
                continue
            category = (entry.get("category") or "").strip()
            sub_category = (entry.get("sub_category") or "").strip()
            city = _city(centre)
            key = combo_key(route, city, category, sub_category)

            if key in seen:
                # Two config entries that mean the same combination (e.g. a
                # renamed centre left alongside its replacement). Keep one row;
                # enabled wins, so the live entry is the one that survives.
                if not entry.get("disabled"):
                    seen[key]["enabled"] = 1
                continue

            combo = ConfigCombo(
                combo_key=key,
                route=route.upper(),
                source_code=source_code,
                dest_code=dest_code,
                country_name=DESTINATION_NAMES.get(dest_code, dest_code),
                centre=centre,
                city=city,
                category=category,
                sub_category=sub_category,
                visa_type=visa_type(category, sub_category),
                purpose=purpose(category, sub_category),
                config_label=(entry.get("label") or "").strip(),
                enabled=0 if entry.get("disabled") else 1,
                # Position in this route file. Disabled entries still take a
                # slot: the bot skips them, but an OLD log was written when they
                # were live, and position is only useful if it means the same
                # thing then as now.
                config_order=order.get(route.upper(), 0),
            )
            order[route.upper()] = order.get(route.upper(), 0) + 1
            seen[key] = combo
            combos.append(combo)

    return combos


def label_variants(combo: ConfigCombo) -> List[str]:
    """Every display spelling this combination is known by, for log matching.

    Covers what `slot_check.combo_label` writes to the log ('Checking slot
    for: ...'), what `result_label` puts in run summaries, and the city-based
    form the Telegram report uses.
    """
    centre = combo["centre"]
    city = combo["city"]
    category = combo["category"]
    sub = combo["sub_category"]

    def join(sep: str, parts) -> str:
        return sep.join(p for p in parts if p)

    variants = [
        combo["config_label"],                      # route file's own label
        join(" / ", [centre, category, sub]),       # combo_label fallback
        join(" - ", [centre, category, sub]),       # result_label
        join(" - ", [city, category, sub]),         # city form
        join(" - ", [centre, sub]),
        join(" - ", [city, sub]),
        join(" - ", [centre, category]),
        join(" - ", [city, category]),
        centre,
        city,
    ]
    out, seen = [], set()
    for v in variants:
        key = _norm(v)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# ===== Database sync =======================================================


def sync(conn: sqlite3.Connection, routes_dir: str = ROUTES_DIR) -> Dict[str, int]:
    """Upserts the config's combinations into `combos` (+ their label aliases).

    Rows are matched on `combo_key`, so re-running this is safe and a portal
    renaming its centre text updates the existing row instead of adding one.
    Combinations that have left the config keep their history and are flagged
    `in_config = 0`, so the dashboard can show them as retired rather than
    pretending the past didn't happen.
    """
    combos = load_combos(routes_dir)
    stats = {"inserted": 0, "updated": 0, "retired": 0, "aliases": 0,
             "ambiguous": 0}

    known_keys = {c.key for c in combos}
    existing = {
        row["combo_key"]: row["id"]
        for row in conn.execute("SELECT id, combo_key FROM combos")
    }

    id_for: Dict[str, int] = {}
    for combo in combos:
        if combo.key in existing:
            conn.execute(
                "UPDATE combos SET route=:route, source_code=:source_code,"
                " dest_code=:dest_code, country_name=:country_name, centre=:centre,"
                " city=:city, category=:category, sub_category=:sub_category,"
                " visa_type=:visa_type, purpose=:purpose, config_label=:config_label,"
                " enabled=:enabled, config_order=:config_order,"
                " in_config=1 WHERE combo_key=:combo_key",
                combo,
            )
            stats["updated"] += 1
            combo_id = existing[combo.key]
        else:
            cur = conn.execute(
                "INSERT INTO combos (combo_key, route, source_code, dest_code,"
                " country_name, centre, city, category, sub_category, visa_type,"
                " purpose, config_label, enabled, config_order, in_config)"
                " VALUES (:combo_key, :route, :source_code, :dest_code,"
                " :country_name, :centre, :city, :category, :sub_category,"
                " :visa_type, :purpose, :config_label, :enabled, :config_order, 1)",
                combo,
            )
            combo_id = cur.lastrowid
            stats["inserted"] += 1

        id_for[combo.key] = combo_id

    # Aliases, in a second pass so ambiguity is known before anything is stored.
    #
    # A bare centre ('Abu Dhabi') is a label variant of EVERY category at that
    # centre. Inserting those one combo at a time and letting the primary key
    # drop the clashes does not reject the ambiguous alias — it awards it to
    # whichever combination happened to be inserted first, silently filing a
    # second category's readings under the first. Collecting the claims first is
    # what makes "more than one claimant" visible at all.
    claims: Dict[tuple, set] = {}
    named: Dict[tuple, set] = {}      # claimed as a route file's own `label`
    for combo in combos:
        route_code = combo["route"]
        own = _norm(combo["config_label"])
        if own:
            named.setdefault((route_code, own), set()).add(id_for[combo.key])
        for label in label_variants(combo):
            key = _norm(label)
            if key:
                claims.setdefault((route_code, key), set()).add(id_for[combo.key])

    for (route_code, key), owners in claims.items():
        # A route file's own `label` is an explicit decision and wins over a
        # variant merely derived from another combination's centre. Greece calls
        # one combination 'Greece Visa Application center-Dubai', which is also
        # the bare centre of the other Dubai combination — treating those as
        # equal claims would refuse a label that was never in doubt.
        owner = named.get((route_code, key)) or owners
        if len(owner) == 1:
            cur = conn.execute(
                "INSERT OR IGNORE INTO label_aliases (label_key, route, combo_id, origin)"
                " VALUES (?, ?, ?, 'config')",
                (key, route_code, next(iter(owner))),
            )
            stats["aliases"] += cur.rowcount
        else:
            # Refuse it, and clear any earlier run's first-come-first-served
            # guess (or an `inferred` alias learned from one). resolve() then
            # falls through to structured matching and, for a log line whose
            # position pins it down, to config order.
            conn.execute(
                "DELETE FROM label_aliases WHERE route = ? AND label_key = ?"
                " AND origin != 'manual'",
                (route_code, key),
            )
            stats["ambiguous"] += 1

    if existing:
        retired = [k for k in existing if k not in known_keys]
        if retired:
            conn.executemany(
                "UPDATE combos SET in_config=0, enabled=0 WHERE combo_key=?",
                [(k,) for k in retired],
            )
            stats["retired"] = len(retired)

    conn.commit()
    return stats


# ===== Label resolution (for reading logs) =================================


def _structured_match(conn: sqlite3.Connection, route: str, label: str) -> Optional[int]:
    """Last resort: work out a combo from what the label *contains*.

    Two hurdles, both required, because a wrong match is worse than no match —
    it files one centre's readings under another and quietly corrupts both:

      1. The candidate's CITY must appear in the label. Every centre spelling
         VFS uses names its city, so a label that doesn't is not about this
         centre (and 'Mars - Short Stay - Tourist' must never land on Dubai
         just because Dubai happens to be the route's only combination).
      2. The category or sub-category words must then pick out exactly ONE
         candidate. A tie stays unmapped.
    """
    rows = conn.execute(
        "SELECT id, city, category, sub_category FROM combos WHERE route = ?",
        (route.upper(),),
    ).fetchall()
    if not rows:
        return None

    low = (label or "").lower()
    candidates = [r for r in rows
                  if not r["city"] or r["city"].lower() in low]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["id"]

    key = _norm(label)
    scored = []
    for row in candidates:
        score = 0
        for field in ("category", "sub_category"):
            value = _norm(row[field])
            if value and value in key:
                score += 1
        scored.append((score, row["id"]))

    best = max(s for s, _ in scored)
    if best == 0:
        return None
    winners = [cid for s, cid in scored if s == best]
    return winners[0] if len(winners) == 1 else None


def ambiguous_candidates(conn: sqlite3.Connection, route: str,
                         label: str) -> List[sqlite3.Row]:
    """The combos a label could equally mean, in config order — else empty.

    This is the pre-August log shape: `slot_check` wrote only the centre, so
    'Abu Dhabi' was logged for BOTH of France's Abu Dhabi categories. Such a
    label is ambiguous by text and always will be; the only thing that separates
    the readings is the order they were taken in.

    Returns rows (id + `enabled`) in config order, and only when the label names
    a centre with several categories and carries NO wording that picks one of
    them out. If any candidate IS named by the label, this is an ordinary match
    (or an ordinary tie) and belongs to `_structured_match`, not here.
    """
    rows = conn.execute(
        "SELECT id, city, category, sub_category, enabled FROM combos"
        " WHERE route = ? ORDER BY config_order, id",
        (route.upper(),),
    ).fetchall()

    low = (label or "").lower()
    candidates = [r for r in rows if not r["city"] or r["city"].lower() in low]
    if len(candidates) < 2:
        return []

    key = _norm(label)
    for row in candidates:
        for field in ("category", "sub_category"):
            value = _norm(row[field])
            if value and value in key:
                return []
    return candidates


def resolve(conn: sqlite3.Connection, route: str, label: str,
            *, learn: bool = True, occurrence: Optional[int] = None,
            occurrence_total: Optional[int] = None) -> Optional[int]:
    """Maps a display label to a combo id, or None if it can't be placed.

    A successful structured match is written back as an `inferred` alias, so the
    same label costs one lookup next time instead of re-deriving.

    `occurrence` / `occurrence_total` are the label's 1-based position among its
    repeats inside one route run, and how many times it appeared in that run.
    They rescue the ambiguous pre-August labels: the bot works through a route
    file top to bottom, so the *n*th 'Abu Dhabi' of a run is the *n*th Abu Dhabi
    combination in the file. The total has to match the number of candidates
    before that reasoning holds — 85 runs in the current logs checked only one
    of France's two, and in those the position says nothing about which one, so
    they stay unmapped instead of being guessed.

    Position is deliberately NOT learned as an alias: it resolves one reading,
    not the label, which means something different the next time it appears.
    """
    route = (route or "").upper()
    key = _norm(label)
    if not key:
        return None

    row = conn.execute(
        "SELECT combo_id FROM label_aliases WHERE route = ? AND label_key = ?",
        (route, key),
    ).fetchone()
    if row:
        return row["combo_id"]

    combo_id = _structured_match(conn, route, label)
    if combo_id:
        if learn:
            conn.execute(
                "INSERT OR IGNORE INTO label_aliases (label_key, route, combo_id, origin)"
                " VALUES (?, ?, ?, 'inferred')",
                (key, route, combo_id),
            )
        return combo_id

    if occurrence and occurrence_total:
        candidates = ambiguous_candidates(conn, route, label)
        # Which candidates were actually walked? Normally the enabled ones — a
        # disabled combination is skipped, which is why Norway logs its Abu
        # Dhabi centre once per run and not twice. But an OLD log was written
        # when today's disabled row may still have been live, and then the run
        # holds one reading per combination in the file. The count the run
        # actually produced picks between those two readings of history; if it
        # matches neither, nothing here is knowable.
        for group in ([r for r in candidates if r["enabled"]], candidates):
            if (group and len(group) == occurrence_total
                    and 1 <= occurrence <= len(group)):
                return group[occurrence - 1]["id"]

    return None


def record_unmapped(conn: sqlite3.Connection, route: str, label: str, ts: str) -> None:
    """Parks a label we couldn't resolve, with a count and a first/last seen."""
    conn.execute(
        "INSERT INTO unmapped_labels (route, label, hits, first_seen, last_seen)"
        " VALUES (?, ?, 1, ?, ?)"
        " ON CONFLICT(route, label) DO UPDATE SET"
        " hits = hits + 1, last_seen = excluded.last_seen",
        ((route or "").upper(), label or "", ts, ts),
    )
