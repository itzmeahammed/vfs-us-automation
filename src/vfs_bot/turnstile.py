"""Cloudflare Turnstile + 'Verify Captcha' dialog + the ngx-ui-loader spinner.

VFS sits behind Cloudflare, which can interrupt the flow at any point in three
ways this module deals with:

  * the Turnstile widget on the login page itself (must pass before Sign In
    enables) — token_*/ wait_for_turnstile_passed / click_turnstile_by_coords
  * the `app-cloudflare-dialog` 'Verify Captcha' popup, which can appear after
    Sign In (or at any later step) and blocks the page until dismissed —
    dismiss_captcha / wait_with_captcha_check
  * VFS's own full-screen `ngx-ui-loader` spinner, which intercepts pointer
    events while a request is in flight — wait_for_loader

All three are pure page-automation with no VfsBot instance state, hence free
functions rather than methods.
"""

import logging

from src.settings import settings
from src.vfs_bot import block_detection, diagnostics, page_guard
from src.vfs_bot.errors import GeoBlockedError, LoginBouncedError

# ===== Turnstile widget (login page) ========================================


def turnstile_token(page) -> str:
    """Returns the current cf-turnstile-response token value (or '')."""
    try:
        return page.evaluate(
            "() => { const i = document.querySelector(\"input[name='cf-turnstile-response']\");"
            " return i ? i.value : ''; }"
        ) or ""
    except Exception:
        return ""


def wait_for_turnstile_passed(page, timeout_ms: int) -> bool:
    """
    Waits until the Cloudflare Turnstile is solved — i.e. the
    cf-turnstile-response token is populated.

    This is the correct gate (NOT the Sign In button): VFS only enables Sign
    In once the token exists AND the credentials are filled, so we must wait
    on the token, then fill the form, after which Sign In enables on its own.
    """
    step_ms = 500
    waited = 0
    while waited < timeout_ms:
        if turnstile_token(page):
            logging.debug(f"Turnstile passed (token populated) after {waited/1000:.0f}s.")
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
        if waited % 10000 == 0:
            logging.debug(f"Waiting for Turnstile to pass... ({waited/1000:.0f}s)")
    return False


