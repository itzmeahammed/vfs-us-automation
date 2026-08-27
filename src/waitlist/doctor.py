"""`doctor` — does this route's config still match VFS's actual page?

A contract test between config/waitlist/<ROUTE>.json and the live portal. It
navigates to the waitlist pages and checks that every configured selector still
resolves to exactly what it should — WITHOUT typing anything, ticking anything
or submitting anything. Completely read-only.

Why this exists
---------------
VFS reskins their portals without warning. Without doctor, a changed field label
surfaces as a cryptic Playwright timeout in the middle of a run — possibly after
login, possibly on the committing step. With it, you get:

    ✗ review_pay: "I accept the" matched 3 elements (expected 2)

which names the file and the line to edit. At 10+ countries this is the
difference between "the bot is broken" and "Switzerland changed, here's the fix".

What it cannot check
--------------------
Only the FIRST page (Appointment Details) is reachable without committing:
getting to "Your Details" requires ticking the waitlist checkbox and submitting,
which mutates the account. So doctor works in two depths:

    --depth page    (default) the checkbox + anything on the current page.
                    Perfectly safe, no state change at all.
    --depth walk    ALSO ticks the checkbox and submits to reach the later
                    pages, checking their selectors too. This DOES advance the
                    booking form — it stops before the committing step, so no
                    registration is created, but the account is no longer on a
                    pristine page. Use deliberately.

`--depth walk` is what you run when mapping a NEW country; `--depth page` is the
cheap pre-flight before a real registration.
"""

import logging
from typing import Any, Dict, List, Optional

from src.settings import settings
from src.vfs_bot import turnstile
from src.waitlist import config as waitlist_config
from src.waitlist import detect, fields
from src.waitlist.errors import WaitlistConfigError

#: How long to look for a control before calling it missing. Short on purpose —
#: doctor is a health check, not a flow, and a slow answer is a failed answer.
PROBE_TIMEOUT_MS = 6000


class Finding:
    """One check: what was probed, and what was found."""

    OK = "ok"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    SKIPPED = "skipped"

    def __init__(self, step: str, name: str, status: str, detail: str = "",
                 found: int = 0):
        self.step = step
        self.name = name
        self.status = status
        self.detail = detail
        self.found = found

    @property
    def ok(self) -> bool:
        return self.status in (Finding.OK, Finding.SKIPPED)

    def line(self) -> str:
        icon = {Finding.OK: "✓", Finding.MISSING: "✗",
                Finding.AMBIGUOUS: "⚠", Finding.SKIPPED: "·"}[self.status]
        text = f"  {icon} {self.name}"
        if self.detail:
            text += f"  — {self.detail}"
        return text


def _probe(page, spec: Dict[str, Any], step_name: str) -> Finding:
    """Locates one field WITHOUT interacting with it.

    Counts matches too: a selector matching three elements is as broken as one
    matching none, and it is the failure mode that silently fills the wrong box.
    """
    name = spec.get("name") or spec.get("label") or spec.get("control") or "?"
    widget = (spec.get("widget") or fields.DEFAULT_WIDGET).strip().lower()

    if spec.get("disabled"):
        return Finding(step_name, name, Finding.SKIPPED, "disabled in config")

    try:
        locator, described = fields._locator(page, spec, widget)
    except WaitlistConfigError as e:
        return Finding(step_name, name, Finding.MISSING, str(e))

    try:
        locator.wait_for(state="attached", timeout=PROBE_TIMEOUT_MS)
    except Exception:
        if spec.get("if_present"):
            # Optional ON THE PAGE (VFS renders this form differently across
            # sessions), so its absence is expected, not a config problem.
            return Finding(step_name, name, Finding.SKIPPED,
                           "not on this page (\"if_present\": true)")
        return Finding(step_name, name, Finding.MISSING,
                       f"not found ({described})")

    # An explicit "index" means several matches are EXPECTED (e.g. the split
    # phone field, or the two identical consent checkboxes), so only report
    # ambiguity when the config did not anticipate it.
    if spec.get("index") is None:
        try:
            count = _sibling_count(page, spec, widget)
            if count > 1:
                return Finding(
                    step_name, name, Finding.AMBIGUOUS,
                    f"matched {count} elements — add \"index\" to disambiguate, "
                    "or make the selector more specific", found=count)
        except Exception:
            pass

    return Finding(step_name, name, Finding.OK, found=1)


