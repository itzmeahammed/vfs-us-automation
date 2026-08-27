"""The MUTATING waitlist flow: tick the checkbox, walk the pages, submit.

This is the only module in the package that changes anything on VFS's side, and
it is built around one rule:

        ═══════════ THE POINT OF NO RETURN ═══════════

    Before the step flagged "commits": true is submitted, a failure is
    WaitlistStepError — abandon quietly, nothing changed.

    After it, a failure is WaitlistCommittedError — which is deliberately NOT a
    RetryableError, so the supervisor's relaunch-and-retry logic can never pick
    it up. A submit that may have landed must never be replayed automatically.

Everything else here serves that rule: the journal 'pending' row is fsync'd
before the committing click, screenshots are unconditional around it, and the
post-commit path only ever READS the page to work out what happened.

The flow itself is entirely declarative — steps, fields and page gates come from
config/waitlist/<ROUTE>.json (see config.py), the values from
config/registrants/<id>.json (see registrant.py). Adding a country means adding
JSON, not editing this file.
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from src.settings import settings
from src.vfs_bot import diagnostics, turnstile
from src.waitlist import config as waitlist_config
from src.waitlist import context as ctx
from src.waitlist import detect, fields, guards, journal
from src.waitlist.errors import (
    WaitlistCommittedError,
    WaitlistConfigError,
    WaitlistNotOfferedError,
    WaitlistSkipped,
    WaitlistStepError,
)
from src.waitlist.result import Status, WaitlistResult

DEFAULT_STEP_TIMEOUT_MS = 45000


# --------------------------------------------------------------------------- #
# Page helpers                                                                 #
# --------------------------------------------------------------------------- #

def _await_page(page, step: Dict[str, Any], timeout_ms: int) -> None:
    """Waits for the step's page gate ("url_contains" / "wait_for_text")."""
    url_fragment = step.get("url_contains")
    if url_fragment:
        try:
            page.wait_for_url(f"**{url_fragment}**", timeout=timeout_ms)
        except Exception as e:
            raise WaitlistStepError(
                f"Step '{step['name']}': never reached a URL containing "
                f"'{url_fragment}' (currently {page.url}): {e}"
            ) from e

    turnstile.wait_for_loader(page)

    text = step.get("wait_for_text")
    if text:
        try:
            page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)
        except Exception as e:
            raise WaitlistStepError(
                f"Step '{step['name']}': page never showed '{text}': {e}"
            ) from e


def _click(page, spec: Any, what: str, timeout_ms: int) -> None:
    """Clicks a button described either as a CSS string or {role, name} object."""
    if isinstance(spec, str):
        locator = page.locator(spec).filter(visible=True).first
    elif isinstance(spec, dict):
        if spec.get("selector"):
            locator = page.locator(spec["selector"]).filter(visible=True).first
        else:
            role = spec.get("role", "button")
            name = spec.get("name")
            if not name:
                raise WaitlistConfigError(
                    f"{what}: needs \"name\" (the button text) or \"selector\"."
                )
            locator = page.get_by_role(role, name=name,
                                       exact=bool(spec.get("exact"))).first
    else:
        raise WaitlistConfigError(f"{what}: must be a CSS string or an object.")

    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.scroll_into_view_if_needed(timeout=5000)

    # Same click ladder the login flow uses: normal, then force, then JS
    # dispatch — a force-click can still time out on a slow Xvfb box.
    for how, kwargs in (("normal", {"timeout": timeout_ms}),
                        ("force", {"force": True, "timeout": timeout_ms})):
        try:
            locator.click(**kwargs)
            logging.debug(f"{what}: clicked ({how}).")
            return
        except Exception as e:
            logging.debug(f"{what}: {how} click failed ({e}); trying next.")
    try:
        locator.evaluate("el => el.click()")
        logging.debug(f"{what}: clicked (JS dispatch).")
    except Exception as e:
        raise WaitlistStepError(f"{what}: could not click: {e}") from e


