"""Declarative field filling — turns a JSON field spec into a page action.

A field in config/waitlist/<ROUTE>.json looks like:

    {
      "name": "passport",                 // optional label for logs/errors
      "control": "passportNumber",        // Angular formcontrolname (preferred)
      "selector": "input#passport",       // ...or a raw CSS selector
      "widget": "text",                   // how to interact (see WIDGETS below)
      "value": "{{passport_number}}",     // resolved via context.py
      "required": true,                   // false => skip silently if absent
      "optional_value": true              // false => blank value is an error
    }

`control` is preferred over `selector`: the Angular form-control name is stable
across VFS's UI reskins where element ids (mat-mdc-checkbox-0, mat-input-3) are
not — the same reasoning detect.py applies to the waitlist checkbox.

Widgets are a REGISTRY, not a chain of ifs: a new portal control type is a new
entry here, and everything else — steps, config, journal — stays untouched. The
two that matter today (text, mat-select) delegate to the helpers the slot-check
flow has already proven in production.
"""

import logging
from typing import Any, Dict, List, Optional

from src.vfs_bot import turnstile
from src.vfs_bot.dom_utils import fill_field
from src.waitlist import context as ctx
from src.waitlist.errors import WaitlistConfigError, WaitlistStepError

DEFAULT_WIDGET = "text"
DEFAULT_TIMEOUT_MS = 15000


# --------------------------------------------------------------------------- #
# Locating                                                                     #
# --------------------------------------------------------------------------- #

#: The wrapper VFS's dynamic forms render around every field. Anchoring on the
#: LABEL inside one of these is the only stable way to find a control on portals
#: (Switzerland, and every other app-dynamic-form portal) that emit no
#: formcontrolname and only volatile ids like 'mat-input-3'.
DYNAMIC_CONTROL = "app-dynamic-control"


def _locator(page, spec: Dict[str, Any], widget: str):
    """Builds the Playwright locator for a field spec.

    Three addressing strategies, in priority order — a field spec picks whichever
    its portal actually supports:

      "selector": raw CSS. Total control; use when the markup gives a stable hook.

      "control":  an Angular formcontrolname. The most robust option WHEN THE
                  PORTAL EMITS ONE (the login form, the slot-check dropdowns and
                  the waitlist checkbox all do).

      "label":    the field's visible caption, e.g. "Passport Number". Scopes to
                  the <app-dynamic-control> block containing that text, then takes
                  the input/mat-select inside it.

                  This is the strategy for VFS's dynamic forms (Switzerland's
                  "Your Details" and friends), which render NEITHER a
                  formcontrolname NOR a stable id — the ids there are
                  'mat-input-3', 'mat-input-4'... allocated in DOM order, so they
                  shift the moment VFS adds or reorders a field. The label is
                  what a human reads and what VFS is least likely to change
                  silently, which makes it the most durable anchor available.
    """
    selector = spec.get("selector")
    if selector:
        return page.locator(selector).first, selector

    control = spec.get("control")
    if control:
        tag = _CONTROL_TAG.get(widget, "input")
        selector = f"{tag}[formcontrolname='{control}']"
        return page.locator(selector).first, selector

    label = spec.get("label")
    if label:
        return _locator_by_label(page, spec, widget, label)

    raise WaitlistConfigError(
        f"Field {_label(spec)}: needs \"label\" (the visible caption — use this "
        "for VFS dynamic forms), \"control\" (an Angular formcontrolname), or "
        "\"selector\" (raw CSS)."
    )


def _locator_by_label(page, spec: Dict[str, Any], widget: str, label: str):
    """Finds a control by the visible label of its <app-dynamic-control> block.

    `index` picks among controls sharing one label — that is how the split phone
    field is addressed: both the country-code and number inputs live under a
    single "Contact number" caption, so they are index 0 and 1 within it.
    """
    wrapper_selector = spec.get("wrapper") or DYNAMIC_CONTROL
    inner = _LABEL_INNER.get(widget, "input")

    block = page.locator(wrapper_selector).filter(has_text=label).last
    described = f"{wrapper_selector}:has-text('{label}') {inner}"

    index = spec.get("index")
    if index is None:
        return block.locator(inner).first, described
    return block.locator(inner).nth(int(index)), f"{described} [{index}]"


#: Which element to take INSIDE a label-matched block, per widget.
_LABEL_INNER = {
    "text": "input",
    "textarea": "textarea",
    "mat-select": "mat-select",
    "select": "select",
    "checkbox": "input[type='checkbox']",
    "radio": "mat-radio-button",
    "date": "input",
    "file": "input[type='file']",
}