def _sibling_count(page, spec: Dict[str, Any], widget: str) -> int:
    """How many elements this field spec matches in total."""
    if spec.get("selector"):
        return page.locator(spec["selector"]).count()
    if spec.get("control"):
        tag = fields._CONTROL_TAG.get(widget, "input")
        return page.locator(f"{tag}[formcontrolname='{spec['control']}']").count()
    label = spec.get("label")
    if label:
        wrapper = spec.get("wrapper") or (
            "mat-checkbox" if widget == "checkbox" else fields.DYNAMIC_CONTROL)
        inner = fields._LABEL_INNER.get(widget, "input")
        if widget == "checkbox":
            return page.locator(wrapper).filter(has_text=label).count()
        block = page.locator(wrapper).filter(has_text=label).last
        return block.locator(inner).count()
    return 1


def _probe_button(page, spec: Any, step_name: str, what: str) -> Finding:
    """Checks a submit/continue button exists, without clicking it."""
    if not spec:
        return Finding(step_name, what, Finding.SKIPPED, "none configured")
    try:
        if isinstance(spec, str):
            locator = page.locator(spec).filter(visible=True)
            described = spec
        elif spec.get("selector"):
            locator = page.locator(spec["selector"]).filter(visible=True)
            described = spec["selector"]
        else:
            locator = page.get_by_role(spec.get("role", "button"),
                                       name=spec.get("name", ""),
                                       exact=bool(spec.get("exact")))
            described = f"{spec.get('role', 'button')} named '{spec.get('name')}'"
        count = locator.count()
    except Exception as e:
        return Finding(step_name, what, Finding.MISSING, str(e))

    if count == 0:
        return Finding(step_name, what, Finding.MISSING, f"not found ({described})")
    if count > 1:
        return Finding(step_name, what, Finding.AMBIGUOUS,
                       f"matched {count} buttons ({described})", found=count)
    return Finding(step_name, what, Finding.OK, found=1)


def check_step(page, step: Dict[str, Any]) -> List[Finding]:
    """Every field + the submit button of one step, on the CURRENT page."""
    turnstile.wait_for_loader(page)
    name = step.get("name", "?")
    findings = [_probe(page, spec, name) for spec in (step.get("fields") or [])]
    findings.append(_probe_button(page, step.get("submit"), name, "submit button"))
    return findings


def check_checkbox(page, route: str) -> Finding:
    """The waitlist checkbox itself — the entry point to the whole flow.

    Resolved through detect.locate(), so this reports exactly what the real flow
    would find rather than probing a selector the flow does not use.
    """
    pinned = waitlist_config.checkbox_selector(route)
    try:
        box = detect.locate(page, pinned)
    except Exception as e:
        return Finding("appointment_details", "waitlist checkbox",
                       Finding.MISSING, str(e))

    if box is None:
        # Absence is genuinely ambiguous: VFS may have changed the control, OR
        # this combination simply has slots / no waitlist today. Say both rather
        # than crying wolf.
        where = f"pinned selector {pinned}" if pinned else "no <mat-checkbox> on the page"
        return Finding(
            "appointment_details", "waitlist checkbox", Finding.MISSING,
            f"not found ({where}) — either VFS changed the control, or this "
            "combination has slots / no waitlist right now")

    try:
        total = page.locator("mat-checkbox").count()
    except Exception:
        total = 1
    if total > 1 and not pinned:
        return Finding(
            "appointment_details", "waitlist checkbox", Finding.AMBIGUOUS,
            f"{total} checkboxes on the page — picked by waitlist wording. Pin "
            "\"checkbox\" in the route config if this is the wrong one",
            found=total)
    return Finding("appointment_details", "waitlist checkbox", Finding.OK, found=1)


def report(findings: List[Finding], route: str) -> str:
    """Renders the findings, grouped by step."""
    lines = [f"Config health for {route}:", ""]
    by_step: Dict[str, List[Finding]] = {}
    for finding in findings:
        by_step.setdefault(finding.step, []).append(finding)

    for step, group in by_step.items():
        good = sum(1 for f in group if f.status == Finding.OK)
        lines.append(f"{step}  ({good}/{len(group)} ok)")
        lines.extend(f.line() for f in group)
        lines.append("")

    broken = [f for f in findings if not f.ok]
    if broken:
        lines.append(f"✗ {len(broken)} problem(s) — edit "
                     f"config/waitlist/{route}.json before registering.")
    else:
        lines.append("✓ Every configured selector resolves.")
    return "\n".join(lines)


def unreachable_steps(route: str, checked: List[str]) -> List[str]:
    """Steps doctor could not reach at this depth (so you know what is untested)."""
    try:
        return [s["name"] for s in waitlist_config.get(route)["steps"]
                if s.get("name") not in checked and not s.get("disabled")]
    except WaitlistConfigError:
        return []


