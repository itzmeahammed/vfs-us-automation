"""Diagnostics: screenshots + verbose browser-activity logging.

Split out of vfs_bot.py so the flow logic isn't interleaved with "how do we
capture evidence of what happened". Nothing here holds state and nothing here
can fail the flow — nothing here raises; every function is best-effort and
swallows its own errors (logging them instead), which is why it's safe to call
from anywhere in the flow without a try/except at the call site.
"""

import logging
import os
from datetime import datetime

from src.settings import settings
from src.utils.config_reader import get_config_value

SCREENSHOT_DIR = "screenshots"

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


def write_screenshot(page, name: str, fixed_name: bool = False) -> None:
    """Unconditionally writes a screenshot, ignoring browser.screenshots_enabled.

    fixed_name=True writes exactly <name>.png (overwritten each run) so you can
    always find e.g. Loginformloaded.png; otherwise the file is timestamped.
    """
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