#: Which element carries the formcontrolname for each widget type.
_CONTROL_TAG = {
    "text": "input",
    "textarea": "textarea",
    "mat-select": "mat-select",
    "select": "select",
    "checkbox": "mat-checkbox",
    "radio": "mat-radio-group",
    "date": "input",
    "file": "input",
}


def _label(spec: Dict[str, Any]) -> str:
    return spec.get("name") or spec.get("control") or spec.get("selector") or "<unnamed>"


# --------------------------------------------------------------------------- #
# Widget implementations                                                       #
# --------------------------------------------------------------------------- #

def _fill_text(page, spec, value, timeout_ms):
    locator, _ = _locator(page, spec, spec.get("widget", "text"))
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.scroll_into_view_if_needed(timeout=5000)
    fill_field(page, locator, str(value))


def _fill_mat_select(page, spec, value, timeout_ms):
    """Angular Material dropdown.

    When the field is addressed by "control", this delegates to the slot-check
    implementation, which already handles the hard parts (options loading async
    behind a spinner, half-open overlays swallowing the next click, retries) and
    is proven in production.

    When addressed by "label" (VFS dynamic forms emit no formcontrolname), the
    same open → wait → pick sequence is applied to the label-scoped locator.
    """
    control = spec.get("control")
    if control:
        from src.vfs_bot.slot_check import select_mat_dropdown

        if not select_mat_dropdown(page, control, str(value),
                                   option_timeout_ms=timeout_ms):
            raise WaitlistStepError(
                f"Could not select '{value}' in dropdown '{control}'."
            )
        return

    trigger, described = _locator(page, spec, "mat-select")
    attempts = int(spec.get("attempts") or 3)
    for attempt in range(1, attempts + 1):
        try:
            turnstile.wait_for_loader(page)
            trigger.wait_for(state="visible", timeout=timeout_ms)
            trigger.scroll_into_view_if_needed(timeout=5000)
            trigger.click(timeout=10000)

            # The overlay opens, then its options load async. Options render in a
            # cdk overlay at the END of the body — NOT inside the field block —
            # so they are matched globally by role, exactly as slot_check does.
            page.wait_for_timeout(200)
            turnstile.wait_for_loader(page)
            option = page.get_by_role(
                "option", name=str(value), exact=bool(spec.get("exact"))).first
            option.wait_for(state="visible", timeout=timeout_ms)
            option.scroll_into_view_if_needed(timeout=5000)
            option.click(timeout=10000)
            page.wait_for_timeout(500)
            return
        except Exception as e:
            logging.warning(
                f"Attempt {attempt}/{attempts}: could not select a value in "
                f"{described}: {e}"
            )
            # Close any half-open overlay so it can't block the next field.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass
            if attempt < attempts:
                page.wait_for_timeout(1000)

    raise WaitlistStepError(
        f"Could not select '{value}' in dropdown {described}."
    )


def _fill_native_select(page, spec, value, timeout_ms):
    locator, _ = _locator(page, spec, "select")
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.select_option(label=str(value))