def _await_enabled(page, spec: Any, timeout_ms: int) -> None:
    """Polls until the submit control is actually enabled.

    This is the SECOND of two defences around a portal countdown, not the only
    one. On VFS's "Your Details" the Save button ENABLES BEFORE the 30s timer
    expires, but clicking it early silently does nothing — the page just does not
    advance. So the button's own state cannot be trusted as the signal.

    The step's "dwell_seconds" waits out the countdown; this poll then confirms
    the button really is live before clicking. Belt and braces: the wait handles
    the invisible timer, the poll handles the visible state (async validation, a
    slow render, a consent checkbox not yet registered).

    Also treats an `aria-disabled` / `mat-mdc-button-disabled` state as disabled:
    Angular Material sometimes keeps a button focusable while it is logically
    off, and is_enabled() alone would report it ready too early.
    """
    if not isinstance(spec, dict) or not spec.get("name"):
        return

    deadline = time.time() + timeout_ms / 1000
    locator = page.get_by_role(spec.get("role", "button"), name=spec["name"]).first
    started, announced = time.time(), False

    while time.time() < deadline:
        try:
            if locator.is_enabled() and not _looks_disabled(locator):
                waited = time.time() - started
                if waited >= 2:
                    logging.info(f"  '{spec['name']}' enabled after "
                                 f"{waited:.0f}s (portal countdown).")
                return
        except Exception:
            pass
        if not announced and time.time() - started > 3:
            announced = True
            logging.info(f"  waiting for '{spec['name']}' to enable "
                         "(portal countdown)...")
        page.wait_for_timeout(500)

    logging.warning(
        f"Submit '{spec['name']}' still disabled after "
        f"{timeout_ms // 1000}s — attempting the click anyway.")


def _looks_disabled(locator) -> bool:
    """True if the element is disabled by attribute or Material's class.

    Belt and braces around is_enabled(): Material's disabled buttons carry
    `disabled="true"` AND `mat-mdc-button-disabled`, and a stale render can
    briefly report one without the other.
    """
    try:
        if locator.get_attribute("disabled") is not None:
            return True
        if (locator.get_attribute("aria-disabled") or "").lower() == "true":
            return True
        classes = locator.get_attribute("class") or ""
        return "mat-mdc-button-disabled" in classes
    except Exception:
        return False


def _scroll_to_bottom(page, step: Dict[str, Any]) -> None:
    """Scrolls to the foot of the page when the step asks for it.

    Some steps (review-pay) put their consent checkboxes and the submit button
    below the fold. Scrolling first makes them visible so the clicks land
    reliably — and it is what a human does before agreeing to terms.
    """
    if not step.get("scroll_to_bottom"):
        return
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(600)
        logging.debug("Scrolled to the bottom of the page.")
    except Exception as e:
        # Not fatal: the per-field scroll_into_view_if_needed still runs.
        logging.debug(f"Could not scroll to the bottom (continuing): {e}")


def _dwell(page, step: Dict[str, Any], key: str, why: str) -> None:
    """Waits `key` seconds, if the step configures it.

    Two distinct waits exist because they serve different purposes:

      "settle_seconds"  BEFORE the fields are filled. Some portals gate on how
                        long the page was open before it was touched — filling a
                        nine-field form the instant it renders is the least
                        human thing a client can do, and Angular may still be
                        wiring up its validators.
      "dwell_seconds"   AFTER filling, before submitting. This is the portal's
                        own stated minimum (Switzerland: "please wait 18 seconds
                        before saving your details and continuing"), which keeps
                        Save disabled until it elapses.

    Keeping them separate means a portal that wants one, the other, or both is a
    config change rather than a code change.
    """
    seconds = float(step.get(key) or 0)
    if seconds <= 0:
        return
    logging.info(f"  waiting {seconds:.0f}s ({why})...")
    page.wait_for_timeout(int(seconds * 1000))


def _read_reference(page, confirmation: Dict[str, Any]) -> Optional[str]:
    """Extracts the VFS reference number from the confirmation page, if any."""
    pattern = (confirmation or {}).get("reference_pattern")
    if not pattern:
        return None
    try:
        body = page.locator("body").inner_text(timeout=10000)
    except Exception:
        return None
    match = re.search(pattern, body)
    return match.group(1) if match else None


def _confirm(page, confirmation: Dict[str, Any], timeout_ms: int) -> bool:
    """True if the page shows the configured success wording."""
    texts = (confirmation or {}).get("success_text") or []
    url_fragment = (confirmation or {}).get("url_contains")

    if url_fragment:
        try:
            page.wait_for_url(f"**{url_fragment}**", timeout=timeout_ms)
        except Exception:
            logging.debug(f"Confirmation URL '{url_fragment}' not reached.")

    turnstile.wait_for_loader(page)
    if not texts:
        return bool(url_fragment)  # URL alone is the signal when no text given

    try:
        body = page.locator("body").inner_text(timeout=timeout_ms).lower()
    except Exception as e:
        logging.warning(f"Could not read the confirmation page: {e}")
        return False
    return all(str(t).lower() in body for t in texts)


# --------------------------------------------------------------------------- #
# Step execution                                                               #
# --------------------------------------------------------------------------- #

