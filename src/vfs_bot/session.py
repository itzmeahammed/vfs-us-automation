"""Cookie-consent banner + cross-run session/cookie hygiene.

Two unrelated-looking but both cookie-shaped concerns:

  * accept_cookies() clicks through the OneTrust consent banner so it stops
    overlaying the login form.
  * clear_site_session() runs once per run, right after attaching to a
    (possibly reused) browser profile, to drop VFS's stale login session while
    keeping Cloudflare's clearance — see its docstring for why.
"""

import logging

from src.utils.config_reader import get_config_value


def accept_cookies(page, attempts: int = 8, interval_ms: int = 1000) -> bool:
    """
    Accepts ALL cookies on the consent banner. We deliberately ACCEPT (never
    reject) — both as the desired behaviour and because the banner overlays
    the bottom of the page and blocks the login fields until cleared.

    The banner often appears a few seconds AFTER the form, so we poll for it
    for a short while, and we click via several strategies (the OneTrust id,
    an exact-text 'Accept Cookies' button/link, force-click) because a single
    role-based lookup was missing it. Returns True if a click was issued.
    """
    for _ in range(attempts):
        # Strategy 1: OneTrust's stable accept-all id.
        for sel in (
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
        ):
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=3000, force=True)
                    logging.debug("Accepted all cookies (OneTrust id).")
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                pass

        # Strategy 2: any clickable element whose visible text is an accept
        # label (button OR link), exact-ish match, force-clicked.
        try:
            btn = page.get_by_text("Accept Cookies", exact=True).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000, force=True)
                logging.debug("Accepted all cookies (text 'Accept Cookies').")
                page.wait_for_timeout(300)
                return True
        except Exception:
            pass

        # Strategy 3: role-based fallbacks.
        for label in ["Accept Cookies", "Accept All Cookies", "Accept All", "Accept"]:
            try:
                btn = page.get_by_role("button", name=label).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=3000, force=True)
                    logging.debug(f"Accepted all cookies via '{label}'.")
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue

        # Banner not present yet — wait and poll again.
        page.wait_for_timeout(interval_ms)

    logging.debug("No cookie banner found after polling, skipping.")
    return False


# Cookie domains to PRESERVE across runs — Cloudflare's clearance lives here.
# Everything else on the VFS domains (the login/auth session) is cleared so
# each run starts logged-out but still trusted by Cloudflare.
_KEEP_COOKIE_NAMES = ("cf_clearance",)
_KEEP_COOKIE_PREFIXES = ("__cf", "__cflb", "cf_")


def _is_cloudflare_cookie(cookie: dict) -> bool:
    name = cookie.get("name", "")
    return name in _KEEP_COOKIE_NAMES or any(
        name.startswith(p) for p in _KEEP_COOKIE_PREFIXES
    )


def clear_site_session(context, keep_cf: bool = None) -> None:
    """
    Clears VFS's stale login/session cookies while KEEPING Cloudflare's
    clearance cookies (cf_clearance / __cf*) — so a persistent profile stays
    trusted by Cloudflare between runs but starts each run logged-out (else it
    lands on "Session Expired or Invalid").

    EXCEPTION — egress IP change: when the supervisor reuses a per-account
    profile from a DIFFERENT egress IP than last time (e.g. a cache warmed on
    the local IP, now run through a proxy), it sets browser.keep_cf_clearance
    = false. cf_clearance is bound to the IP that solved the challenge, so a
    stale one from another IP is useless AND can look suspicious to Cloudflare
    — we drop it and let Turnstile re-run cleanly. The HTTP asset cache is
    untouched either way; that's what makes a local-warmed profile cheap to
    run on a proxy IP.

    `keep_cf` overrides that config decision. Pass False to force cf_clearance
    out as well — the escalation used when a 'Session Expired or Invalid' page
    survives a session-cookie clear, where the clearance itself is the last
    remaining suspect. None (default) reads browser.keep_cf_clearance as before.
    """
    if keep_cf is None:
        keep_cf = str(get_config_value(
            "browser", "keep_cf_clearance", "true")).strip().lower() != "false"
    try:
        all_cookies = context.cookies()
    except Exception as e:
        logging.warning(f"Could not read cookies to clear session: {e}")
        return

    keep = [c for c in all_cookies if _is_cloudflare_cookie(c)] if keep_cf else []
    dropped = len(all_cookies) - len(keep)

    try:
        context.clear_cookies()  # nukes everything...
        if keep:
            context.add_cookies(keep)  # ...then restore Cloudflare's only
        if keep_cf:
            logging.debug(
                f"Cleared {dropped} VFS session cookie(s); kept {len(keep)} "
                f"Cloudflare cookie(s)."
            )
        else:
            logging.info(
                f"Egress IP changed for this profile — cleared ALL {dropped} "
                f"cookie(s) incl. cf_clearance (HTTP asset cache kept)."
            )
    except Exception as e:
        logging.warning(f"Failed to selectively clear cookies: {e}")
