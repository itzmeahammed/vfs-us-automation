"""Telegram message templates for the VFS slot checker.

Edit the formatting here — this is the single place that decides what the slot
report and failure-alert messages look like. It is route-aware: every message
shows the destination's flag and a link to that portal's visa-center site, so
adding new routes does not silently mislabel messages.

The functions return a plain string; sending is done by `src.utils.telegram`.
Plain URLs render as clickable links in Telegram, so no HTML/Markdown is needed.
"""

# Destination code -> flag emoji shown before each slot line.
# Add an entry when you add a new route (falls back to no flag if missing).
DESTINATION_FLAGS = {
    "MT": "🇲🇹", "MLT": "🇲🇹",       # Malta
    "LUX": "🇱🇺", "LU": "🇱🇺",       # Luxembourg
    "CHE": "🇨🇭", "CH": "🇨🇭",       # Switzerland
    "DNK": "🇩🇰", "DK": "🇩🇰",       # Denmark
    "HUN": "🇭🇺", "HU": "🇭🇺",       # Hungary
    "CZE": "🇨🇿", "CZ": "🇨🇿",       # Czech Republic
    "ITA": "🇮🇹", "IT": "🇮🇹",       # Italy
    "FRA": "🇫🇷", "FR": "🇫🇷",       # France
    "GRC": "🇬🇷", "GR": "🇬🇷",       # Greece
    "DEU": "🇩🇪", "DE": "🇩🇪",       # Germany
    "NOR": "🇳🇴", "NO": "🇳🇴",       # Norway
    "NLD": "🇳🇱", "NL": "🇳🇱",       # Netherlands
}

# Destination code -> friendly country name, inserted into each label so the
# message reads "Abu Dhabi - Hungary - Short Stay - Business". Add an entry when
# you add a new route (falls back to the raw code if missing).
DESTINATION_NAMES = {
    "MT": "Malta", "MLT": "Malta",
    "LUX": "Luxembourg", "LU": "Luxembourg",
    "CHE": "Switzerland", "CH": "Switzerland",
    "DNK": "Denmark", "DK": "Denmark",
    "HUN": "Hungary", "HU": "Hungary",
    "CZE": "Czech Republic", "CZ": "Czech Republic",
    "ITA": "Italy", "IT": "Italy",
    "FRA": "France", "FR": "France",
    "GRC": "Greece", "GR": "Greece",
    "DEU": "Germany", "DE": "Germany",
    "NOR": "Norway", "NO": "Norway",
    "NLD": "Netherlands", "NL": "Netherlands",
}


def _flag(dest_code: str) -> str:
    """Flag emoji for a destination code, or '' if unknown (with no trailing space)."""
    return DESTINATION_FLAGS.get((dest_code or "").upper(), "")


def _country(dest_code: str) -> str:
    """Friendly country name for a destination code, or the raw code if unknown."""
    return DESTINATION_NAMES.get((dest_code or "").upper(), (dest_code or "").upper())


def _label_with_country(label: str, dest_code: str) -> str:
    """
    Rewrites a combo label as 'Centre - Country - SubCategory', dropping the
    middle 'category' segment (e.g. 'Short Stay').

    'Abu Dhabi - Short Stay - Business'  -> 'Abu Dhabi - Hungary - Business'
    'Dubai - SCHENGEN'                   -> 'Dubai - Hungary - SCHENGEN'
    'Abu Dhabi'                          -> 'Abu Dhabi - Hungary'
    """
    country = _country(dest_code)
    parts = [p.strip() for p in label.split(" - ") if p.strip()]
    if len(parts) >= 3:
        # centre - <category dropped> - sub  ->  centre - country - sub
        return f"{parts[0]} - {country} - {parts[-1]}"
    if len(parts) == 2:
        # centre - sub (no category)  ->  centre - country - sub
        return f"{parts[0]} - {country} - {parts[1]}"
    # single part (just a centre)
    return f"{parts[0]} - {country}" if parts else country


import re

# A combination has a real, actionable slot only if its banner contains a date
# (e.g. '... is : 11-08-2026'). 'No slot message shown' and 'Could not select ...'
# have no date, so they are filtered out — we only message about real slots.
_DATE_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")


def _has_slot(message: str) -> bool:
    """True if a combination's banner text contains an actual slot date."""
    return bool(_DATE_RE.search(message or ""))