class _DryRunStop(Exception):
    """A dry run reached a submit it will not click, so the walk is over.

    Control flow, not an error: it carries the step name so the caller can say
    how far the rehearsal got. Deliberately private and caught in this module —
    it must never surface as a failure to the runner.
    """

    def __init__(self, step_name: str):
        super().__init__(f"dry run stopped at step '{step_name}'")
        self.step_name = step_name


def _run_step(page, step: Dict[str, Any], context: Dict[str, Any],
              result: WaitlistResult, dry_run: bool) -> None:
    """Runs ONE pre-commit step: gate the page, fill the fields, submit."""
    name = step["name"]
    timeout_ms = int(step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS)
    logging.info(f"Waitlist step '{name}'...")

    _await_page(page, step, timeout_ms)
    _screenshot(page, result, f"waitlist_{name}_before", step)

    _dwell(page, step, "settle_seconds", "letting the page settle before filling")
    _scroll_to_bottom(page, step)
    fields.fill_all(page, step.get("fields"), context,
                    where=f"step '{name}'", dry_run=dry_run)

    submit = step.get("submit")
    if not submit:
        result.steps_completed.append(name)
        return

    _dwell(page, step, "dwell_seconds", "portal requires it before submitting")
    _screenshot(page, result, f"waitlist_{name}_filled", step)

    if dry_run:
        # Raised, not returned. Returning let the caller's loop advance to the
        # NEXT step, which then waited 120s for a navigation that could not
        # happen — because this step deliberately never submitted. Every dry run
        # therefore ended in a timeout and reported 'failed', which made Stage 2
        # useless: it could not tell "the form is filled correctly" from "the
        # portal broke".
        #
        # A dry run's job ends at the first submit it declines to click, so say
        # so and unwind, rather than pretending there is a further step to walk.
        logging.info(f"  [dry-run] would submit step '{name}' — stopping here.")
        raise _DryRunStop(name)

    _await_enabled(page, submit, timeout_ms)
    _click(page, submit, f"Step '{name}' submit", timeout_ms)
    result.steps_completed.append(name)
    page.wait_for_timeout(1500)
    turnstile.wait_for_loader(page)


def _run_commit_step(page, step: Dict[str, Any], context: Dict[str, Any],
                     result: WaitlistResult, dry_run: bool,
                     confirmation: Dict[str, Any]) -> None:
    """
    Runs the COMMITTING step — the one whose submit actually registers.

    Everything before the click may still fail safely. Everything from the click
    onward raises WaitlistCommittedError, never a retryable error.
    """
    name = step["name"]
    timeout_ms = int(step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS)
    logging.info(f"Waitlist step '{name}' (COMMITTING)...")

    # --- still safe: page gate, fields, dwell -------------------------------
    _await_page(page, step, timeout_ms)
    _screenshot(page, result, f"waitlist_{name}_before", step)
    _dwell(page, step, "settle_seconds", "letting the page settle before filling")
    _scroll_to_bottom(page, step)
    fields.fill_all(page, step.get("fields"), context,
                    where=f"step '{name}'", dry_run=dry_run)
    submit = step.get("submit")
    if not submit:
        raise WaitlistConfigError(
            f"Step '{name}' is marked \"commits\": true but has no \"submit\" — "
            "the committing step must be the one that submits."
        )
    _dwell(page, step, "dwell_seconds", "portal requires it before submitting")
    _screenshot(page, result, f"waitlist_{name}_filled", step)   # evidence

    if dry_run:
        logging.info(
            f"  [dry-run] would SUBMIT '{name}' here — this is the point of no "
            "return. Stopping without submitting."
        )
        result.finish(Status.DRY_RUN, "dry run — stopped before submit")
        return

    _await_enabled(page, submit, timeout_ms)

    # --- write-ahead: record the intent BEFORE acting on it -----------------
    result.status = Status.PENDING
    journal.append(result)          # fsync'd inside

    # ═══════════════════ POINT OF NO RETURN ═══════════════════
    try:
        _click(page, submit, f"Step '{name}' SUBMIT", timeout_ms)
    except Exception as e:
        # Even a failed click may have been delivered — treat as committed.
        _screenshot(page, result, f"waitlist_{name}_submit_error", step)
        raise WaitlistCommittedError(
            f"Submit on committing step '{name}' failed: {e}. The registration "
            "may or may not have gone through — verify on the VFS account."
        ) from e

    result.steps_completed.append(name)
    logging.info(f"  submitted '{name}' — reading confirmation...")

    # --- post-commit: READ ONLY. Never re-click, never retry. ---------------
    try:
        page.wait_for_timeout(2000)
        confirmed = _confirm(page, confirmation, timeout_ms)
        reference = _read_reference(page, confirmation)
    except Exception as e:
        _screenshot(page, result, f"waitlist_{name}_after_submit", step)
        raise WaitlistCommittedError(
            f"Submitted '{name}' but could not read the confirmation page: {e}. "
            "Verify on the VFS account."
        ) from e

    _screenshot(page, result, f"waitlist_{name}_after_submit", step)

    if confirmed:
        result.vfs_reference = reference
        result.finish(Status.SUCCESS,
                      f"registered{f' (ref {reference})' if reference else ''}")
        return

    raise WaitlistCommittedError(
        f"Submitted '{name}' but the confirmation wording was not found on "
        f"{page.url}. The registration may still have succeeded — verify on the "
        "VFS account before retrying."
    )