def wait_for_turnstile_token(page, timeout_ms: int = 15000) -> None:
    """
    Waits until the Turnstile hidden input (`cf-turnstile-response`) holds a
    non-empty token, meaning the challenge auto-solved. Falls back to a short
    fixed wait if the input can't be read (it lives in a closed shadow root on
    some pages, so its value isn't always queryable).
    """
    try:
        page.wait_for_function(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return el && el.value && el.value.length > 0;
            }""",
            timeout=timeout_ms,
        )
        logging.debug("Turnstile token populated.")
    except Exception:
        # Token not observable (closed shadow DOM) — give it a moment anyway.
        page.wait_for_timeout(3000)


def wait_for_signin_enabled(page, sign_in, timeout_ms: int = 60000) -> bool:
    """
    Polls until the Sign In button becomes enabled (Cloudflare Turnstile
    auto-solved) or the timeout elapses.

    Logs progress periodically and reports when the Turnstile token appears,
    so a stuck challenge is visible in the logs rather than a silent wait.
    Returns True if Sign In became enabled.
    """
    step_ms = 1000
    waited = 0
    token_seen = False
    while waited < timeout_ms:
        try:
            if sign_in.is_enabled():
                logging.debug(f"Sign In enabled after {waited/1000:.0f}s.")
                return True
        except Exception:
            pass

        # Surface when the Turnstile token populates (challenge solved) even
        # if the button takes another moment to flip enabled.
        if not token_seen and turnstile_token(page):
            token_seen = True
            logging.debug("Turnstile token populated (challenge passed).")

        page.wait_for_timeout(step_ms)
        waited += step_ms
        if waited % 10000 == 0:
            logging.debug(f"Waiting for Cloudflare to enable Sign In... ({waited/1000:.0f}s)")
    return False


def _turnstile_widget_box(page, scope: str = ""):
    """On-screen box {x,y,w,h} of the VISIBLE Turnstile widget, or None.

    Tries, in order: the Cloudflare challenge iframe (its element box IS the
    real widget, readable even though its content is cross-origin), then the
    `.cf-turnstile` container, then — as a legacy fallback — the hidden
    `cf-turnstile-response` input's nearest sized ancestor. `scope` restricts the
    search to a container (e.g. 'app-cloudflare-dialog ' for the captcha dialog)."""
    for sel in (f"{scope}iframe[src*='challenges.cloudflare.com']",
                f"{scope}.cf-turnstile"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                bb = loc.bounding_box()
                if bb and bb["width"] > 20 and bb["height"] > 20:
                    return {"x": bb["x"], "y": bb["y"],
                            "w": bb["width"], "h": bb["height"]}
        except Exception:
            continue
    # Legacy fallback: walk up from the hidden response input to a sized ancestor.
    try:
        box = page.evaluate(
            """() => {
                const inp = document.querySelector("input[name='cf-turnstile-response']");
                if (!inp) return null;
                let el = inp;
                for (let i = 0; i < 6 && el; i++) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 50 && r.height > 30) {
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                    }
                    el = el.parentElement;
                }
                return null;
            }"""
        )
        if box:
            return box
    except Exception:
        pass
    return None


def click_turnstile_by_coords(page, scope: str = "") -> bool:
    """
    Click the Turnstile 'Verify you are human' checkbox by coordinates. The
    challenge iframe is cross-origin-isolated so its checkbox can't be targeted
    as an element — but a real mouse click at the widget's on-screen position
    lands it. We locate the visible widget (iframe > .cf-turnstile > hidden-input
    ancestor) and click near its left edge, vertically centred (the checkbox).

    `scope` restricts the search to a container so the login widget and the
    post-Sign-In dialog widget aren't confused (e.g. 'app-cloudflare-dialog ').

    Logs at INFO — this only runs when the challenge did NOT auto-pass, so the
    file records exactly when the bot fell back to clicking the checkbox.
    """
    box = _turnstile_widget_box(page, scope)
    if not box:
        logging.debug("Turnstile widget box not found; can't coord-click.")
        return False
    x = box["x"] + 30      # checkbox is near the left edge
    y = box["y"] + box["h"] / 2
    where = "captcha dialog" if scope else "login page"
    logging.info(
        f"Turnstile did not auto-pass ({where}) — clicking the 'Verify you are "
        f"human' checkbox by coordinates ({x:.0f}, {y:.0f})."
    )
    try:
        page.mouse.move(x, y)
        page.wait_for_timeout(300)
        page.mouse.click(x, y)
        return True
    except Exception as e:
        logging.warning(f"Coordinate-click of Turnstile failed: {e}")
        return False


def _dialog_turnstile_solved(page, timeout_ms: int = 6000) -> bool:
    """True once the captcha DIALOG's own Turnstile token is populated.

    Scoped to `app-cloudflare-dialog` so a solved LOGIN-page token isn't mistaken
    for the dialog's (they share the input name). If the dialog is already gone,
    there's nothing to wait on — treated as solved."""
    try:
        page.wait_for_function(
            """() => {
                const d = document.querySelector('app-cloudflare-dialog');
                if (!d) return true;
                const el = d.querySelector('input[name="cf-turnstile-response"]');
                return !!(el && el.value && el.value.length > 0);
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


# ===== 'Verify Captcha' dialog (app-cloudflare-dialog) =======================


def captcha_visible(page) -> bool:
    """True if the Cloudflare captcha dialog is currently showing."""
    try:
        dialog = page.locator("app-cloudflare-dialog")
        return dialog.count() > 0 and dialog.first.is_visible()
    except Exception:
        return False


def dismiss_captcha(page) -> bool:
    """
    Dismisses the Cloudflare 'Verify Captcha' dialog (`app-cloudflare-dialog`)
    if it is showing. The Turnstile widget auto-solves, so we just need to
    click its 'Submit' button. Returns immediately (and silently) when no
    dialog is present.

    Returns True if a dialog was present and handled, False if there was none —
    so a caller polling in a loop can COUNT how many times it had to solve the
    dialog and give up on a Cloudflare re-challenge loop (see
    await_dashboard_handling_captcha).
    """
    try:
        dialog = page.locator("app-cloudflare-dialog")
        if dialog.count() == 0 or not dialog.first.is_visible():
            return False
    except Exception:
        return False

    _do_dismiss_captcha(page)
    return True


def solve_captcha_dialog(page) -> bool:
    """Solve the 'Verify Captcha' dialog (`app-cloudflare-dialog`) if it's up:
    wait for its Turnstile token and click Submit, retrying a few rounds.

    Unlike dismiss_captcha (which reports only whether a dialog was PRESENT, so
    the dashboard loop can count re-challenge cycles), this reports whether the
    dialog was actually CLEARED. Returns True only if a dialog was present and
    it went away; False if there was no dialog or it couldn't be solved. Use it
    where the caller needs to know if solving succeeded (e.g. the post-Sign-In
    403 path).
    """
    try:
        dialog = page.locator("app-cloudflare-dialog")
        if dialog.count() == 0 or not dialog.first.is_visible():
            return False
    except Exception:
        return False
    return _do_dismiss_captcha(page)


def _do_dismiss_captcha(page) -> bool:
    """
    Clears a confirmed-visible Cloudflare captcha dialog. Returns True if the
    dialog was cleared, False if it couldn't be (Submit unclickable / still up
    after all retries).

    The Turnstile widget shows 'Verifying...' for a few seconds and only
    populates its hidden `cf-turnstile-response` token once solved — clicking
    Submit before then does nothing. So we wait for that token to appear,
    then click Submit, and retry the whole cycle a few times if the dialog
    is still up (it can require more than one round).
    """
    dialog = page.locator("app-cloudflare-dialog")
    logging.info("Cloudflare 'Verify Captcha' dialog detected — handling it.")

    for attempt in range(1, 4):  # up to 3 Submit cycles
        # Wait briefly for the dialog's Turnstile to auto-solve; if it doesn't,
        # click its checkbox — a 'managed' challenge won't populate its token (so
        # Submit does nothing) until it's actually clicked. Scoped to the dialog
        # so we target ITS widget, not the already-solved login-page one.
        if not _dialog_turnstile_solved(page, timeout_ms=6000):
            if click_turnstile_by_coords(page, scope="app-cloudflare-dialog "):
                solved = _dialog_turnstile_solved(page, timeout_ms=8000)
                logging.info(
                    "Captcha dialog: Turnstile "
                    + ("passed after checkbox click." if solved
                       else "still not passed after checkbox click — submitting anyway.")
                )
        try:
            submit = dialog.get_by_role("button", name="Submit").first
            try:
                submit.click(timeout=10000)
            except Exception:
                # Slow EC2 / overlay — force, then JS-dispatch as last resort.
                try:
                    submit.click(force=True, timeout=10000)
                except Exception:
                    submit.evaluate("el => el.click()")
            logging.debug(f"Clicked captcha 'Submit' (attempt {attempt})")
        except Exception as e:
            logging.warning(f"Could not click captcha 'Submit': {e}")
            diagnostics.take_screenshot(page, "ERROR_captcha")
            return False

        # Did the dialog go away?
        try:
            dialog.first.wait_for(state="hidden", timeout=12000)
            logging.info(
                f"Cloudflare 'Verify Captcha' dialog SOLVED (cleared on Submit "
                f"attempt {attempt})."
            )
            diagnostics.take_screenshot(page, "captcha_handled")
            return True
        except Exception:
            # Still visible (e.g. token wasn't ready yet) — loop and retry.
            if not captcha_visible(page):
                return True  # raced away on its own
            logging.debug(
                f"Captcha still visible after Submit (attempt {attempt}); retrying..."
            )

    logging.warning(
        "Cloudflare 'Verify Captcha' dialog NOT solved — still visible after all "
        "Submit retries; it may need a manual solve."
    )
    diagnostics.take_screenshot(page, "ERROR_captcha_persist")
    return False


def wait_with_captcha_check(page, total_ms: int, step_ms: int = 3000) -> None:
    """
    Sleeps for `total_ms`, checking for (and dismissing) the Cloudflare captcha
    dialog every `step_ms`. Use for long idle waits where the dialog could pop
    up while the bot is otherwise doing nothing.
    """
    elapsed = 0
    while elapsed < total_ms:
        page.wait_for_timeout(min(step_ms, total_ms - elapsed))
        elapsed += step_ms
        dismiss_captcha(page)


# ===== VFS's own 'please wait' reminder dialog ===============================


def dismiss_wait_dialog(page) -> None:
    """
    Dismisses VFS's intermittent reminder dialogs that block a step — e.g.
    'Please wait for some time before saving and continuing'. These are
    mat-dialogs whose only action is a 'Continue' (or 'OK') button; clicking
    it lets the flow proceed. Silent no-op when none is present.
    """
    try:
        dialog = page.locator("mat-dialog-container, .mat-mdc-dialog-container")
        if dialog.count() == 0 or not dialog.first.is_visible():
            return
        text = (dialog.first.inner_text() or "").lower()
    except Exception:
        return

    # Only handle the informational 'wait/reminder' dialogs here — leave the
    # Cloudflare captcha dialog to its dedicated handler.
    if "captcha" in text:
        return
    if not any(k in text for k in ("wait", "reminder", "received", "please")):
        return

    for label in ("Continue", "OK", "Ok", "Close"):
        try:
            btn = dialog.first.get_by_role("button", name=label).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                logging.debug(f"Dismissed VFS reminder dialog via '{label}'.")
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue


# ===== ngx-ui-loader spinner =================================================


def wait_for_loader(page, timeout: int = 30000) -> None:
    """
    Waits for VFS's ngx-ui-loader overlay to clear. This full-screen spinner
    intercepts pointer events, so clicking while it is up times out. Returns
    immediately if no loader is present.

    Also clears any Cloudflare 'Verify Captcha' dialog and the VFS 'please
    wait before continuing' reminder first — either can pop up at any step
    and blocks the form until dismissed.

    Raises (via page_guard) if VFS has killed the session: this is the single
    most-called function in the slot-check flow, so the free sentinel check here
    gives near-continuous liveness coverage for no cost. Without it we would sit
    out the full `timeout` waiting for a spinner on a page that no longer exists.
    """
    page_guard.assert_alive(page, "waiting for the VFS loader")
    dismiss_captcha(page)
    dismiss_wait_dialog(page)
    try:
        page.locator("ngx-ui-loader .ngx-overlay.loading-foreground").wait_for(
            state="hidden", timeout=timeout
        )
    except Exception:
        pass  # loader absent or already cleared


# ===== Post Sign-In redirect =================================================


def dashboard_url_reached(page) -> bool:
    """True when the browser URL is actually the VFS dashboard.

    The check is deliberately strict — it matches '/dashboard' as a path
    segment, not merely anywhere in the string — so a stray query/fragment
    can't fake it. This is the *authoritative* gate: we only treat the
    dashboard as reached once the URL says so.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    return "/dashboard" in url


