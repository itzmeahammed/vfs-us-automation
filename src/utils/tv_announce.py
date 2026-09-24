"""TV announcements: push slot finds to the web app's TV pipeline.

Sent alongside the Telegram slot report, but only for the destinations listed
in [tv_announce] destinations (France and Italy by default). Settings live in
the [tv_announce] section of config.ini; the API key belongs in
config/config.local.ini, never in config.ini.

ONE POST PER COMBINATION, not one per route. The TV shows a single headline per
announcement, so a route with two open categories needs two announcements or
the second one is never read out. They go out most-wanted-first — see
[tv_announce] priority — because the screen rotates in arrival order.

The endpoint suppresses repeats of the same TITLE for 5 minutes, so every
announcement in a batch has to carry its own title; that is why the category is
in the title rather than only in the message, and why two combinations sharing
a category get their city appended. A retried route re-announcing the same find
inside that window is still silently dropped by the server ("duplicate": true)
— that part is intended.

A combination in a route file can override what the screen calls it, with
"tv_title" (the headline) and "tv_category" (the category in the message). Only
the TV wording changes; nothing downstream reclassifies.

Sending is best-effort, like Telegram: any failure is logged and swallowed so
it never breaks the bot's main flow.
"""

import json
import logging
import urllib.error
import urllib.request

from src.utils import telegram_message
from src.utils.config_reader import get_config_value

DEFAULT_URL = "https://www.travnooker.com/api/tv/ingest/announce"
DEFAULT_DESTINATIONS = "FRA, ITA"

# Categories announced first, most wanted first. Matched case-insensitively
# anywhere in a combination's category / sub-category, so 'Tourist' also picks
# up 'Short Stay - Tourist'. France has no tourist category — its tourist route
# is 'Short Stay (any purpose)' — hence the first entry.
DEFAULT_PRIORITY = "Short Stay (any purpose), Tourist, Tourism"

# The headline, e.g. 'France Tourist Available'. In config so rewording the
# screen does not mean editing this file. Placeholders: {country}, {visa_type}
# and {city}; anything else in the string is printed as written.
DEFAULT_TITLE_FORMAT = "{country} {visa_type} Available"


def _enabled() -> bool:
    value = get_config_value("tv_announce", "enabled", "true") or "true"
    return value.strip().lower() in ("1", "true", "yes", "on")


def _url() -> str:
    return get_config_value("tv_announce", "url", DEFAULT_URL) or DEFAULT_URL


def _api_key() -> str:
    return get_config_value("tv_announce", "api_key", "") or ""


def _timeout() -> float:
    try:
        return float(get_config_value("tv_announce", "timeout_seconds", "10") or 10)
    except ValueError:
        return 10.0


def is_configured() -> bool:
    """True when the feature is on and has an API key to send with."""
    return _enabled() and bool(_url() and _api_key())


def applies_to(dest_code: str) -> bool:
    """True if slot finds for this destination should be announced.

    Compared by country name, so 'FRA' in config also matches a route coded 'FR'.
    """
    raw = get_config_value("tv_announce", "destinations", DEFAULT_DESTINATIONS)
    wanted = {telegram_message._country(c.strip()) for c in (raw or "").split(",") if c.strip()}
    return telegram_message._country(dest_code) in wanted


def _priority_terms() -> list:
    """The [tv_announce] priority list, lowercased, in announcement order."""
    raw = get_config_value("tv_announce", "priority", DEFAULT_PRIORITY)
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


def _rank(combo: dict, terms: list) -> int:
    """Position of the first priority term this combination matches.

    len(terms) when it matches none, so unprioritised categories sort last and,
    the sort being stable, keep the order the route checked them in. "tv_title"
    is matched too, so the priority list can be written in the words the screen
    uses rather than the portal's.
    """
    text = " ".join(str(combo.get(k) or "")
                    for k in ("category", "sub_category", "label", "tv_title")).lower()
    for index, term in enumerate(terms):
        if term in text:
            return index
    return len(terms)


def _visa_type(combo: dict) -> str:
    """The visa type the headline names — the 'Tourist' in 'France Tourist
    Available'.

    A route file may set "tv_title" on a combination when the portal's own
    wording is not what the screen should say. France sells its tourist
    appointments as 'Short Stay (any purpose)', which tells a reader across a
    room nothing; that combination sets "tv_title": "Tourist". This is a display
    name for the TV only — it deliberately does NOT touch registry.purpose(),
    which still files any-purpose availability under `any` so it keeps counting
    for business clients on the dashboard.

    With no override the category is used as-is, joined through the same helper
    the report lines use, so a route that repeats itself in category and
    sub-category ('Tourism'/'Tourism') still says it once.
    """
    override = (combo.get("tv_title") or "").strip()
    if override:
        return override
    return telegram_message._join_label(
        "", "", [combo.get("category"), combo.get("sub_category")])


