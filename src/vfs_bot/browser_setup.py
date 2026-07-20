"""Browser/context/page bring-up for a single run() — CDP attach (prod, EC2)
or a self-launched browser (local/dev).

Pulled out of run() so that ~90-line branch reads as one call instead of being
inlined in the middle of the flow method. Pure Playwright orchestration with
no VfsBot instance state, aside from the CdpConnectError translation.
"""

import logging

from playwright_stealth import stealth_sync

from src.vfs_bot import session
from src.vfs_bot.errors import CdpConnectError


def launch_or_attach(playwright, browser_type: str, headless_mode: str, cdp_url: str):
    """
    Returns a ready (browser, context, page) tuple.

    If `cdp_url` is set, attaches to the Chrome the supervisor already
    launched (and owns the lifecycle of) over the Chrome DevTools Protocol —
    the EC2/prod path. Otherwise launches a fresh browser here — the local/dev
    path, using Playwright's own lifecycle.

    Raises CdpConnectError if a CDP url was given but connecting failed.
    """
    if cdp_url:
        return _attach_over_cdp(playwright, cdp_url)
    return _launch_new(playwright, browser_type, headless_mode)


def _attach_over_cdp(playwright, cdp_url: str):
    # Attach to an existing Chrome launched with --remote-debugging-port.
    # The supervisor launches/kills that Chrome and injects its cdp_url
    # at runtime; we only attach here.
    logging.debug(f"Connecting to Chrome via CDP: {cdp_url}")
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        raise CdpConnectError(f"Could not connect to Chrome at {cdp_url}: {e}") from e

    context = browser.contexts[0] if browser.contexts else browser.new_context()
    # Drop only VFS's stale login/session cookies while KEEPING Cloudflare's
    # clearance (cf_clearance / __cf*). The persistent profile keeps
    # cf_clearance so we don't get a fresh 403, but its old VFS session would
    # otherwise land us on "Session Expired" — clearing it makes each run log
    # in fresh.
    session.clear_site_session(context)
    # Reuse Chrome's startup tab if present, else open one. Either way we
    # (re)navigate the caller below so the page loads with clearance kept but
    # the VFS session gone.
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page


def _launch_new(playwright, browser_type: str, headless_mode: str):
    # Launch our own browser (the prod path — headless on a server).
    is_headless = headless_mode in ("True", "true")
    launch_args = {}
    if browser_type == "chromium":
        launch_args["args"] = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
    browser = getattr(playwright, browser_type).launch(headless=is_headless, **launch_args)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    stealth_sync(page)
    return browser, context, page