def dashboard_content_ready(page) -> bool:
    """True once the dashboard has actually RENDERED its interactive content,
    not just navigated to the /dashboard URL.

    Cloudflare frequently leaves us parked on the dashboard URL with a blank /
    still-loading body (a Turnstile challenge quietly running underneath). In
    that state the old URL-only check reported 'reached', the flow marched on,
    and every downstream dropdown timed out. We require, in addition to the URL:
      * NO 'Verify Captcha' dialog still up (the redirect hasn't truly settled),
      * the dashboard's own 'Start New Booking' button present in the DOM
        (the one thing the very next step clicks — if it isn't there, the
        dashboard hasn't rendered).
    """
    if not dashboard_url_reached(page):
        return False
    try:
        # A captcha dialog still showing means we're mid-challenge, not done.
        if captcha_visible(page):
            return False
        # The dashboard's primary action. Presence in the DOM (not necessarily
        # on-screen — a loader overlay may still cover it) is enough to prove
        # the dashboard app rendered rather than a blank Cloudflare shell.
        return page.locator("button:has-text('Start New Booking')").count() > 0
    except Exception:
        return False


def login_form_showing(page) -> bool:
    """True if the sign-in form is on screen, settled and usable again.

    Called only AFTER Sign In has been clicked, where it means the login
    BOUNCED: VFS threw us back to the form instead of sending an OTP or loading
    the dashboard. That state is otherwise indistinguishable from "still
    loading" until some other timeout expires, which is why runs used to sit
    there for 60-90s before mislabelling the failure.

    Every clause exists to rule out a page that is merely mid-flight:
      * no 'Verify Captcha' dialog — a challenge is still in progress;
      * a VISIBLE password field — the login form's signature (the OTP step has
        a code input, the dashboard has neither);
      * no full-screen ngx-loader — the Sign In request is still running;
      * Sign In present AND re-enabled — Angular only re-enables the button once
        it has given up on the submission.
    """
    try:
        if captcha_visible(page):
            return False
        pw = page.locator("input[type='password']").filter(visible=True)
        if pw.count() == 0:
            return False
        # .first matters: is_visible() is strict, so a page rendering more than
        # one loader overlay would raise, get swallowed below, and silently
        # disable bounce detection altogether.
        if page.locator(
                "ngx-ui-loader .ngx-overlay.loading-foreground").first.is_visible():
            return False
        btn = page.get_by_role("button", name="Sign In").first
        return btn.count() > 0 and btn.is_visible() and btn.is_enabled()
    except Exception:
        return False


