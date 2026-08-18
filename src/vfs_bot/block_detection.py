"""VFS / Cloudflare block-page classification.

VFS (and Cloudflare in front of it) fail closed in a handful of recognisable
ways — an IP block, a geo block, an account lock, wrong credentials, and so
on. Telling these apart matters a lot to the supervisor: an IP block should
rotate the proxy, wrong credentials should stop and alert, an account lock
should wait out a cooldown, etc. (see src/vfs_bot/errors.py for the exact
taxonomy). This module is the single place that inspects page text and
decides which of those states we're in.

Every predicate here is a pure function of `page` — it reads text off the
page and returns a bool/str, it never mutates anything and (deliberately)
never raises on its own. That makes it possible to unit test the text-matching
rules with a stub object exposing only `.evaluate()` / `.content()` /
`.frames`, without a real browser (see tests/test_block_detection.py).
"""

from src.vfs_bot import diagnostics
from src.vfs_bot.errors import (
    AccessRestrictedError,
    AccountBlockedError,
    AccountLockedError,
    GeoBlockedError,
    IpBlockedError,
)


def _body_text(page) -> str:
    """The current page's <body> innerText, lowered. "" if it can't be read."""
    try:
        return (page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        ) or "").lower()
    except Exception:
        return ""


def _body_contains(page, *needles: str) -> bool:
    text = _body_text(page)
    return any(needle in text for needle in needles)


def full_page_text(page) -> str:
    """
    Best-effort ALL text of the current page for block-code detection: the
    body innerText, the full serialized HTML (catches raw-JSON error pages
    that Chrome shows in a JSON viewer / <pre>), and every sub-frame. Lowered.
    """
    parts = []
    try:
        parts.append(page.evaluate(
            "() => document.body ? document.body.innerText : ''") or "")
    except Exception:
        pass
    try:
        parts.append(page.content() or "")   # full HTML source
    except Exception:
        pass
    try:
        for fr in page.frames:
            try:
                parts.append(fr.evaluate(
                    "() => document.body ? document.body.innerText : ''") or "")
            except Exception:
                pass
    except Exception:
        pass
    return " ".join(parts).lower()


def is_ip_blocked(page) -> bool:
    """
    True if VFS returned code 403201 — an IP-based block (this IP made too
    many requests / is flagged). VFS renders it as JSON like
    {"code":"403201"}, so we scan the full page text (HTML + frames).
    """
    return "403201" in full_page_text(page)


def is_geo_blocked(page) -> bool:
    """
    True if the current page is VFS's 'Permission Issues (403203)' block
    (shown when the IP is outside the permitted location / rate-limited).

    Detected by page content so it works regardless of URL — looks for the
    403203 code or the 'Permission Issues' heading.
    """
    return _body_contains(page, "403203", "permission issues")


def is_email_not_registered(page) -> bool:
    """
    True if the login page is showing the 'email id is not registered'
    banner — meaning the current account isn't registered for this portal.

    Detected by the banner text so it works regardless of exact markup.
    """
    return _body_contains(page, "not registered with us")


def is_invalid_credentials(page) -> bool:
    """
    True if the login page is showing an incorrect email/password error —
    meaning the account's credentials are wrong.

    Detected by the banner/toast text so it works regardless of exact markup
    (VFS shows variants like 'The email or password you have entered is
    incorrect.'). The separate 'not registered' banner is excluded here — it
    is a different, already-handled case (EmailNotRegisteredError).
    """
    text = _body_text(page)
    if "not registered with us" in text:
        return False
    return any(phrase in text for phrase in (
        "email or password",
        "password you have entered is incorrect",
        "incorrect email or password",
        "invalid username or password",
    ))


def is_session_expired(page) -> bool:
    """
    True if VFS is showing its 'Session Expired or Invalid' page instead of the
    login form (often after cookies/cf_clearance were cleared on an egress-IP
    change). The login form never appears on this page, so detecting it lets the
    flow refresh / fail fast instead of waiting out the full login-form timeout.
    """
    return _body_contains(page, "session expired or invalid",
                          "session has expired or become invalid")


def is_access_restricted(page) -> bool:
    """
    True if VFS is showing its 'Access Restricted' block page — the portal
    has cut off this route for the current account/session. Detected by page
    text so it works regardless of URL or exact markup.
    """
    return _body_contains(page, "access restricted")