# --------------------------------------------------------------------------- #
# Harvesting dropdown options                                                  #
# --------------------------------------------------------------------------- #
#
# WHY THIS EXISTS
# ---------------
# A route config says `"widget": "mat-select"` — a dropdown — but not WHICH
# values it accepts. That gap is where wrong data comes from: four client files
# in this repo disagree about what a country is even called ("India", "Belize",
# "Lebanese"), because everyone guessed. A guess is only found wrong minutes
# into a live run, when get_by_role("option", name="Lebanese") matches nothing
# on a portal whose actual entry is "Lebanon" — after a login, a Turnstile
# solve and a committed form step.
#
# So the lists are READ OFF THE PORTAL rather than typed from memory, and
# written back into the route config with the date they were observed. The API
# then serves them (options_status="known"), a web app renders a real dropdown,
# and POST /clients rejects anything else — all of it derived from one
# observation instead of four people's recollections.
#
# This rides along with `--walk` on purpose. Reaching /your-details costs a
# login, a Turnstile solve, a checkbox tick and a submit; that page visit is
# already happening, so harvesting there is nearly free. A standalone command
# would pay the whole cost again for the same three dropdowns.

#: How long to wait for an overlay's options to render after opening it.
OPTION_TIMEOUT_MS = 8000

#: Below this, a country-sized dropdown is assumed to be lazily rendered rather
#: than genuinely short — see _looks_truncated().
SMALL_LIST = 25


class Harvest:
    """One dropdown's observed options, or why they could not be trusted."""

    def __init__(self, step: str, name: str, options: List[str],
                 skipped: str = ""):
        self.step = step
        self.name = name
        self.options = options
        self.skipped = skipped          # non-empty => do NOT write this one

    @property
    def usable(self) -> bool:
        return not self.skipped and bool(self.options)

    def line(self) -> str:
        if self.skipped:
            return f"  · {self.name} — not harvested: {self.skipped}"
        return f"  ✓ {self.name} — {len(self.options)} option(s)"


def _looks_truncated(page, options: List[str]) -> str:
    """Is this list plausibly INCOMPLETE? Returns a reason, or "" if it looks whole.

    The failure this guards is the dangerous one. Some VFS country dropdowns are
    searchable and render only a slice until you type; capturing that slice and
    writing it as the authoritative list would flip the field to
    options_status="known" and start REJECTING valid countries — worse than
    never harvesting, because it looks validated.

    We cannot prove completeness, so we look for the two tells of a lazy list
    and refuse on either. Refusing costs nothing (the field simply stays
    "unknown"); writing a partial list costs a broken form.
    """
    # A search box inside the overlay means the list filters as you type, so
    # what is rendered now is not the whole set.
    for probe in ("input[type='search']", ".mat-select-search input",
                  "input[placeholder*='Search' i]", "input[matinput]"):
        try:
            if page.locator(f".cdk-overlay-pane {probe}").count() > 0:
                return ("the overlay has a search box, so it renders only part "
                        "of the list — harvest is not trustworthy here")
        except Exception:
            pass

    # A cdk VIRTUAL scroll viewport renders only the visible window.
    try:
        if page.locator(".cdk-overlay-pane cdk-virtual-scroll-viewport").count() > 0:
            return ("the overlay uses virtual scrolling, so only the visible "
                    "options are in the DOM")
    except Exception:
        pass

    return ""


def harvest_select(page, spec: Dict[str, Any], step_name: str) -> Harvest:
    """Opens one mat-select, reads its options, closes it. Changes nothing.

    Opening a dropdown and pressing Escape leaves no value selected and no form
    state touched, which is what keeps doctor read-only even while harvesting.
    """
    name = spec.get("name") or spec.get("label") or "?"

    try:
        trigger, _ = fields._locator(page, spec, "mat-select")
        trigger.wait_for(state="visible", timeout=PROBE_TIMEOUT_MS)
        trigger.scroll_into_view_if_needed(timeout=5000)
        trigger.click(timeout=PROBE_TIMEOUT_MS)
    except Exception as e:
        return Harvest(step_name, name, [], f"could not open the dropdown ({e})")

    try:
        turnstile.wait_for_loader(page)
        page.get_by_role("option").first.wait_for(
            state="visible", timeout=OPTION_TIMEOUT_MS)
        # Options live in a cdk overlay at the END of <body>, not inside the
        # field block, so they are read globally by role — exactly how fields.py
        # picks one when filling.
        raw = page.get_by_role("option").all_text_contents()
        options = [t.strip() for t in raw if t and t.strip()]
    except Exception as e:
        options, reason = [], f"no options appeared ({e})"
        _close_overlay(page)
        return Harvest(step_name, name, [], reason)

    truncated = _looks_truncated(page, options)
    _close_overlay(page)

    if truncated:
        return Harvest(step_name, name, [], truncated)
    if not options:
        return Harvest(step_name, name, [], "the overlay rendered no options")

    # Duplicates would be a sign we scraped something other than one list.
    if len(set(options)) != len(options):
        seen, deduped = set(), []
        for opt in options:
            if opt not in seen:
                seen.add(opt)
                deduped.append(opt)
        options = deduped

    return Harvest(step_name, name, options)