def await_dashboard_handling_captcha(page, timeout_ms: int = 90000,
                                      on_poll=None) -> bool:
    """
    Waits for the dashboard while continuously dismissing the Cloudflare
    'Verify Captcha' dialog, which can pop up at any time during the post-
    Sign-In redirect and blocks it until its Submit is clicked.

    Returns True only once the dashboard is BOTH at the /dashboard URL AND has
    rendered its content (see dashboard_content_ready) — not on the URL alone,
    which Cloudflare can reach with a blank body. Returns False on timeout.

    `on_poll` (optional) is called each iteration and may raise. The bot passes
    its network-block classifier, so a VFS 403 that arrives after the post
    Sign-In check — routine on a slow proxy — is read here rather than sitting
    unexamined while this polls for a dashboard that will never load. Without
    it, a 403201 IP block reached this function as an unexplained timeout.
    """
    step_ms = 1000
    waited = 0
    captcha_cycles = 0
    url_logged = False
    loader_up = 0  # B: how long the ngx loader has been continuously stuck
    max_cycles = settings().retry.dashboard_captcha_cycles
    frozen_ms = settings().timeouts.dashboard_frozen_ms
    bounce_grace_ms = settings().timeouts.login_bounce_grace_ms
    while waited < timeout_ms:
        # Already there — URL AND content both ready?
        try:
            if dashboard_url_reached(page):
                if not url_logged:
                    logging.info(
                        f"Dashboard URL reached ({page.url}) — verifying the page "
                        "actually rendered before proceeding."
                    )
                    url_logged = True
                if dashboard_content_ready(page):
                    logging.info("Dashboard content rendered — verified reached.")
                    return True
        except Exception:
            pass
        # Read any VFS 403 captured since Sign In, so a late one is classified
        # (403201 IP block vs rejected token) instead of expiring as a timeout.
        if on_poll is not None:
            on_poll()
        # Stop early if VFS served its 'Permission Issues (403203)' geo-block
        # page — retrying won't help (same IP), so raise to abort immediately.
        if block_detection.is_geo_blocked(page):
            diagnostics.take_final_screenshot(page, "geo_blocked")
            raise GeoBlockedError(
                "VFS 'Permission Issues (403203)' — IP outside permitted "
                "location or rate-limited. Not retrying."
            )
        # Stop early on any account/route block page (429002 denied / 429202
        # locked / 429001 restricted) — a block, not a slow dashboard.
        block_detection.raise_if_blocked(page)
        # Stop early if this email isn't registered for this site — no point
        # waiting; the caller skips this URL for this account.
        if block_detection.is_email_not_registered(page):
            return False
        # Stop early on a wrong-credentials banner too (no point waiting 90s).
        if block_detection.is_invalid_credentials(page):
            return False
        # Stop early on VFS's 'Session Expired or Invalid' (/page-not-found) page.
        # The dashboard will NEVER load from here (stale cookies / cf_clearance,
        # often after an egress-IP change), so bail on the first poll instead of
        # grinding the full ~90s timeout. The caller raises DashboardNotReachedError
        # (retryable) — a fresh browser on the next attempt can re-establish it.
        if block_detection.is_session_expired(page):
            logging.warning(
                "VFS 'Session Expired or Invalid' page after Sign In — the "
                "dashboard cannot load from here; failing fast (no 90s wait)."
            )
            return False
        # Sign In bounced straight back to the login form. The dashboard will
        # never arrive, so stop polling for it: raise the classified error and
        # let the caller reload + re-solve on this IP, then rotate. The grace
        # period keeps us from mistaking the form that is still on screen during
        # the submission itself for a bounce.
        if waited >= bounce_grace_ms and login_form_showing(page):
            logging.warning(
                f"Sign In bounced back to the login form after "
                f"{waited / 1000:.0f}s (no OTP, no dashboard, no error banner) "
                "— not waiting out the dashboard timeout."
            )
            diagnostics.take_final_screenshot(page, "login_bounced")
            raise LoginBouncedError(
                "Sign In returned to the login form with no error shown — the "
                "login was silently refused (stale/rejected Cloudflare token)."
            )
        # B: fast-bail a "solved but frozen" redirect. After the captcha clears,
        # Cloudflare can leave the app wedged behind a stuck ngx-loader overlay
        # that never redirects to the dashboard (no dialog, no block page — just
        # a dead spinner intercepting clicks). Waiting the full dashboard_ms here
        # just burns ~90s on a session that will never load, so if the loader
        # stays up continuously (and no captcha dialog is mid-solve) for
        # frozen_ms, abandon this attempt — a fresh IP/browser breaks it.
        if frozen_ms:
            try:
                loader_visible = (
                    page.locator(
                        "ngx-ui-loader .ngx-overlay.loading-foreground"
                    ).first.is_visible()          # .first: is_visible() is strict
                    and not captcha_visible(page)
                )
            except Exception:
                loader_visible = False
            if loader_visible:
                loader_up += step_ms
                if loader_up >= frozen_ms:
                    logging.warning(
                        f"Post-login redirect frozen — loading spinner stuck for "
                        f"{loader_up / 1000:.0f}s with no dashboard (session wedged "
                        "behind Cloudflare); bailing fast so a fresh IP/browser "
                        "can retry."
                    )
                    diagnostics.take_final_screenshot(page, "dashboard_frozen")
                    return False
            else:
                loader_up = 0
        # Clear the captcha dialog if it's blocking the redirect. If Cloudflare
        # keeps RE-PRESENTING it after each solve (a re-challenge loop), stop:
        # every re-solve re-downloads the challenge (megabytes) and never
        # redirects. Bail after a few cycles so the supervisor relaunches fresh
        # (a different IP usually breaks the loop) instead of grinding the full
        # timeout.
        if dismiss_captcha(page):
            captcha_cycles += 1
            if captcha_cycles >= max_cycles:
                logging.warning(
                    f"Cloudflare captcha re-challenge loop — the 'Verify Captcha' "
                    f"dialog re-appeared after {captcha_cycles} solve cycle(s) and "
                    f"the dashboard never loaded; abandoning this attempt."
                )
                diagnostics.take_final_screenshot(page, "captcha_loop")
                return False
        page.wait_for_timeout(step_ms)
        waited += step_ms
        if waited % 20000 == 0:
            logging.debug(f"Waiting for dashboard (handling captcha)... ({waited/1000:.0f}s)")
    # Final check — same strict gate: URL alone is not enough.
    if dashboard_url_reached(page) and not dashboard_content_ready(page):
        logging.warning(
            "Timed out on the /dashboard URL but its content never rendered "
            "(Cloudflare shell / blank body) — treating as NOT reached."
        )
    return dashboard_content_ready(page)
