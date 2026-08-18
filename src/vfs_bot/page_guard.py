"""Continuous "is this session still alive?" watch.

VFS can kill a session at ANY moment — not just during login. In the field a run
reached the dashboard, checked one combination successfully, and was then served
the 'Permission Issues (403)' page mid-slot-check. Nothing noticed: block
detection only ever ran BEFORE the dashboard, so the flow kept driving a page
whose form no longer existed, burned ~97s in dropdown timeouts against missing
elements, and reported the route as OK with "no availability" — a false negative
that also cleared the account's failure strikes.

This module is the always-on answer. It works in two layers, so the check is
continuous without being expensive:

  Layer 1 — SUSPICION (passive, event-driven, free)
      install() hooks 'framenavigated' and 'response'. A main-frame navigation to
      a known error route, or a 4xx/5xx on a VFS *document* request, stamps a
      reason on the page's guard state. Playwright swallows exceptions raised
      inside event handlers, so handlers only ever SET A FLAG — they never raise.

  Layer 2 — CONFIRMATION (active, but only when something is suspicious)
      assert_alive() is called at checkpoints throughout the flow. It first does
      two free checks (page.url is a locally cached property, and the flag is a
      plain attribute read) and returns immediately when both are clean — which
      is the overwhelmingly common case. Only when something IS suspicious does
      it spend one DOM pass classifying the page, and raise the SPECIFIC typed
      error the supervisor already knows how to act on.

Splitting it this way matters: the sentinel alone would false-positive (a VFS API
can 403 while the app recovers fine), and the DOM pass alone would be too costly
to run continuously. Suspicion is cheap and noisy; confirmation is precise and
rare. A suspicion the DOM does not confirm is CLEARED, not raised.

Everything here is fail-safe: a missing/stub page, an uninstalled guard or a
dead browser degrades to "no opinion" rather than breaking the flow.
"""

import logging
from urllib.parse import urlparse

from src.vfs_bot import block_detection, diagnostics
from src.vfs_bot.errors import (
    AccessRestrictedError,
    AccountBlockedError,
    AccountLockedError,
    GeoBlockedError,
    IpBlockedError,
    PageBlockedError,
)

# Everything assert_alive() can raise. Call sites that wrap a step in a broad
# `except Exception` MUST re-raise these first — swallowing one turns a dead
# session straight back into the fake "no availability" this module exists to
# stop. Kept here (rather than spelled out at each site) so a new block error
# added to the classifier is honoured everywhere automatically.
BLOCK_ERRORS = (
    IpBlockedError,
    GeoBlockedError,
    AccountBlockedError,
    AccountLockedError,
    AccessRestrictedError,
    PageBlockedError,
)

# Attribute the guard state is stashed on (the Page object itself, so free
# functions all over the flow can reach it without threading a bot reference
# through every signature). _FALLBACK covers any object that rejects setattr.
_ATTR = "_vfs_guard"
_FALLBACK = {}

# Path fragments that mean "VFS navigated us off the app and onto an error view".
# Matched against the URL PATH only (never the query string, where a harmless
# ?returnUrl=/error would otherwise trip it).
_ERROR_PATH_MARKERS = (
    "page-not-found", "pagenotfound", "not-found", "notfound",
    "error", "accessdenied", "access-denied",
    "unauthorized", "unauthorised", "permission",
    "session-expired", "sessionexpired", "blocked",
)

# Document-request statuses that mean the server refused this navigation.
# 5xx is handled by a range check alongside these.
_BAD_STATUS = frozenset({401, 403, 404, 407, 429})


class _GuardState:
    """Per-page suspicion flag. `reason` is "" while the session looks healthy."""

    __slots__ = ("reason",)

    def __init__(self):
        self.reason = ""


# ===== state plumbing =======================================================


def _state(page):
    """The page's guard state, or None if the guard was never installed."""
    try:
        state = getattr(page, _ATTR, None)
    except Exception:
        state = None
    return state if state is not None else _FALLBACK.get(id(page))


def _set_state(page, state) -> None:
    try:
        setattr(page, _ATTR, state)
    except Exception:
        _FALLBACK[id(page)] = state


def suspect(page, reason: str) -> None:
    """Flag the session as suspicious. Only the FIRST reason is kept — it is the
    one closest to the cause; later ones are just fallout from the same event."""
    state = _state(page)
    if state is not None and not state.reason:
        state.reason = reason


def suspicion(page) -> str:
    """The pending suspicion reason, or ""."""
    state = _state(page)
    return state.reason if state is not None else ""


def clear(page) -> None:
    """Drop a suspicion the DOM did not confirm, so it can't fire repeatedly."""
    state = _state(page)
    if state is not None:
        state.reason = ""


# ===== URL classification ===================================================


def _path_reason(url: str) -> str:
    """The error-route marker matched by `url`'s PATH, or "". Pure function."""
    try:
        path = (urlparse(url or "").path or "").lower()
    except Exception:
        return ""
    for marker in _ERROR_PATH_MARKERS:
        if marker in path:
            return marker
    return ""


def error_url_reason(page) -> str:
    """Non-empty if the browser is currently sitting on a VFS error route.

    Free: Playwright caches page.url locally, so this costs no round-trip to the
    browser and is safe to call on every checkpoint.
    """
    try:
        url = page.url or ""
    except Exception:
        return ""
    marker = _path_reason(url)
    return f"navigated to an error page ('{marker}' in {url})" if marker else ""