def _fill_checkbox(page, spec, value, timeout_ms):
    """Ticks/unticks only when the current state differs — never toggles blindly.

    State is always READ from the native <input type=checkbox>, which is the
    element that actually holds it; the wrapper carries none of its own.

    Clicking is a ladder of strategies, each verified by re-reading the state
    before moving on (see the list below). That is not defensiveness for its own
    sake — Angular Material stacks a ripple and a background div over the input,
    and VFS's consent labels contain a LINK, so several otherwise-obvious targets
    either get intercepted or navigate away instead of ticking.

    Addressing: prefer "label" with wording unique to the box you want. VFS's
    review-pay boxes render with DUPLICATE ids (two are 'mat-mdc-checkbox-1'),
    so id selectors are ambiguous and indices shift as the page changes. Text is
    the only stable discriminator; "index" is a fallback for when two boxes
    genuinely share wording.
    """
    want = str(value).strip().lower() in ("1", "true", "yes", "on")

    label = spec.get("label")
    if label and not spec.get("selector") and not spec.get("control"):
        wrapper = spec.get("wrapper") or "mat-checkbox"
        index = int(spec.get("index") or 0)
        box_wrapper = page.locator(wrapper).filter(has_text=label).nth(index)
        described = f"{wrapper}:has-text('{label}')[{index}]"
    else:
        box_wrapper, described = _locator(page, spec, "checkbox")

    box_wrapper.wait_for(state="visible", timeout=timeout_ms)
    native = box_wrapper.locator("input[type='checkbox']").first
    try:
        current = bool(native.is_checked())
    except Exception:
        current = False

    if current == want:
        logging.debug(f"Checkbox {described} already {'ticked' if want else 'clear'}.")
        return

    box_wrapper.scroll_into_view_if_needed(timeout=5000)

    # Click ladder, most reliable FIRST. Order matters here for a specific
    # reason: VFS's consent labels contain a LINK —
    #
    #   <label><span>I accept the</span><a target="_blank">Terms and
    #   Conditions</a></label>
    #
    # and Playwright clicks an element's CENTRE, which on that label lands on
    # the anchor. Clicking the label therefore opened the T&Cs in a new tab and
    # left the box unticked. So the native <input> is targeted first: it is the
    # element that actually holds the state, and force=True bypasses the
    # Material ripple/background overlay that would otherwise intercept it.
    attempts = (
        ("native input", lambda: native.click(timeout=timeout_ms, force=True)),
        # The touch target is a bare overlay div with no text and no link —
        # Material renders it precisely to be clicked.
        ("touch target",
         lambda: box_wrapper.locator(".mat-mdc-checkbox-touch-target").first
         .click(timeout=timeout_ms, force=True)),
        # Label, but aimed at its FIRST span rather than its centre, so a
        # trailing link can never be the click target.
        ("label text",
         lambda: box_wrapper.locator("label span").first.click(timeout=timeout_ms)),
        ("wrapper", lambda: box_wrapper.click(timeout=timeout_ms, force=True)),
        # Last resort: drive the input directly and fire the events Angular
        # listens for, so its form model updates even without a real click.
        ("JS dispatch", lambda: native.evaluate(
            """el => {
                if (!el.checked) { el.click(); }
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")),
    )

    for how, click in attempts:
        try:
            click()
        except Exception as e:
            logging.debug(f"Checkbox {described}: {how} click failed ({e}); "
                          "trying next.")
            continue

        page.wait_for_timeout(300)
        try:
            if bool(native.is_checked()) == want:
                logging.debug(f"Checkbox {described} set via {how}.")
                return
        except Exception:
            # State unreadable — assume the click landed rather than looping and
            # possibly toggling it back off.
            logging.debug(f"Checkbox {described}: state unreadable after {how}; "
                          "assuming it worked.")
            return
        logging.debug(f"Checkbox {described}: {how} click did not change the "
                      "state; trying next.")

    raise WaitlistStepError(
        f"Checkbox {described} did not change state (wanted "
        f"{'ticked' if want else 'clear'}) after trying "
        f"{len(attempts)} click strategies.")


def _fill_radio(page, spec, value, timeout_ms):
    locator, selector = _locator(page, spec, "radio")
    locator.wait_for(state="visible", timeout=timeout_ms)
    option = page.locator(f"{selector} mat-radio-button").filter(
        has_text=str(value)).first
    option.scroll_into_view_if_needed(timeout=5000)
    option.click(timeout=timeout_ms)


def _fill_date(page, spec, value, timeout_ms):
    """Types into the date input directly.

    Typing beats driving the calendar overlay when the portal accepts it: it is
    faster and far less brittle. If a route turns out to REQUIRE the picker, add
    a 'datepicker' widget here rather than complicating this one.
    """
    _fill_text(page, spec, value, timeout_ms)
    try:
        page.keyboard.press("Escape")  # dismiss any auto-opened calendar overlay
    except Exception:
        pass


def _fill_file(page, spec, value, timeout_ms):
    """Uploads a document.

    Some portals (Italy) take the applicant's details FROM the file rather than
    from typed fields: you upload the passport bio page, confirm it, and VFS
    OCRs the details into the form. Three optional keys support that:

        "path_must_exist"  fail early with a clear message rather than letting
                           Playwright report a cryptic upload error
        "after_upload"     a button to click once the file is attached
                           (Italy's "Continue" on the upload preview)
        "wait_after_ms"    how long to let the extraction run before the step
                           moves on

    The <input type=file> is usually hidden behind a styled Browse button;
    set_input_files() targets the input directly and works regardless.
    """
    from src.waitlist import documents

    # documents.resolve() handles both forms a client file may use — an explicit
    # path, or the sentinel "managed" meaning "look it up in the managed store" —
    # and VALIDATES either before a browser sees it: extension allowlist, magic
    # bytes, and the portal's 2MB cap. Failing here costs a second; failing at
    # the portal costs a run and an opaque rejection.
    # The client id rides in on the field spec, put there by fill_one from the
    # resolution context (context.build exposes it as registrant.id). A route
    # config must never need to name WHICH client it is filling in for.
    registrant_id = str(spec.get("_registrant_id") or "")
    path = documents.resolve(registrant_id, str(value),
                             kind=spec.get("kind") or documents.PASSPORT)

    locator, _ = _locator(page, spec, "file")
    locator.set_input_files(path, timeout=timeout_ms)

    after = spec.get("after_upload")
    if after:
        # Give the preview a moment to render before its button is clickable.
        page.wait_for_timeout(int(spec.get("preview_wait_ms") or 1500))
        turnstile.wait_for_loader(page)
        _click_after_upload(page, after, _label(spec), timeout_ms)

    wait_ms = int(spec.get("wait_after_ms") or 0)
    if wait_ms:
        logging.info(f"  waiting {wait_ms / 1000:.0f}s for the portal to read "
                     f"the uploaded document...")
        page.wait_for_timeout(wait_ms)
        turnstile.wait_for_loader(page)


def _click_after_upload(page, spec, what: str, timeout_ms: int) -> None:
    """Clicks the confirm/continue control shown on an upload preview.

    Kept local to the file widget rather than reusing register._click(): that
    lives in the step layer and importing it here would invert the dependency
    (fields is the lower layer). The ladder is the same idea — normal, then
    force, then a JS dispatch.
    """
    if isinstance(spec, str):
        locator = page.locator(spec).filter(visible=True).first
        described = spec
    elif isinstance(spec, dict) and spec.get("selector"):
        locator = page.locator(spec["selector"]).filter(visible=True).first
        described = spec["selector"]
    elif isinstance(spec, dict) and spec.get("name"):
        locator = page.get_by_role(spec.get("role", "button"),
                                   name=spec["name"],
                                   exact=bool(spec.get("exact"))).first
        described = f"button '{spec['name']}'"
    else:
        raise WaitlistConfigError(
            f"{what}: \"after_upload\" needs \"name\" or \"selector\".")

    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.scroll_into_view_if_needed(timeout=5000)
    except Exception as e:
        raise WaitlistStepError(
            f"{what}: upload preview never showed {described}: {e}") from e

    for how, kwargs in (("normal", {"timeout": timeout_ms}),
                        ("force", {"force": True, "timeout": timeout_ms})):
        try:
            locator.click(**kwargs)
            logging.info(f"  confirmed the upload ({described}).")
            return
        except Exception as e:
            logging.debug(f"{what}: {how} click on {described} failed ({e}).")
    try:
        locator.evaluate("el => el.click()")
        logging.info(f"  confirmed the upload ({described}, JS dispatch).")
    except Exception as e:
        raise WaitlistStepError(
            f"{what}: could not click {described}: {e}") from e


#: widget name -> handler. Extend here for a new control type.
WIDGETS = {
    "text": _fill_text,
    "textarea": _fill_text,
    "mat-select": _fill_mat_select,
    "select": _fill_native_select,
    "checkbox": _fill_checkbox,
    "radio": _fill_radio,
    "date": _fill_date,
    "file": _fill_file,
}


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

#: How long to look for an "if_present" field before deciding it is absent.
#: Short by design: the page is already rendered by the time fields are filled
#: (the step's page gate has passed), so a control that is going to exist is
#: there within a moment. A long probe would just be dead time on every absent
#: field — the exact cost this avoids.
PRESENCE_PROBE_MS = 1500


def _exists(page, spec: Dict[str, Any], widget: str) -> bool:
    """True if an "if_present" field is actually rendered on the current page.

    Deliberately checks ATTACHED rather than VISIBLE: a control inside a
    collapsed section or below the fold is present and fillable (fill_field
    scrolls to it), and treating it as absent would silently skip real data.
    """
    try:
        locator, _ = _locator(page, spec, widget)
    except WaitlistConfigError:
        return False   # a malformed spec can never be "present"
    try:
        locator.wait_for(state="attached", timeout=PRESENCE_PROBE_MS)
        return True
    except Exception:
        return False


def resolve_value(spec: Dict[str, Any], context: Dict[str, Any],
                  where: str = "") -> Optional[str]:
    """Resolves a field's {{value}}. Returns None when the field should be skipped."""
    raw = spec.get("value")
    if raw is None:
        if spec.get("required", False):
            raise WaitlistConfigError(
                f"Field {_label(spec)}{' in ' + where if where else ''}: "
                "marked required but has no \"value\"."
            )
        return None

    value = ctx.resolve(raw, context, where=f"{where} field {_label(spec)}".strip())

    if value in (None, ""):
        if spec.get("required", False) and not spec.get("optional_value", False):
            raise WaitlistConfigError(
                f"Field {_label(spec)}{' in ' + where if where else ''}: resolved "
                f"to an empty value from '{raw}'. Fill it in the registrant "
                "profile, or mark the field \"required\": false."
            )
        return None
    return value


def fill_one(page, spec: Dict[str, Any], context: Dict[str, Any],
             where: str = "", dry_run: bool = False) -> Optional[str]:
    """
    Fills a single field. Returns the value written, or None if skipped.

    In dry_run the value is resolved and validated but NOT typed — so a dry run
    still catches every config and data error without touching the page.
    """
    value = resolve_value(spec, context, where=where)
    if value is None:
        logging.debug(f"Field {_label(spec)}: no value — skipped.")
        return None

    widget = (spec.get("widget") or DEFAULT_WIDGET).strip().lower()
    handler = WIDGETS.get(widget)
    if handler is None:
        raise WaitlistConfigError(
            f"Field {_label(spec)}: unknown widget '{widget}'. Available: "
            + ", ".join(sorted(WIDGETS))
        )

    # The upload widget needs to know WHICH client it is uploading for, so it can
    # look the document up in the managed store. That is a property of the run,
    # not of the route config, so it is carried on the context and attached here
    # rather than being something a route file has to declare.
    if widget == "file":
        spec = dict(spec)
        spec["_registrant_id"] = str(context.get("registrant.id") or "")

    if dry_run:
        logging.info(f"  [dry-run] would set {_label(spec)} ({widget})")
        return value

    timeout_ms = int(spec.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    turnstile.wait_for_loader(page)   # controls can be behind the spinner

    # "if_present": the field is optional ON THE PAGE. Each portal renders a
    # different subset of fields (and VFS varies it between sessions), so a
    # route config lists every field it may need and marks the conditional ones.
    #
    # Absence is decided by a SHORT existence probe, not by letting the fill time
    # out: waiting the full 15s per absent field is pure dead time, and — more
    # importantly — a timeout cannot be told apart from a field that is present
    # but unfillable. Probing first keeps "not on this page" (skip) and "on the
    # page but broken" (fail) as genuinely different outcomes.
    if spec.get("if_present") and not _exists(page, spec, widget):
        logging.info(f"  {_label(spec)} not on this page — skipped "
                     "(\"if_present\": true).")
        return None

    try:
        handler(page, spec, value, timeout_ms)
    except (WaitlistStepError, WaitlistConfigError):
        raise
    except Exception as e:
        raise WaitlistStepError(
            f"Could not fill field {_label(spec)} ({widget}): {e}"
        ) from e

    # Never log the value itself — these are passport numbers and dates of birth.
    logging.info(f"  set {_label(spec)} ({widget})")
    return value


def fill_all(page, specs: List[Dict[str, Any]], context: Dict[str, Any],
             where: str = "", dry_run: bool = False) -> int:
    """Fills every field in order. Returns how many were actually written."""
    written = 0
    for spec in specs or []:
        if spec.get("disabled"):
            logging.debug(f"Field {_label(spec)}: disabled — skipped.")
            continue
        if fill_one(page, spec, context, where=where, dry_run=dry_run) is not None:
            written += 1
    return written


def templates_in(specs: List[Dict[str, Any]], where: str = "") -> List[tuple]:
    """(where, template) pairs for every field value — for pre-flight validation.

    "if_present" fields are EXCLUDED. Their data is optional by definition: at
    fill time an absent control is probed for and skipped (see _exists), so a
    client with no `address_line_1` registers perfectly well on the renders that
    do not ask for one.

    Including them made pre-flight demand data the portal may never request —
    /clients rejected a client as invalid for omitting a field the readiness
    endpoint had just reported as optional. Validation must not be stricter than
    the thing it is validating for.
    """
    out = []
    for spec in specs or []:
        if spec.get("disabled") or spec.get("value") is None:
            continue
        if spec.get("if_present"):
            continue
        out.append((f"{where} field {_label(spec)}".strip(), spec["value"]))
    return out