def _screenshot(page, result: WaitlistResult, label: str,
                step: Dict[str, Any] = None) -> None:
    """Screenshots unconditionally and records the PATH on the result.

    Waitlist evidence ignores the global browser.screenshots_enabled switch:
    these images are the only record of what a committing run actually saw, and
    the journal stores their paths so a human resolving an 'unknown' outcome can
    find them.

    The path is computed here rather than taken from diagnostics.write_screenshot
    (which returns None) — deliberately not changing that shared helper's
    signature, so the always-on slot-check flow is untouched.

    EXCEPT on a step marked "no_screenshots": true. On portals that take the
    applicant's details from an uploaded passport (Italy), the page renders the
    bio page itself — so a debug screenshot would capture the photo, the MRZ and
    the signature as pixels, which no amount of log redaction can reach. The
    evidence value of a screenshot does not justify writing a copy of someone's
    passport to disk on every run.
    """
    from datetime import datetime

    if step and step.get("no_screenshots"):
        logging.debug(f"Screenshot '{label}' suppressed "
                      "(\"no_screenshots\": true — the page shows a document).")
        return

    try:
        os.makedirs(diagnostics.SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        route_code = getattr(diagnostics, "_ROUTE_CODE", "") or ""
        prefix = f"{timestamp}_{route_code}_" if route_code else f"{timestamp}_"
        path = os.path.join(diagnostics.SCREENSHOT_DIR, f"{prefix}{label}.png")
        page.screenshot(path=path, full_page=False, timeout=8000,
                        animations="disabled", caret="initial")
        result.screenshots.append(path)
        logging.debug(f"Screenshot saved: {path}")
    except Exception as e:
        # Never let a screenshot failure break the flow — least of all next to
        # the commit boundary.
        logging.warning(f"Skipped waitlist screenshot '{label}' (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def register(page, route: str, combo: str, registrant, account: str = "",
             combo_parts: Dict[str, Any] = None,
             attempted_this_run: int = 0,
             force_dry_run: Optional[bool] = None) -> WaitlistResult:
    """
    Registers for the waitlist on the CURRENT page (Appointment Details, with the
    waitlist checkbox showing).

    Returns a WaitlistResult in every case — including guard skips and failures.
    Only WaitlistCommittedError escapes, because that one must never be swallowed.

    The caller is responsible for having reached the Appointment Details page with
    `combo` selected; this function starts by ticking the checkbox.
    """
    result = WaitlistResult(
        route=route, combo=combo, registrant_id=registrant.id,
        status=Status.SKIPPED, account=account,
    )

    # --- gates (cheap, before any page interaction) -------------------------
    verdict = guards.check(route, combo, registrant,
                           attempted_this_run=attempted_this_run)
    if not verdict:
        logging.info(f"Waitlist registration skipped: {verdict.reason}")
        return result.finish(Status.SKIPPED, verdict.reason)

    dry_run = guards.dry_run() if force_dry_run is None else force_dry_run
    cfg = waitlist_config.get(route)

    # --- pre-flight: resolve EVERY value before touching the page -----------
    # A missing passport number must cost a second here, not a half-finished
    # registration three pages in.
    context = ctx.build(registrant, route=route, combo=combo,
                        combo_parts=combo_parts)
    problems = ctx.validate(_all_templates(cfg), context)
    if problems:
        reason = "config/data problems: " + "; ".join(problems[:3])
        logging.error(reason)
        result.finish(Status.FAILED, reason)
        journal.update_status(result)
        return result

    # --- the waitlist checkbox ---------------------------------------------
    checkbox = cfg.get("checkbox") or detect.DEFAULT_CHECKBOX_SELECTOR
    if not detect.is_offered(page, checkbox):
        raise WaitlistNotOfferedError(
            f"No waitlist checkbox on the page for '{combo}'."
        )

    commit_name = waitlist_config.commit_step_name(route)
    logging.info(
        f"Registering for waitlist: {route} / {combo} / {registrant.label()} "
        f"{'[DRY RUN]' if dry_run else '[LIVE]'} "
        f"(commit step: '{commit_name}')"
    )

    try:
        _tick_checkbox(page, checkbox, dry_run)

        for step in cfg["steps"]:
            if step.get("disabled"):
                logging.debug(f"Step '{step['name']}' disabled — skipped.")
                continue
            if step.get("commits"):
                _run_commit_step(page, step, context, result, dry_run,
                                 cfg.get("confirmation") or {})
                if dry_run:
                    break          # dry run stops at the commit boundary
            else:
                _run_step(page, step, context, result, dry_run)

        if dry_run and result.status != Status.DRY_RUN:
            result.finish(Status.DRY_RUN, "dry run — stopped before submit")

    except _DryRunStop as e:
        # The expected end of a dry run: the form was reached and filled, and we
        # declined to submit. Caught ahead of the error handlers below so it is
        # never misreported as a failure.
        result.finish(
            Status.DRY_RUN,
            f"dry run — filled and stopped at '{e.step_name}' without submitting")
        journal.update_status(result)
        logging.info(f"Dry run complete: reached '{e.step_name}', nothing submitted.")
        return result

    except WaitlistCommittedError as e:
        # Committed and ambiguous: record 'unknown' so the triple stays blocked
        # until a human confirms, then re-raise for the caller to alert on.
        result.finish(Status.UNKNOWN, str(e))
        journal.update_status(result)
        logging.error(f"Waitlist COMMITTED but unconfirmed: {e}")
        raise
    except (WaitlistStepError, WaitlistConfigError) as e:
        # Pre-commit: nothing was submitted, so this is a clean abandonment.
        result.finish(Status.FAILED, str(e))
        journal.update_status(result)
        logging.warning(f"Waitlist registration failed (nothing submitted): {e}")
        return result
    except Exception as e:
        # Unclassified. We cannot prove nothing was submitted, so the honest
        # answer is 'unknown' if we are past the commit step, 'failed' if not.
        committed = commit_name in result.steps_completed
        status = Status.UNKNOWN if committed else Status.FAILED
        result.finish(status, f"unexpected error: {e}")
        journal.update_status(result)
        if committed:
            raise WaitlistCommittedError(
                f"Unexpected error after submitting '{commit_name}': {e}"
            ) from e
        logging.warning(f"Waitlist registration failed (nothing submitted): {e}")
        return result

    journal.update_status(result)
    logging.info(result.summary())
    return result


def _tick_checkbox(page, selector: str, dry_run: bool) -> None:
    """Ticks the waitlist checkbox, unless it is already ticked.

    Idempotent on purpose: a previous partial attempt may have left it ticked,
    and a blind click would toggle it back OFF.

    Clicks the <label> in preference to the wrapper: Angular Material hides the
    native <input> under an overlay, so clicking the input directly is
    intercepted, while the label is what a real user hits.
    """
    if detect.is_checked(page, selector):
        logging.debug("Waitlist checkbox already ticked.")
        return
    if dry_run:
        logging.info("  [dry-run] would tick the waitlist checkbox")
        return

    box = detect.locate(page, selector)
    if box is None:
        raise WaitlistStepError(
            "Waitlist checkbox not found on the page. If VFS changed it, pin an "
            "exact selector as \"checkbox\" in the route config.")
    try:
        box.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    for what, target in (("label", box.locator("label").first),
                         ("wrapper", box)):
        try:
            target.click(timeout=10000)
            logging.debug(f"Ticked the waitlist checkbox (via {what}).")
            break
        except Exception as e:
            logging.debug(f"Checkbox {what} click failed ({e}); trying next.")
    else:
        try:
            box.locator("input[type='checkbox']").first.evaluate("el => el.click()")
            logging.debug("Ticked the waitlist checkbox (JS dispatch).")
        except Exception as e:
            raise WaitlistStepError(
                f"Could not tick the waitlist checkbox: {e}") from e

    page.wait_for_timeout(500)
    if not detect.is_checked(page, selector):
        raise WaitlistStepError(
            "Clicked the waitlist checkbox but it did not become ticked.")
    logging.info("  ticked the waitlist checkbox")


def _all_templates(cfg: Dict[str, Any]) -> List[tuple]:
    """Every {{template}} in a route config, for pre-flight validation."""
    out = []
    for step in cfg.get("steps", []):
        out.extend(fields.templates_in(step.get("fields"),
                                       where=f"step '{step.get('name')}'"))
    return out
