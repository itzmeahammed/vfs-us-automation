"""Diagnostics: screenshots + verbose browser-activity logging.

Split out of vfs_bot.py so the flow logic isn't interleaved with "how do we
capture evidence of what happened". Nothing here holds state and nothing here
can fail the flow — nothing here raises; every function is best-effort and
swallows its own errors (logging them instead), which is why it's safe to call
from anywhere in the flow without a try/except at the call site.
"""

import logging
import os
from contextlib import contextmanager
from datetime import datetime

from src.settings import settings
from src.utils.config_reader import get_config_value
from src.utils.run_context import run_id

#: Slot-checker screenshots: runs/<run_id>/. Under runs/ with the booking
#: artifacts so there is ONE tree of per-run evidence, and per-run rather than
#: one flat folder so a screenshot can be traced back to the run that took it
#: — the old layout reached 199 loose files with no way back to a run, which is
#: the same as having none. `_ROUTE_CODE` still goes in the FILENAME, so a
#: multi-route run keeps its routes distinguishable inside one folder.
SCREENSHOT_DIR = os.path.join("runs", run_id())

# Destination country code for the run in progress (e.g. 'GRC', 'ITA'), inserted
# into timestamped screenshot names so evidence is easy to attribute per route.
# Set once at the start of VfsBot.run(); '' before then (name omits it).
_ROUTE_CODE = ""


def set_route(dest_code: str) -> None:
    """Record the destination country code used in timestamped screenshot names."""
    global _ROUTE_CODE
    _ROUTE_CODE = (dest_code or "").strip().upper()


def browser_activity_enabled() -> bool:
    """
    Whether to attach verbose Playwright page hooks (navigations, network
    requests/responses, console messages, page errors). Controlled by the
    BROWSER_ACTIVITY_LOG env var or [logging] browser_activity in config.
    """
    raw = (
        os.environ.get("BROWSER_ACTIVITY_LOG")
        or get_config_value("logging", "browser_activity", "False")
    )
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def attach_activity_logging(page) -> None:
    """
    Attaches verbose Playwright event listeners to `page` so that browser
    activity — frame navigations, network requests/responses, console
    messages and page/request errors — is logged at DEBUG level.

    No-op unless browser-activity logging is enabled (BROWSER_ACTIVITY_LOG
    env var or [logging] browser_activity in config). The network listeners
    are intentionally chatty, so they log at DEBUG: set the log level to
    DEBUG to actually see them.
    """
    if not browser_activity_enabled():
        return

    log = logging.getLogger("browser")
    log.info("Browser-activity logging enabled (navigations, network, console).")

    def on_request(request):
        log.debug(f">> {request.method} {request.url}")

    def on_response(response):
        log.debug(f"<< {response.status} {response.url}")

    def on_request_failed(request):
        failure = getattr(request, "failure", None)
        log.warning(f"XX request failed: {request.method} {request.url} ({failure})")

    def on_console(msg):
        log.debug(f"[console:{msg.type}] {msg.text}")

    def on_page_error(error):
        log.warning(f"[page error] {error}")

    def on_frame_navigated(frame):
        # Only the main frame's navigations are interesting; iframes are noisy.
        if frame == page.main_frame:
            log.info(f"Navigated: {frame.url}")

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("framenavigated", on_frame_navigated)


#: Screenshots are SUPPRESSED FOR THE CURRENT RUN while this is True.
#:
#: WHY A RUN-SCOPED FLAG AND NOT A MODULE SWITCH. Everything under
#: src/vfs_bot/ — login, OTP, Turnstile, block detection, page_guard — is shared
#: by all four things that drive a browser: the slot checker
#: (supervisor._run_one_attempt), the booking walk (booking/probe.py), the
#: waitlist registration (waitlist/runner.py) and the single-route CLI
#: (src/main.py). They are told apart by WHO STARTED THE RUN, never by which
#: module took the picture, so a switch read here could not tell a slot check's
#: geo_blocked from a booking's.
#:
#: It also cannot be process-wide: the supervisor fires a waitlist registration
#: in its OWN process after the slot-check browser closes, and that run's
#: screenshots are the ones that diagnosed Italy's form on 2026-10-26. So the
#: slot checker sets this for the duration of its own browser work and clears
#: it, via the suppressed() context manager.
_SUPPRESSED = False