def _report_label(combo: dict, dest_code: str) -> str:
    """Header for a slot line, built from the combo's STRUCTURED fields so it
    shows the centre, the country, then the category and sub-category:

      'Abu Dhabi - France - Short Stay - Business'
      'Temporary Enrolment Location - Damac Hills - France - Short Stay (any purpose)'

    Reads centre / category / sub_category directly (never by splitting a joined
    label) — the centre itself can contain ' - ' (e.g. the Damac Hills location),
    which the old string-splitting mangled into 'Temporary ... - France - Damac
    Hills'. Empty fields are simply omitted.
    """
    country = _country(dest_code)
    centre = (combo.get("centre") or combo.get("label") or "").strip()
    parts = [f"{centre} - {country}" if centre else country]
    for key in ("category", "sub_category"):
        val = (combo.get(key) or "").strip()
        if val:
            parts.append(val)
    return " - ".join(parts)


def slot_report(source_code: str, dest_code: str, entries: list, login_url: str = "") -> str:
    """
    Builds the slot-report message for ONE route — ONLY for combinations that
    actually have a slot.

    Combinations with no availability or a config error (no date in their text)
    are dropped. If NONE of the combinations have a slot, this returns an empty
    string and the caller should send nothing.

    Args:
        source_code / dest_code: e.g. 'AE' / 'DNK'.
        entries: list of (combo_dict, message) tuples — one per combination
            checked. The combo dict (centre/category/sub_category) is what lets
            the header show the category + sub-category, not just the centre.
        login_url: the portal's login URL, shown as a clickable link.

    Returns:
        The formatted message string, or "" if there are no slots to report.
    """
    flag = _flag(dest_code)
    prefix = f"{flag} " if flag else ""

    # Keep only combinations that actually have a slot date.
    available = [(combo, message) for combo, message in entries if _has_slot(message)]
    if not available:
        return ""  # nothing available -> caller sends no message

    lines = []
    for combo, message in available:
        lines.append(f"{prefix}{_report_label(combo, dest_code)}:")
        # `message` may hold SEVERAL banner lines (one per applicant count) —
        # indent each so multi-slot combos read cleanly.
        for ln in message.splitlines():
            if ln.strip():
                lines.append(f"  {ln.strip()}")
        lines.append("")
    body = "\n".join(lines).strip()
    return body


# Per-route status -> icon for the run summary.
_STATUS_ICON = {
    "OK": "✅", "FAILED": "❌", "STOPPED": "🔑", "GEO": "⛔", "SKIPPED": "⏭️",
    "LOCKED": "🔒", "RESTRICTED": "🚫", "BLOCKED": "🔴", "PAUSED": "💤",
}