def _close_overlay(page) -> None:
    """Dismisses an open overlay so it cannot swallow the next click."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def harvest_step(page, step: Dict[str, Any]) -> List[Harvest]:
    """Every mat-select on the CURRENT page, in config order."""
    out = []
    for spec in step.get("fields") or []:
        if spec.get("disabled"):
            continue
        if (spec.get("widget") or "").strip().lower() != "mat-select":
            continue
        out.append(harvest_select(page, spec, step.get("name", "?")))
    return out


def apply_harvest(route: str, harvests: List[Harvest],
                  captured_at: str) -> List[str]:
    """Writes observed option lists back into config/waitlist/<ROUTE>.json.

    Only the route's OWN file is touched, and only the two option keys on the
    fields that were harvested — every comment, every other key and the file's
    ordering survive, because those comments are the documentation for why each
    selector looks the way it does.

    One cosmetic caveat: the file is re-serialised with json.dump, so the BLANK
    LINES that separate blocks by hand are lost (the "_comment" keys themselves,
    and their order, are not). That is why the write is opt-in behind --harvest
    rather than something a plain --walk does: a health check should not reflow
    a file you are reading.

    A field is matched by its "name" within its step, so a route that inherits
    a step from _default.json but has no field of its own for it is left alone
    rather than having one grafted in: the write is an UPDATE of something the
    route already declares, never a new declaration.

    Returns a human-readable list of what changed.
    """
    import json
    import os

    path = os.path.join(waitlist_config.WAITLIST_DIR, f"{route}.json")
    if not os.path.isfile(path):
        raise WaitlistConfigError(
            f"Cannot write options: {path} does not exist. A route must have "
            "its own config file before its dropdowns can be harvested.")

    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    by_step: Dict[str, Dict[str, Harvest]] = {}
    for harvest in harvests:
        if harvest.usable:
            by_step.setdefault(harvest.step, {})[harvest.name] = harvest

    changes: List[str] = []
    for step in raw.get("steps") or []:
        wanted = by_step.get(step.get("name"))
        if not wanted:
            continue
        for spec in step.get("fields") or []:
            harvest = wanted.get(spec.get("name"))
            if harvest is None:
                continue
            before = spec.get("options")
            if before == harvest.options:
                changes.append(f"{harvest.name}: unchanged "
                               f"({len(harvest.options)} options)")
                # Still refresh the date — it records when we last CONFIRMED it.
                spec["options_captured_at"] = captured_at
                continue
            spec["options"] = harvest.options
            spec["options_captured_at"] = captured_at
            if before:
                changes.append(
                    f"{harvest.name}: {len(before)} -> {len(harvest.options)} "
                    "options (CHANGED — the portal's list moved)")
            else:
                changes.append(f"{harvest.name}: {len(harvest.options)} "
                               "options (new)")

    if changes:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        waitlist_config.clear_cache()

    return changes


def harvest_report(harvests: List[Harvest], changes: List[str],
                   wrote: bool) -> str:
    """Renders what was observed, and what (if anything) was written."""
    lines = ["", "Dropdown options:"]
    if not harvests:
        lines.append("  (no mat-select fields on the pages reached)")
        return "\n".join(lines)

    lines.extend(h.line() for h in harvests)

    skipped = [h for h in harvests if h.skipped]
    if not wrote:
        lines.append("")
        lines.append("  Nothing written — re-run with --harvest to save these "
                     "into the route config.")
    elif changes:
        lines.append("")
        lines.append("  Written:")
        lines.extend(f"    {c}" for c in changes)
    else:
        lines.append("")
        lines.append("  Nothing to write.")

    if skipped:
        lines.append("")
        lines.append("  A skipped list stays options_status=\"unknown\": the "
                     "form renders free text and no value is rejected. That is "
                     "deliberate — a partial list would look authoritative and "
                     "reject valid entries.")
    return "\n".join(lines)