# ===== installation (Layer 1) ===============================================


def install(page) -> list:
    """Attach the passive sentinel to `page`.

    Returns the (target, event, handler) triples the caller must append to its
    listener list so they can be DETACHED before Chrome is torn down (a stray
    handler firing during teardown is what produces Playwright's
    'Exception in callback SyncBase._sync' noise — see VfsBot._detach_listeners).
    """
    state = _GuardState()
    _set_state(page, state)

    def _on_navigated(frame):
        # Main frame only: iframes (Turnstile, trackers) navigate constantly and
        # their URLs say nothing about whether OUR session is alive.
        try:
            if frame != page.main_frame:
                return
            marker = _path_reason(frame.url)
            if marker and not state.reason:
                state.reason = f"navigated to an error page ('{marker}' in {frame.url})"
        except Exception:
            pass

    def _on_response(resp):
        # Only VFS *navigations* — a refused document is the earliest possible
        # signal that the session is dead, arriving before the DOM even repaints.
        # Sub-resource and XHR failures are deliberately ignored here: the app
        # routinely recovers from those, and real API 403s are already captured
        # by VfsBot._attach_block_watcher (which flags us via suspect()).
        try:
            if not resp.request.is_navigation_request():
                return
            status = resp.status
            if status not in _BAD_STATUS and status < 500:
                return
            host = (urlparse(resp.url).hostname or "").lower()
            if "vfsglobal" not in host:
                return
            if not state.reason:
                state.reason = f"VFS returned HTTP {status} for {resp.url}"
        except Exception:
            pass

    listeners = []
    for event, handler in (("framenavigated", _on_navigated),
                           ("response", _on_response)):
        try:
            page.on(event, handler)
            listeners.append((page, event, handler))
        except Exception as e:  # a stub/limited page — degrade to Layer 2 only
            logging.debug(f"page_guard: could not hook '{event}' ({e}).")
    return listeners


def uninstall(page) -> None:
    """Forget the page's guard state (listeners are detached by the caller)."""
    try:
        if getattr(page, _ATTR, None) is not None:
            setattr(page, _ATTR, None)
    except Exception:
        pass
    _FALLBACK.pop(id(page), None)


# ===== assertion (Layer 2) ==================================================


def assert_alive(page, where: str = "", deep: bool = False) -> None:
    """Raise if the VFS session is dead; return (fast) if it looks healthy.

    Call this liberally — the healthy path is two attribute reads and no browser
    round-trip.

    `where` names the step for the log line ("selecting centre 'Dubai'"), so a
    block is reported at the exact point it killed the flow.

    `deep=True` forces the DOM classification even with nothing flagged. Use it
    at points where a step has ALREADY failed: Angular can swap in an error view
    client-side without a navigation event, so a failure is itself grounds to go
    and look. Everywhere else leave it False and stay free.

    Raises the most specific error available (so the supervisor's existing
    per-error handling applies unchanged):
        403201                       -> IpBlockedError       (rotate IP)
        403203 / 'Permission Issues' -> GeoBlockedError      (rotate IP)
        429002 / 429202 / 429001     -> Account* errors      (disable / bench)
        anything else recognised     -> PageBlockedError     (relaunch fresh)
    """
    reason = error_url_reason(page) or suspicion(page)
    if not reason and not deep:
        return

    # Confirm against the DOM, raising the matching typed error. Which classifier
    # runs depends on whether anything is actually suspicious:
    #   * nothing flagged (a bare deep probe, e.g. after an empty slot read) ->
    #     the body-text predicates only. This is the hot path — once per
    #     no-availability combination — so it must not pay for is_ip_blocked's
    #     full_page_text scan, which serialises the whole DOM.
    #   * something flagged -> the full classifier, that scan included, because a
    #     raw 403201 JSON body only shows up there.
    classify = (block_detection.raise_if_blocked if reason
                else block_detection.raise_if_rendered_block)
    try:
        classify(page)
    except Exception as e:
        logging.warning(
            f"VFS session blocked{f' while {where}' if where else ''}: "
            f"{type(e).__name__} — {e}"
        )
        raise

    if block_detection.is_session_expired(page):
        _die(page, "session_expired",
             "VFS 'Session Expired or Invalid' — the session was invalidated "
             "mid-flow; the page we were driving is gone.", where)

    if reason:
        # An error ROUTE is self-confirming: the app itself navigated us there.
        if error_url_reason(page):
            _die(page, "session_dead", reason, where)
        # Suspicion the DOM does not corroborate (e.g. one API 403 the app
        # recovered from). Clear it so it can't fire again, and carry on — the
        # caller's own timeouts still guard the step.
        logging.debug(
            f"page_guard: suspicion not confirmed by the page ({reason}) — "
            "clearing and continuing."
        )
        clear(page)


def _die(page, screenshot: str, message: str, where: str = "") -> None:
    context = f" while {where}" if where else ""
    logging.warning(f"VFS session lost{context}: {message}")
    diagnostics.take_final_screenshot(page, screenshot)
    raise PageBlockedError(f"{message}{context}.")