def _short(text: str, limit: int = 110) -> str:
    """Collapse whitespace and truncate a (possibly multi-line) error for a summary line."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_summary(outcomes: list, account: str, timestamp: str) -> str:
    """
    Build the compact, one-line-per-route run summary sent to the summary chat
    after EVERY run — a monitoring digest of each URL's status.

    Args:
        outcomes: list of dicts, each with keys: source, dest, status
            ('OK'|'FAILED'|'STOPPED'|'GEO'|'SKIPPED'), attempts, error, slots.
        account: masked account used this hour, e.g. 'pa***@travnook.com (cred 7/10)'.
        timestamp: run time, e.g. '2026-07-04 12:29'.

    Returns:
        The formatted summary string (always non-empty).
    """
    header = f"🕐 {timestamp}"
    if account:
        # Legacy single-account header; with per-route rotation the account is
        # shown on each route's own line instead.
        header += f" · 👤 {account}"
    lines = ["📋 VFS Slot-Check Summary", header, ""]
    counts = {"OK": 0, "FAILED": 0, "STOPPED": 0, "GEO": 0, "SKIPPED": 0,
              "LOCKED": 0, "RESTRICTED": 0, "BLOCKED": 0, "PAUSED": 0}
    total_slots = 0

    for o in outcomes:
        status = o.get("status", "FAILED")
        counts[status] = counts.get(status, 0) + 1
        flag = _flag(o.get("dest"))
        prefix = f"{flag} " if flag else ""
        route = f"{o.get('source')}-{o.get('dest')}"
        icon = _STATUS_ICON.get(status, "•")
        head = f"{prefix}{_country(o.get('dest'))} ({route}): {icon} "
        slots = o.get("slots", 0)
        total_slots += slots
        combo_errors = o.get("combo_errors", [])

        if status == "OK":
            # Break the slot count down by visa type when available, e.g.
            # 'OK | Tourism: 🎫 2 slot(s) | Business: 🎫 1 slot(s)'.
            slot_types = o.get("slot_types") or []
            if slot_types:
                head += "OK | " + " | ".join(
                    f"{t}: 🎫 {c} slot(s)" for t, c in slot_types
                )
            elif slots:
                head += f"OK · 🎫 {slots} slot(s)"
            elif o.get("waitlist"):
                # No bookable slot, but VFS offered a waitlist for this route.
                head += "OK · 📝 waitlist"
            else:
                head += "OK · no slots"
            # The route succeeded, but flag any combo(s) that errored during the
            # search (e.g. VFS spinner stalled) — reported, not a route failure.
            if combo_errors:
                head += f" · ⚠️ {len(combo_errors)} combo error(s)"
        elif status == "FAILED" and combo_errors:
            # Completed, but one or more combinations errored during slot search.
            head += f"FAILED · ⚠️ {len(combo_errors)} combo error(s)"
            if slots:
                head += f" · 🎫 {slots} slot(s)"
        elif status == "FAILED":
            head += f"FAILED ({o.get('attempts', 0)} attempts)"
            if o.get("error"):
                head += f" — {_short(o['error'])}"
        elif status == "STOPPED":
            head += "STOPPED — invalid credentials"
        elif status == "GEO":
            head += "GEO-BLOCKED (403203)"
        elif status == "LOCKED":
            # The error already carries the on-page text (e.g. 'Account Locked
            # (429202) — ...'), so don't repeat the code here.
            head += f"LOCKED — {_short(o['error'])}" if o.get("error") else "LOCKED (429202)"
        elif status == "RESTRICTED":
            # The error carries the on-page text; the route was skipped for this
            # run only and is tried again fresh on the next scheduled run.
            head += (f"RESTRICTED — {_short(o['error'])}" if o.get("error")
                     else "RESTRICTED — access restricted, skipped this run")
        elif status == "BLOCKED":
            # Account disabled until a human fixes it (wrong creds / 429002) and
            # runs: python -m src.utils.account_health clear <email>
            head += f"BLOCKED — {_short(o['error'])}" if o.get("error") else "BLOCKED — account disabled"
        elif status == "PAUSED":
            # All eligible accounts for this route are cooling down (protecting
            # them) — nothing run this tick.
            head += f"PAUSED — {_short(o.get('error') or 'all accounts cooling down')}"
        elif status == "SKIPPED":
            # Skips carry their reason: email not registered on this portal, or
            # no [credN] 'routes' list covers this route at all.
            head += f"SKIPPED — {_short(o.get('error') or 'not registered here')}"
        else:
            head += status

        # Name the switched-off combinations (route JSON "disabled": true) so the
        # summary shows what is deliberately not being checked. Labels are
        # 'Centre - Category'; centres are dropped and duplicates collapsed, so
        # both Business combos read as one 'Business Visa: disabled'.
        disabled_names = []
        for label in o.get("disabled", []):
            name = label.split(" - ", 1)[-1].strip() or label
            if name not in disabled_names:
                disabled_names.append(name)
        if disabled_names:
            head += " | " + " | ".join(f"{n}: disabled" for n in disabled_names)

        # Which account this route used (per-route rotation) — masked email.
        if o.get("account"):
            head += f" · 👤 {o['account']}"

        lines.append(head)

        # Name each failed combination and its short reason (indented sub-lines).
        for label, reason in combo_errors:
            lines.append(f"   ⚠️ {label}: {_short(reason, 80)}")

    # Roll-up footer — show a bucket only if it has any routes (OK/FAILED always).
    roll = f"✅ {counts['OK']} · ❌ {counts['FAILED']}"
    if counts["STOPPED"]:
        roll += f" · 🔑 {counts['STOPPED']}"
    if counts["GEO"]:
        roll += f" · ⛔ {counts['GEO']}"
    if counts["LOCKED"]:
        roll += f" · 🔒 {counts['LOCKED']}"
    if counts["RESTRICTED"]:
        roll += f" · 🚫 {counts['RESTRICTED']}"
    if counts["BLOCKED"]:
        roll += f" · 🔴 {counts['BLOCKED']}"
    if counts["PAUSED"]:
        roll += f" · 💤 {counts['PAUSED']}"
    if counts["SKIPPED"]:
        roll += f" · ⏭️ {counts['SKIPPED']}"
    roll += f"  |  🎫 {total_slots} slot(s)"

    lines.append(roll)
    return "\n".join(lines)


def failure_alert(source_code: str, dest_code: str, error: str, attempts: int,
                  login_url: str = "", email: str = "") -> str:
    """Builds the 'run failed' alert message for ONE route.

    Shows the destination country name, the account that failed, how many
    attempts were made, and the reason.
    """
    flag = _flag(dest_code)
    prefix = f"{flag} " if flag else ""
    country = _country(dest_code)
    msg = (
        f"⚠️ {prefix}{country} slot check FAILED after {attempts} attempt(s)."
    )
    if email:
        msg += f"\nAccount: {email}"
    msg += f"\n\nLast error:\n{error}"
    return msg