def is_access_denied(page) -> bool:
    """
    True if VFS is showing its 'Access Denied Due to Unauthorised Activity
    (429002)' block page — the account is blocked (usually after repeated
    wrong-credential attempts) and needs a manual fix. Detected by text.
    """
    return _body_contains(page, "429002", "access denied due to unauthorised activity")


def is_account_locked(page) -> bool:
    """
    True if VFS is showing its 'Account Locked (429202)' page — too many
    requests in a short window, temporary cooldown. Detected by text so it
    works regardless of exact markup.
    """
    return _body_contains(page, "429202", "account locked")


def landing_status(page) -> str:
    """
    Best-effort short description of what the page currently shows, for
    reporting when we did NOT reach the dashboard — so the alert says what we
    actually landed on instead of a generic 'no dashboard'.

    Returns the most prominent heading + first meaningful line of text (e.g.
    'Account Locked (429202) — It seems like you have made multiple requests
    ...'), or the URL if nothing readable is found.
    """
    try:
        info = page.evaluate(
            """() => {
                const txt = (el) => (el && el.innerText ? el.innerText.trim() : '');
                const heading = txt(document.querySelector('h1'))
                    || txt(document.querySelector('h2'))
                    || txt(document.querySelector('.error-title, .title'));
                let msg = '';
                for (const el of document.querySelectorAll('p, li')) {
                    const t = (el.innerText || '').trim();
                    if (t.length > 20) { msg = t; break; }
                }
                return { heading, msg, url: location.href };
            }"""
        ) or {}
    except Exception:
        info = {}
    parts = [p for p in ((info.get("heading") or "").strip(),
                         (info.get("msg") or "").strip()) if p]
    if parts:
        text = " — ".join(parts)
        return text if len(text) <= 300 else text[:299] + "…"
    return f"unrecognised page at {info.get('url') or getattr(page, 'url', '')}"


# Account/route block pages, checked in this order after the IP block (which
# has its own fixed message rather than landing_status(), so it's handled
# separately in raise_if_blocked). Each entry is (predicate, error class, the
# diagnostic screenshot name to use).
_ACCOUNT_BLOCK_TABLE = (
    (is_access_denied, AccountBlockedError, "access_denied"),
    (is_account_locked, AccountLockedError, "account_locked"),
    (is_access_restricted, AccessRestrictedError, "access_restricted"),
    # 403203 / 'Permission Issues' is listed LAST because its text is the most
    # generic of the four, so a page carrying both codes classifies as the more
    # specific one. It lives here (rather than only in turnstile's pre-dashboard
    # wait, its original home) so EVERY raise_if_blocked call site catches it —
    # including the ones during the slot check, where VFS was refusing sessions
    # mid-run with nothing watching for it.
    (is_geo_blocked, GeoBlockedError, "geo_blocked"),
)


def raise_if_blocked(page, cause: Exception = None) -> None:
    """
    If the page is a known VFS account/route block page, raise the SPECIFIC
    error so it's classified (and the account handled) correctly:

      403201 IP block                                   -> IpBlockedError
      429002 'Access Denied ... Unauthorised Activity'   -> AccountBlockedError
                                                            (manual fix needed)
      429202 'Account Locked'                            -> AccountLockedError
      429001 / 'Access Restricted'                       -> AccessRestrictedError
      403203 / 'Permission Issues'                       -> GeoBlockedError

    No-op if the page isn't a block page. `cause` (if given) is chained.
    """
    if is_ip_blocked(page):
        diagnostics.take_final_screenshot(page, "ip_blocked_403201")
        raise IpBlockedError("VFS 403201 — IP blocked (too many requests / "
                             "flagged IP). Rotate to a different IP.") from cause
    raise_if_rendered_block(page, cause)


def raise_if_rendered_block(page, cause: Exception = None) -> None:
    """The BODY-TEXT half of raise_if_blocked: every block VFS renders as a
    readable page (403203 Permission Issues, 429002, 429202, 429001).

    Split out because it costs ONE small innerText read, whereas the 403201 check
    above it scans full_page_text — page.content() serialises the entire Angular
    DOM, plus every sub-frame. Hot-path callers that merely want to confirm a page
    is healthy (page_guard's probe after each empty slot read) use this and skip
    that cost; the 403201 JSON body that needs the full scan always announces
    itself as a refused document or an error route as well, and those callers use
    the full raise_if_blocked."""
    for predicate, error_cls, screenshot_name in _ACCOUNT_BLOCK_TABLE:
        if predicate(page):
            diagnostics.take_final_screenshot(page, screenshot_name)
            raise error_cls(landing_status(page)) from cause