@contextmanager
def suppressed(active: bool = True):
    """Take no screenshots at all inside this block. Restores on exit.

    `active=False` is a no-op, so a caller can pass a config flag straight in
    rather than wrapping the `with` in an `if`.
    """
    global _SUPPRESSED
    previous = _SUPPRESSED
    _SUPPRESSED = _SUPPRESSED or bool(active)
    try:
        yield
    finally:
        _SUPPRESSED = previous


def screenshots_suppressed() -> bool:
    """Whether the current run is writing no screenshots."""
    return _SUPPRESSED


def write_screenshot(page, name: str, fixed_name: bool = False) -> None:
    """Writes a screenshot, ignoring browser.screenshots_enabled.

    THE ONE CHOKEPOINT. Every screenshot in src/vfs_bot/ arrives here, through
    take_screenshot, take_final_screenshot or take_milestone_screenshot, so the
    run-scoped suppression below is enforced in a single place rather than at
    forty call sites.

    fixed_name=True writes exactly <name>.png (overwritten each run) so you can
    always find e.g. Loginformloaded.png; otherwise the file is timestamped.
    """
    if _SUPPRESSED:
        logging.debug(f"Screenshot '{name}' not taken: this run takes none.")
        return

    if fixed_name:
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # e.g. 20260721_090048_GRC_turnstile_fail_1.png (route code omitted if unset).
        prefix = f"{timestamp}_{_ROUTE_CODE}_" if _ROUTE_CODE else f"{timestamp}_"
        path = os.path.join(SCREENSHOT_DIR, f"{prefix}{name}.png")
    # Screenshots are diagnostic-only and MUST never hang the flow. Use a
    # short bounded timeout and disable the font/animation/stability waits.
    # If it can't capture quickly, log and move on — never block, never use a
    # CDP fallback (that send() had no timeout and could hang forever, which
    # stalled a whole run between 'Password entered' and Sign In).
    try:
        # MADE HERE, on the first write, not when Chrome starts. VfsBot used to
        # create it up front, so every run left a folder whether or not it had
        # anything to put in it — ~48 empty folders a day once the slot check
        # stopped taking screenshots at all. Playwright would create the parent
        # itself, but doing it explicitly keeps the waitlist writer
        # (register.py) and this one on the same rule.
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        page.screenshot(
            path=path,
            full_page=False,
            timeout=5000,
            animations="disabled",
            caret="initial",
        )
        logging.debug(f"Screenshot saved: {path}")
    except Exception as e:
        logging.warning(f"Skipped screenshot '{name}' (non-fatal): {e}")


def take_screenshot(page, name: str) -> None:
    """Per-step screenshot — a no-op unless browser.screenshots_enabled is set."""
    if not settings().browser.screenshots_enabled:
        return
    write_screenshot(page, name)


def take_final_screenshot(page, name: str = "final") -> None:
    """Always writes one screenshot (used at the end of the run, and at every
    classified failure point, regardless of the screenshots_enabled setting)."""
    write_screenshot(page, name)


def take_milestone_screenshot(page, name: str) -> None:
    """A PROOF-OF-PROGRESS shot on a run that is going fine — off by default.

    The difference from take_final_screenshot is what the picture is FOR. A
    failure shot is evidence: the state cannot be reproduced, so it is written
    whatever the settings say. A milestone shot ("we reached the dashboard")
    only confirms something the log already states in words, and the log is
    what anyone actually reads.

    They were not distinguished, so the three milestones — after_signin,
    dashboard and the success-path final — went through take_final_screenshot
    and ignored every switch. At roughly 13 routes x 2 ticks an hour that came
    to 11,502 files and 548 MB of healthy runs, against 113 MB of the blocks,
    turnstile failures, account locks and waitlist pages that are the reason
    runs/ exists. Turning the ratio around is the whole point of this function.

    Set [browser] milestone_screenshots = true to get them back while
    debugging a flow that the logs alone are not explaining.
    """
    if not settings().browser.milestone_screenshots:
        return
    write_screenshot(page, name)
