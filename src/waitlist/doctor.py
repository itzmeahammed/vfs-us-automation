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