def _label(combo: dict, dest_code: str) -> str:
    """'France - Abu Dhabi - Short Stay' — the message under the headline.

    "tv_category" in the route file replaces the portal's category text here the
    same way "tv_title" replaces the headline, so France's any-purpose line can
    read 'Short Stay' while its title reads 'Tourist'.
    """
    override = (combo.get("tv_category") or "").strip()
    if not override:
        return telegram_message._report_label(combo, dest_code)
    city = telegram_message._city(combo.get("centre") or combo.get("label") or "")
    return telegram_message._join_label(
        telegram_message._country(dest_code), city, [override])


def _title(dest_code: str, combo: dict, fallback: str) -> str:
    """'France Tourist Available' — the headline, per [tv_announce] title_format.

    A format string that names a placeholder this does not supply would break
    every announcement on the screen, which is not worth a config typo: it is
    logged once and the built-in wording is used instead.
    """
    fields = {
        "country": telegram_message._country(dest_code),
        "visa_type": _visa_type(combo) or fallback,
        "city": telegram_message._city(combo.get("centre") or combo.get("label") or ""),
    }
    template = get_config_value("tv_announce", "title_format",
                                DEFAULT_TITLE_FORMAT) or DEFAULT_TITLE_FORMAT
    try:
        return template.format(**fields).strip()
    except (KeyError, IndexError, ValueError) as e:
        logging.warning(
            f"[tv_announce] title_format {template!r} is not usable ({e}) — "
            f"announcing as {DEFAULT_TITLE_FORMAT!r} instead."
        )
        return DEFAULT_TITLE_FORMAT.format(**fields).strip()


def build_announcement(dest_code: str, combo: dict) -> dict:
    """The request body for ONE combination's slot find.

    The title names the country and the visa type, because that is the line the
    TV reads out and because it is what the server deduplicates on. The message
    carries the full 'Country - City - Category' label, with no date — the TV
    only needs to say where a slot opened.
    """
    label = _label(combo, dest_code)
    return {
        "kind": get_config_value("tv_announce", "kind", "urgent") or "urgent",
        "title": _title(dest_code, combo, label),
        "message": label,
        "source": get_config_value("tv_announce", "source", "vfs-slot-checker")
                  or "vfs-slot-checker",
    }


def build_announcements(dest_code: str, entries: list) -> list:
    """One announcement per combination that has a slot, most wanted first.

    `entries` is the route's (combo_dict, message) list. Combinations without a
    real slot are dropped; the rest are ordered by [tv_announce] priority.
    """
    found = [combo for combo, message in entries
             if telegram_message._has_slot(message)]
    terms = _priority_terms()
    found.sort(key=lambda combo: _rank(combo, terms))
    payloads = [build_announcement(dest_code, combo) for combo in found]
    _disambiguate(payloads, found)
    return payloads


def _disambiguate(payloads: list, combos: list) -> None:
    """Add the city to titles that would otherwise collide, in place.

    Two centres offering the same visa type ('Abu Dhabi - Short Stay - Tourist'
    and 'Dubai - Short Stay - Tourist') produce one title between them, and the
    server's 5-minute title suppression would drop the second announcement.
    """
    counts = {}
    for payload in payloads:
        counts[payload["title"]] = counts.get(payload["title"], 0) + 1
    for payload, combo in zip(payloads, combos):
        if counts[payload["title"]] < 2:
            continue
        city = telegram_message._city(combo.get("centre") or combo.get("label") or "")
        if city:
            payload["title"] = f"{payload['title']} ({city})"


def announce_slots(dest_code: str, entries: list) -> bool:
    """Announce a route's slot finds on the TV if its destination qualifies.

    Sends one announcement per open combination, in priority order. Returns True
    only when the server accepted every one of them. Never raises.
    """
    if not applies_to(dest_code):
        return False
    payloads = build_announcements(dest_code, entries)
    if not payloads:
        return False                      # no combination has a slot
    if not is_configured():
        logging.warning(
            "TV announce not configured — skipping. "
            "Set [tv_announce] api_key in config/config.local.ini to enable it."
        )
        return False
    # Listed, not lazy: every announcement is attempted even when an earlier one
    # failed, the same way one failed Telegram send does not cancel the rest.
    return all([send(payload) for payload in payloads])


def send(payload: dict) -> bool:
    """POST one announcement. Returns True on success; logs and returns False otherwise."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key(),
        "User-Agent": "vfs-slot-checker-tv/1",
    }
    try:
        req = urllib.request.Request(_url(), data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
        if not result.get("ok"):
            logging.warning(f"TV announce returned not-ok: {result}")
            return False
        if result.get("duplicate"):
            logging.info(f"TV announce accepted as a duplicate (suppressed): {payload['title']}")
        else:
            logging.info(f"TV announce sent: {payload['title']}")
        return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        logging.warning(f"TV announce failed (HTTP {e.code}): {detail}")
        return False
    except Exception as e:
        logging.warning(f"Failed to send TV announce: {e}")
        return False
