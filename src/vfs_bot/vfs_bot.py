import logging
import os
import time
from abc import ABC
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value
from src.utils.route_schema import get_route_schema


def _browser_activity_enabled() -> bool:
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

SCREENSHOT_DIR = "screenshots"

# When False, the per-step `_take_screenshot` calls are no-ops; only the single
# final screenshot (taken at the end of run()) is written. Flip to True for
# step-by-step debugging.
SCREENSHOTS_ENABLED = False

# How many times to RELOAD the login page to retry a stuck Cloudflare Turnstile
# before giving up (a reload re-runs the challenge and often unsticks it). This
# is a cheap inner retry within one browser; the supervisor's browser relaunch
# is the outer retry.
TURNSTILE_REFRESH_ATTEMPTS = 2

USERNAME_SELECTOR = (
    "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
)
PASSWORD_SELECTOR = (
    "input[formcontrolname='password'], #mat-input-1, input[type='password']"
)
# The OTP entry field shown after Sign In on routes with "otp": true. Several
# strategies because the exact markup hasn't been pinned down yet ([i] = case-
# insensitive attribute match).
OTP_INPUT_SELECTOR = (
    "input[formcontrolname*='otp' i], input[autocomplete='one-time-code'], "
    "input[name*='otp' i], input[id*='otp' i], input[placeholder*='otp' i]"
)


class LoginError(Exception):
    """Exception raised when login fails."""


class RetryableError(Exception):
    """
    Base for failures the supervisor should retry by relaunching a fresh browser.

    The hourly EC2 run treats every failure mode below as "tear down Chrome and
    try the whole flow again", since a fresh browser is the most reliable way to
    get a clean Cloudflare pass / recover from a dead page.
    """


class CdpConnectError(RetryableError):
    """Could not connect to Chrome over CDP (Chrome not up / port not ready)."""


class LoginFormNotReadyError(RetryableError):
    """The login form never appeared (Cloudflare spinner / 403 / slow load)."""


class SignInDisabledError(RetryableError):
    """The Sign In button stayed disabled — Cloudflare 'Verify you are human'
    was not passed, so login can't proceed."""


class DashboardNotReachedError(RetryableError):
    """Sign In was clicked but the dashboard never loaded (bad creds, captcha,
    or a slow/blocked redirect)."""


class SlotCheckError(RetryableError):
    """Reached the appointment step but couldn't complete the slot check."""


class GeoBlockedError(Exception):
    """
    VFS returned its 'Permission Issues (403203)' page — the request is blocked
    because the IP is outside the permitted location (or rate-limited).

    This is NOT a RetryableError on purpose: retrying with a fresh browser uses
    the SAME IP, so it would fail identically. The supervisor should log it and
    stop immediately instead of burning attempts.
    """


class IpBlockedError(Exception):
    """
    VFS returned code 403201 — an IP-BASED block (too many requests from this IP,
    or a flagged/datacenter IP). It is tied to the IP, not the account.

    Retrying on the SAME IP is pointless, but a DIFFERENT IP may work — so the
    supervisor rotates to another proxy and tries once more, without penalising
    the account's health.
    """


class EmailNotRegisteredError(Exception):
    """
    The login page showed 'The entered email id is not registered with us' — the
    current account simply isn't registered for THIS portal.

    NOT retryable (the same email will never work here) and NOT an error worth
    alerting about — the supervisor should just SKIP this URL for this account
    and move on to the next URL.
    """


class InvalidCredentialsError(Exception):
    """
    The login page showed an 'email or password is incorrect' error — the
    account's password is wrong (or the account is locked/disabled).

    NOT retryable: the same wrong credentials fail identically every time, so the
    supervisor STOPS this run immediately (no retries) and sends an alert to the
    error channel instead of burning attempts.
    """


class AccountLockedError(Exception):
    """
    VFS served its 'Account Locked (429202)' page — too many requests in a short
    period; access auto-resets after a cooldown (typically ~2 hours).

    NOT retryable: retrying immediately hits the same lock. The supervisor stops
    this run and reports the exact on-page message to the summary chat.
    """


class OtpVerificationError(RetryableError):
    """
    The OTP step failed on a route with "otp": true — the OTP field never
    appeared, the email never arrived, or the code couldn't be read/entered.

    Retryable: a fresh browser re-triggers Sign In, which sends a fresh OTP.
    """


class AccessRestrictedError(Exception):
    """
    VFS served its 'Access Restricted' block page for this route — the portal
    has (temporarily) cut off access here.

    NOT retryable within a run: hammering the route only prolongs the
    restriction. The supervisor skips this route immediately and moves on to
    the next one (same account); the route is tried again fresh on the next
    scheduled run (e.g. hit at :29 -> retried at :59).
    """


class AccountBlockedError(Exception):
    """
    VFS served its 'Access Denied Due to Unauthorised Activity (429002)' page —
    the account has been blocked, typically after repeated wrong-credential
    attempts. It needs a HUMAN fix (correct the password / unlock the account),
    not a timed cooldown.

    NOT retryable. The supervisor DISABLES the account indefinitely (skipped by
    selection) and alerts, until someone fixes it and flags it healthy
    (python -m src.utils.account_health clear <email>).
    """


class VfsBot(ABC):
    """
    Slot-check bot for the VFS Malta portal.

    This is the trimmed, production slice of the original schema-driven VFS bot:
    it only reaches Step 1 (Appointment Details), reads the 'Earliest available
    slot' banner for each configured combination, and reports the results via
    Telegram. The booking/OTP/payment machinery is intentionally absent — this
    project never books anything.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None
        self.schema = {}
        # Populated by the slot-check flow with the (label, message) pairs for
        # every combination checked, so the supervisor can build a run summary.
        self.slot_results = []
        # The account actually used this run (set once selected), so the
        # supervisor can update that account's health after the run.
        self.active_email = None
        # Optional credential injected by the supervisor (which selects it once,
        # skipping benched accounts). When set, the bot uses it instead of
        # selecting its own — keeps selection in ONE place.
        self._cred_override = None
        # Set by the network watcher when VFS returns an HTTP 403 (code in the
        # body if we could read it) — caught even if the page then closes.
        self._block_code = None

    def set_credential(self, email: str, password: str) -> None:
        """Supervisor-provided credential to use for this run (overrides self-select)."""
        self._cred_override = (email, password)

    def _attach_block_watcher(self, page) -> None:
        """
        Watch network responses for an HTTP 403 (VFS's IP/access blocks —
        403201 / 403203). Records the code on self._block_code so the flow can
        fail FAST and rotate the IP, even if the page dies right after.
        """
        def _on_resp(resp):
            # Do NOT read the body here (sync handler -> deadlock risk). Flag only
            # a 403 from a VFS API/XHR call (that's the 403201 block) — not
            # Cloudflare challenge documents or third-party assets.
            try:
                if resp.status != 403:
                    return
                url = (resp.url or "").lower()
                if "vfsglobal" not in url:
                    return
                try:
                    rtype = resp.request.resource_type
                except Exception:
                    rtype = ""
                if rtype in ("xhr", "fetch", ""):
                    self._block_code = "403"
                    logging.warning(f"VFS HTTP 403 ({rtype or '?'}) from {resp.url} "
                                    "— treating as IP block.")
            except Exception:
                pass
        try:
            page.on("response", _on_resp)
        except Exception:
            pass

    def _check_blocked(self, page) -> None:
        """Raise the right block error from the DOM (specific code) or a seen 403
        response (generic IP block). No-op if not blocked. Call inside wait loops
        to fail fast."""
        VfsBot._raise_if_blocked(page)  # DOM: 403201 / 403203 / 429xxx (specific)
        if self._block_code:
            raise IpBlockedError(
                "VFS HTTP 403 — IP blocked (too many requests / flagged IP). Rotate IP.")

    def run(self) -> bool:
        """
        Connects to a browser over CDP, navigates to the VFS login URL, logs in,
        starts a new booking and runs the slot-check flow.

        On the EC2/supervisor path, Chrome is launched and killed by the
        supervisor (see src/supervisor.py); this method only attaches to it.

        Returns:
            bool: True if the slot check completed and a report was produced.

        Raises:
            RetryableError (and subclasses): on any failure the supervisor should
            retry with a fresh browser — CDP connect failure, login form never
            ready, Sign In disabled (Cloudflare), dashboard not reached, or a
            slot-check failure.
        """
        logging.info(
            f"Starting VFS Slot Checker for "
            f"{self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        # Load the per-route flow schema (the combinations to check).
        self.schema = get_route_schema(
            self.source_country_code, self.destination_country_code
        )

        browser_type = get_config_value("browser", "type", "chromium")
        headless_mode = get_config_value("browser", "headless", "True")
        url_key = self.source_country_code + "-" + self.destination_country_code
        vfs_url = get_config_value("vfs-url", url_key)
        if not vfs_url:
            logging.error(
                f"No VFS URL configured for '{url_key}'. Add it to config/vfs_urls.ini"
            )
            return False

        # Use the supervisor-injected credential if present (it selects once,
        # skipping benched accounts); else self-select for this hour AND route
        # (each route rotates through its registered accounts — see credentials.py).
        if self._cred_override:
            email_id, password = self._cred_override
        else:
            from src.utils import credentials
            email_id, password = credentials.get_credential(url_key.upper())
        if not email_id or not password:
            raise LoginError(
                f"No credential is registered for route '{url_key.upper()}' — "
                "add it to a [credN] 'routes' list in config/credentials.local.ini."
            )
        self.active_email = email_id

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        with sync_playwright() as p:
            cdp_url = get_config_value("browser", "cdp_url")
            if cdp_url:
                # Attach to an existing Chrome launched with --remote-debugging-port.
                # The supervisor launches/kills that Chrome and injects its cdp_url
                # at runtime; we only attach here.
                logging.debug(f"Connecting to Chrome via CDP: {cdp_url}")
                try:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                except Exception as e:
                    raise CdpConnectError(
                        f"Could not connect to Chrome at {cdp_url}: {e}"
                    ) from e
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                # Drop only VFS's stale login/session cookies while KEEPING
                # Cloudflare's clearance (cf_clearance / __cf*). The persistent
                # profile keeps cf_clearance so we don't get a fresh 403, but its
                # old VFS session would otherwise land us on "Session Expired" —
                # clearing it makes each run log in fresh.
                VfsBot._clear_site_session(context)
                # Reuse Chrome's startup tab if present, else open one. Either way
                # we (re)navigate below so the page loads with clearance kept but
                # the VFS session gone.
                page = context.pages[0] if context.pages else context.new_page()
            else:
                # Launch our own browser (the prod path — headless on a server).
                is_headless = headless_mode in ("True", "true")
                launch_args = {}
                if browser_type == "chromium":
                    launch_args["args"] = [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ]
                browser = getattr(p, browser_type).launch(
                    headless=is_headless, **launch_args
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()
                stealth_sync(page)

            VfsBot._attach_activity_logging(page)
            # Watch for HTTP 403 (IP/access blocks) at the network level, so we
            # catch 403201 even when it's an XHR/JSON that never renders (or the
            # page dies right after).
            self._block_code = None
            self._attach_block_watcher(page)

            # Pin a fixed viewport so the page layout (and thus the Turnstile
            # checkbox position for the coordinate click) is deterministic.
            try:
                page.set_viewport_size({"width": 1280, "height": 1024})
            except Exception as e:
                logging.debug(f"Could not set viewport: {e}")

            # Always (re)load the login URL fresh — we just cleared the VFS
            # session, so this loads the clean login page with Cloudflare
            # clearance still intact.
            logging.debug(f"Navigating to {vfs_url}")
            page.goto(vfs_url, timeout=60000, wait_until="domcontentloaded")

            # Bail immediately if the very first load was a block page.
            self._check_blocked(page)
            self.pre_login_steps(page)

            try:
                self.login(page, email_id, password)
            except (GeoBlockedError, EmailNotRegisteredError, InvalidCredentialsError,
                    AccountLockedError, AccessRestrictedError, AccountBlockedError,
                    IpBlockedError):
                # Non-retryable ON THIS IP/account & expected: geo-block, email not
                # registered, wrong password, account locked/restricted/blocked, or
                # 403201 IP block (the supervisor rotates the IP for that one).
                # Propagate so the supervisor handles it without a same-IP retry.
                raise
            except RetryableError:
                # A classified, expected failure — screenshot the end state and
                # let it bubble up so the supervisor retries with a fresh browser.
                self._take_final_screenshot(page, "final")
                raise
            except Exception as e:
                # Anything unclassified (incl. Playwright TargetClosedError when
                # the page/browser died mid-flow) is also retryable.
                self._take_final_screenshot(page, "final")
                raise RetryableError(f"Unexpected flow error: {e}") from e

            # Single final screenshot capturing the successful end state.
            self._take_final_screenshot(page, "final")
            logging.info("Slot check complete. Run finished.")
            return True

    # ------------------------------------------------------------------ #
    # Flow steps                                                          #
    # ------------------------------------------------------------------ #

    def pre_login_steps(self, page) -> None:
        """
        Accept ALL cookies on the consent banner. We deliberately ACCEPT (never
        reject) — both as the desired behaviour and because the banner overlays
        the bottom of the page and blocks the login fields until cleared.

        The banner often appears a few seconds AFTER the form, so we poll for it
        for a short while, and we click via several strategies (the OneTrust id,
        an exact-text 'Accept Cookies' button/link, force-click) because a single
        role-based lookup was missing it.
        """
        VfsBot._accept_cookies(page, attempts=8, interval_ms=1000)

    @staticmethod
    def _accept_cookies(page, attempts: int = 8, interval_ms: int = 1000) -> bool:
        """Polls for the cookie banner and clicks Accept. Returns True if clicked."""
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
                        page.wait_for_timeout(600)
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
                    page.wait_for_timeout(600)
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
                        page.wait_for_timeout(600)
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

    @staticmethod
    def _clear_site_session(context) -> None:
        """
        Clears VFS's stale login/session cookies while KEEPING Cloudflare's
        clearance cookies (cf_clearance / __cf*).

        Why: we run on a persistent Chrome profile so Cloudflare's clearance
        survives between runs (avoiding a fresh 403 on a datacenter IP). But that
        same profile would carry an expired VFS *login* session, landing us on
        "Session Expired or Invalid". So we surgically drop the VFS cookies and
        re-keep the Cloudflare ones by re-adding them after a full clear.
        """
        try:
            all_cookies = context.cookies()
        except Exception as e:
            logging.warning(f"Could not read cookies to clear session: {e}")
            return

        def _is_cloudflare(c) -> bool:
            name = c.get("name", "")
            return name in VfsBot._KEEP_COOKIE_NAMES or any(
                name.startswith(p) for p in VfsBot._KEEP_COOKIE_PREFIXES
            )

        keep = [c for c in all_cookies if _is_cloudflare(c)]
        dropped = len(all_cookies) - len(keep)

        try:
            context.clear_cookies()  # nukes everything...
            if keep:
                context.add_cookies(keep)  # ...then restore Cloudflare's only
            logging.debug(
                f"Cleared {dropped} VFS session cookie(s); kept {len(keep)} "
                f"Cloudflare cookie(s)."
            )
        except Exception as e:
            logging.warning(f"Failed to selectively clear cookies: {e}")

    @staticmethod
    def _wait_for_signin_enabled(page, sign_in, timeout_ms: int = 60000) -> bool:
        """
        Polls until the Sign In button becomes enabled (Cloudflare Turnstile
        auto-solved) or the timeout elapses.

        Logs progress periodically and reports when the Turnstile token appears,
        so a stuck challenge is visible in the logs rather than a silent wait.
        Returns True if Sign In became enabled.
        """
        step_ms = 2000
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
            if not token_seen:
                try:
                    val = page.evaluate(
                        "() => { const i = document.querySelector(\"input[name='cf-turnstile-response']\");"
                        " return i ? i.value : ''; }"
                    )
                    if val:
                        token_seen = True
                        logging.debug("Turnstile token populated (challenge passed).")
                except Exception:
                    pass

            page.wait_for_timeout(step_ms)
            waited += step_ms
            if waited % 10000 == 0:
                logging.debug(f"Waiting for Cloudflare to enable Sign In... ({waited/1000:.0f}s)")
        return False

    @staticmethod
    def _turnstile_token(page) -> str:
        """Returns the current cf-turnstile-response token value (or '')."""
        try:
            return page.evaluate(
                "() => { const i = document.querySelector(\"input[name='cf-turnstile-response']\");"
                " return i ? i.value : ''; }"
            ) or ""
        except Exception:
            return ""

    @staticmethod
    def _wait_for_turnstile_passed(page, timeout_ms: int) -> bool:
        """
        Waits until the Cloudflare Turnstile is solved — i.e. the
        cf-turnstile-response token is populated.

        This is the correct gate (NOT the Sign In button): VFS only enables Sign
        In once the token exists AND the credentials are filled, so we must wait
        on the token, then fill the form, after which Sign In enables on its own.
        """
        step_ms = 1000
        waited = 0
        while waited < timeout_ms:
            if VfsBot._turnstile_token(page):
                logging.debug(f"Turnstile passed (token populated) after {waited/1000:.0f}s.")
                return True
            page.wait_for_timeout(step_ms)
            waited += step_ms
            if waited % 10000 == 0:
                logging.debug(f"Waiting for Turnstile to pass... ({waited/1000:.0f}s)")
        return False

    @staticmethod
    def _click_turnstile_by_coords(page) -> bool:
        """
        Attempts to click the Turnstile 'Verify you are human' checkbox by
        coordinates. The challenge iframe is cross-origin-isolated so its
        checkbox can't be targeted as an element — but a real mouse click at the
        widget's on-screen position can still land it.

        We anchor on the `cf-turnstile-response` token input (always present),
        find the nearest sized ancestor (the visible widget), and click near its
        left edge, vertically centred (where the checkbox renders).
        """
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
            if not box:
                logging.debug("Turnstile widget box not found; can't coord-click.")
                return False
            x = box["x"] + 30      # checkbox is near the left edge
            y = box["y"] + box["h"] / 2
            logging.debug(f"Clicking Turnstile checkbox by coordinates ({x:.0f}, {y:.0f}).")
            page.mouse.move(x, y)
            page.wait_for_timeout(300)
            page.mouse.click(x, y)
            return True
        except Exception as e:
            logging.warning(f"Coordinate-click of Turnstile failed: {e}")
            return False

    @staticmethod
    def _fill_field(page, locator, value: str) -> None:
        """
        Sets an input's value robustly, immune to overlays and Xvfb hangs.

        Tries Playwright fill() first (bounded timeout). If that's blocked (an
        overlay intercepting actionability), falls back to setting the value via
        JS and dispatching the 'input'/'change' events Angular listens for, so
        the form model updates even without a real focus/click.
        """
        try:
            locator.fill(value, timeout=4000)
            return
        except Exception as e:
            logging.debug(f"fill() blocked ({e}); using JS value-set fallback.")
        try:
            locator.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""",
                value,
            )
        except Exception as e:
            raise RetryableError(f"Could not fill a login field: {e}") from e

    def login(self, page, email_id: str, password: str) -> None:
        """
        Fills the login form, signs in, and — once on the dashboard — clicks
        Start New Booking and runs the slot check.
        """
        # Wait for the login form OR a block page — POLL (don't block 120s) so a
        # 403201 / 429xxx page is caught within ~1.5s and we fail fast.
        waited, form_ready = 0, False
        while waited < 120000:
            self._check_blocked(page)  # raises IpBlocked/Geo/etc if a block appeared
            try:
                el = page.locator(USERNAME_SELECTOR).first
                if el.count() > 0 and el.is_visible():
                    form_ready = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(1500)
            waited += 1500
        if not form_ready:
            self._check_blocked(page)
            raise LoginFormNotReadyError(
                "Login form never appeared within 120s (Cloudflare spinner / 403 / "
                "slow load)."
            )
        logging.debug("Login form loaded")

        # Dismiss the cookie banner (it overlays the form and blocks fields).
        self.pre_login_steps(page)

        # --- Turnstile FIRST -------------------------------------------------
        # Check the Cloudflare 'Verify you are human' challenge BEFORE touching
        # the credentials — no point filling a form we can't submit. The Sign In
        # button is enabled only once Turnstile passes, so that's our signal.
        #
        # Inner retry: a stuck Turnstile is often unstuck by a page reload (it
        # re-runs the challenge with more signals), which is far cheaper than
        # relaunching the whole browser. Try the wait, and on failure reload and
        # try again a couple of times before giving up to the supervisor.
        # We gate on the TURNSTILE TOKEN, not the Sign In button: VFS keeps Sign
        # In disabled until the token exists AND the fields are filled, so waiting
        # on the button here would deadlock (we fill the fields only after). Once
        # the token is populated we fill credentials, and Sign In enables itself.
        passed = False
        for turn_attempt in range(1, TURNSTILE_REFRESH_ATTEMPTS + 2):  # 1 + N reloads
            # Fail FAST if VFS served a block page instead of a solvable Turnstile
            # (403201 IP block / 429xxx / a seen HTTP 403) — no point waiting or
            # refreshing for a challenge that will never appear.
            self._check_blocked(page)
            logging.debug(
                f"Waiting for Cloudflare Turnstile to pass "
                f"(try {turn_attempt}/{TURNSTILE_REFRESH_ATTEMPTS + 1})..."
            )
            # Local headed analysis: set [turnstile] manual_wait_seconds so you
            # can click the checkbox by hand; we just wait that long for the token
            # and don't coord-click or refresh.
            manual_wait = int(get_config_value("turnstile", "manual_wait_seconds", "0"))
            if manual_wait > 0:
                logging.info(
                    f"MANUAL MODE: click the 'Verify you are human' checkbox in the "
                    f"Chrome window — waiting up to {manual_wait}s..."
                )
                if VfsBot._wait_for_turnstile_passed(page, timeout_ms=manual_wait * 1000):
                    passed = True
                break  # don't auto-refresh in manual mode

            # Give it ~10s to auto-solve first.
            if VfsBot._wait_for_turnstile_passed(page, timeout_ms=10000):
                passed = True
                break

            # Didn't auto-solve — try clicking the checkbox by coordinates, then
            # wait a short while again for the token.
            VfsBot._click_turnstile_by_coords(page)
            if VfsBot._wait_for_turnstile_passed(page, timeout_ms=10000):
                passed = True
                break

            # Not passed — reload and re-run the challenge, unless out of tries.
            if turn_attempt <= TURNSTILE_REFRESH_ATTEMPTS:
                logging.debug("Turnstile not passed — refreshing the page to retry.")
                VfsBot._take_final_screenshot(page, f"turnstile_fail_{turn_attempt}")
                try:
                    page.reload(timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    logging.warning(f"Reload failed: {e}")
                try:
                    page.wait_for_selector(USERNAME_SELECTOR, timeout=120000)
                except Exception as e:
                    raise LoginFormNotReadyError(
                        f"Login form did not reappear after reload: {e}"
                    ) from e
                self.pre_login_steps(page)

        if not passed:
            VfsBot._take_final_screenshot(page, "turnstile_failed_final")
            raise SignInDisabledError(
                "Cloudflare 'Verify you are human' did not pass after "
                f"{TURNSTILE_REFRESH_ATTEMPTS} refresh(es) — token never populated."
            )

        # --- Turnstile passed: now fill credentials --------------------------
        email_input = page.locator(USERNAME_SELECTOR).first
        password_input = page.locator(PASSWORD_SELECTOR).first

        logging.debug("Turnstile passed. Filling email field...")
        VfsBot._fill_field(page, email_input, email_id)
        page.wait_for_timeout(500)
        logging.debug("Email entered; filling password field...")

        VfsBot._fill_field(page, password_input, password)
        page.wait_for_timeout(800)
        logging.debug("Password entered; waiting for Sign In to enable...")

        # Now that the token exists AND the fields are filled, Sign In should
        # enable. Wait for it (this is the correct point to wait on the button).
        sign_in = page.get_by_role("button", name="Sign In").first
        if not sign_in.is_enabled():
            if not VfsBot._wait_for_signin_enabled(page, sign_in, timeout_ms=20000):
                raise SignInDisabledError(
                    "Sign In stayed disabled after Turnstile passed and credentials "
                    "were filled."
                )
        logging.debug("Sign In enabled; clicking it.")

        # Recorded just before Sign In: only OTP emails received AFTER this
        # moment count, so a stale code from a previous run can never be used.
        otp_since = time.time()

        # On the slow EC2 box even force-click can exceed its timeout, so try a
        # normal click, then force, then a JS .click() which dispatches instantly
        # and can't be blocked by actionability waits.
        clicked = False
        for how, kwargs in (("normal", {"timeout": 20000}),
                            ("force", {"force": True, "timeout": 20000})):
            try:
                sign_in.click(**kwargs)
                clicked = True
                logging.debug(f"Clicked Sign In ({how}).")
                break
            except Exception as e:
                logging.debug(f"Sign In {how} click failed ({e}); trying next.")
        if not clicked:
            try:
                sign_in.evaluate("el => el.click()")
                clicked = True
                logging.debug("Clicked Sign In (JS dispatch).")
            except Exception as e:
                raise RetryableError(f"Could not click Sign In: {e}") from e
        VfsBot._take_final_screenshot(page, "after_signin")

        # The Sign In API is the most common place VFS returns 403201 (IP block).
        # Check both the network 403 and the page before waiting on the dashboard.
        page.wait_for_timeout(2000)
        self._check_blocked(page)
        if VfsBot._email_not_registered(page):
            raise EmailNotRegisteredError(
                "Login page: 'The entered email id is not registered with us' — "
                "skipping this URL for this account."
            )
        if VfsBot._invalid_credentials(page):
            raise InvalidCredentialsError(
                "Login page: email or password is incorrect — stopping this run "
                "for this account (no retries)."
            )

        # Routes flagged "otp": true in config/routes/<ROUTE>.json require an
        # emailed one-time password after Sign In before the dashboard loads.
        if self.schema.get("otp"):
            self._verify_otp(page, email_id, password, otp_since)

        # After Sign In, Cloudflare often shows the 'Verify Captcha' dialog
        # (app-cloudflare-dialog with a Submit button) that BLOCKS the redirect
        # to the dashboard. It can appear at any moment during this wait, so we
        # poll: dismiss the dialog if present AND check whether we've landed on
        # the dashboard, for up to ~90s, instead of a single blind wait_for_url.
        # We also keep checking for the 'not registered' banner during the wait.
        if not VfsBot._await_dashboard_handling_captcha(page, timeout_ms=90000):
            if VfsBot._email_not_registered(page):
                raise EmailNotRegisteredError(
                    "Login page: 'The entered email id is not registered with us' — "
                    "skipping this URL for this account."
                )
            if VfsBot._invalid_credentials(page):
                raise InvalidCredentialsError(
                    "Login page: email or password is incorrect — stopping this "
                    "run for this account (no retries)."
                )
            VfsBot._raise_if_blocked(page)
            # Not a known state — report what we ACTUALLY landed on, not a guess.
            raise DashboardNotReachedError(
                f"Did not reach dashboard — landed on: {VfsBot._landing_status(page)} "
                f"(URL: {page.url})."
            )

        logging.info(f"Reached dashboard: {page.url}")
        page.wait_for_timeout(2000)
        self._start_new_booking(page)
        self._check_slots(page)

    def _verify_otp(self, page, email_id: str, password: str,
                    since_epoch: float) -> None:
        """
        Completes the OTP step on routes flagged "otp": true.

        Waits for the OTP input to appear (skipping cleanly if the portal went
        straight to the dashboard), fetches the code from the account's mailbox
        (src/utils/otp_service.py — same email/password as the VFS login), types
        it in and submits. Raises OtpVerificationError (retryable — a fresh
        browser triggers a fresh OTP) on any failure.
        """
        from src.utils import otp_service

        # Wait for the OTP field — or the dashboard, if VFS skipped the step
        # (e.g. a recently-verified session). Captcha can pop here too.
        otp_input = None
        waited = 0
        while waited < 60000:
            try:
                if "/dashboard" in (page.url or ""):
                    logging.info("No OTP step — portal went straight to dashboard.")
                    return
            except Exception:
                pass
            # The OTP page can be replaced by an account-block page (429002 denied
            # / 429202 locked / 429001 restricted). Classify it here — so it's
            # handled correctly instead of masquerading as an OTP timeout — and
            # bail immediately rather than waiting the full 60s.
            VfsBot._raise_if_blocked(page)
            try:
                candidate = page.locator(OTP_INPUT_SELECTOR).first
                if candidate.count() > 0 and candidate.is_visible():
                    otp_input = candidate
                    break
            except Exception:
                pass
            VfsBot._dismiss_captcha(page)
            page.wait_for_timeout(2000)
            waited += 2000
        if otp_input is None:
            # One last classification pass before the generic OTP-timeout error.
            VfsBot._raise_if_blocked(page)
            VfsBot._take_final_screenshot(page, "otp_field_missing")
            raise OtpVerificationError(
                "Route is flagged otp=true but no OTP input appeared within 60s "
                f"(landed on: {VfsBot._landing_status(page)})."
            )
        logging.info("OTP step detected — fetching the code from email.")
        VfsBot._take_screenshot(page, "otp_step")

        try:
            code = otp_service.get_otp(email_id, password, since_epoch)
        except Exception as e:
            VfsBot._take_final_screenshot(page, "otp_fetch_failed")
            raise OtpVerificationError(f"Could not obtain the OTP: {e}") from e

        VfsBot._fill_field(page, otp_input, code)
        page.wait_for_timeout(500)
        logging.info("OTP entered; submitting...")

        # The confirm button's label isn't pinned down — try the likely ones,
        # falling back to force/JS clicks (same ladder as Sign In).
        for label in ("Verify", "Submit", "Confirm", "Sign In", "Continue"):
            try:
                btn = page.get_by_role("button", name=label).first
                if btn.count() == 0 or not btn.is_visible():
                    continue
                if not btn.is_enabled():
                    VfsBot._wait_for_signin_enabled(page, btn, timeout_ms=15000)
                try:
                    btn.click(timeout=10000)
                except Exception:
                    try:
                        btn.click(force=True, timeout=10000)
                    except Exception:
                        btn.evaluate("el => el.click()")
                logging.info(f"Submitted OTP via '{label}'.")
                VfsBot._take_screenshot(page, "otp_submitted")
                return
            except Exception:
                continue

        VfsBot._take_final_screenshot(page, "otp_submit_missing")
        raise OtpVerificationError(
            "OTP was entered but no Verify/Submit button could be clicked."
        )

    @staticmethod
    def _start_new_booking(page) -> None:
        """Clicks the 'Start New Booking' button on the VFS dashboard."""
        try:
            page.wait_for_timeout(2000)
            # VFS renders two copies of this button (responsive: one for mobile,
            # one for desktop) — one is CSS-hidden at any given viewport. Target
            # the <button> (not its inner <span>) and keep only the visible copy,
            # otherwise the click lands on the hidden element and times out.
            booking_button = (
                page.locator("button:has-text('Start New Booking')")
                .filter(visible=True)
                .first
            )
            booking_button.scroll_into_view_if_needed(timeout=10000)
            booking_button.click(timeout=15000)
            logging.debug("Clicked Start New Booking")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "06_start_new_booking")
            logging.debug(f"Start New Booking opened. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Start New Booking failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_start_new_booking")

    # ------------------------------------------------------------------ #
    # Slot check                                                         #
    # ------------------------------------------------------------------ #

    def _check_slots(self, page) -> None:
        """
        Slot-check flow (no booking): on the Appointment Details step, run through
        each configured combination (centre / category / sub-category), read the
        earliest-slot banner for each, then send all results in one Telegram
        message. Combinations come from the route schema's `slot_check.combinations`.
        """
        all_combos = self.schema.get("slot_check", {}).get("combinations", [])
        # JSON has no comments, so combinations are switched off with
        # `"disabled": true` instead of being deleted from the route file.
        combos = [c for c in all_combos if not c.get("disabled")]
        if len(combos) < len(all_combos):
            logging.info(
                f"Skipping {len(all_combos) - len(combos)} disabled combination(s)."
            )
        if not combos:
            logging.warning("Slot-check mode but no combinations configured.")
            return

        try:
            page.wait_for_url("**/application-detail", timeout=30000)
        except Exception as e:
            raise SlotCheckError(
                f"Did not reach the Appointment Details page; cannot check slots: {e}"
            ) from e

        VfsBot._wait_for_loader(page)
        page.wait_for_timeout(1000)

        results = []
        prev = {}  # the centre/category/sub-category selected for the previous combo
        for combo in combos:
            label = combo.get("label") or " / ".join(
                filter(None, [combo.get("centre"), combo.get("category"), combo.get("sub_category")])
            )
            logging.info(f"Checking slot for: {label}")

            # The dropdowns cascade (centre -> category -> sub-category), so they
            # are selected in order. Skip any level whose value is unchanged from
            # the previous combination — but once a parent level changes it resets
            # its children, so every level below a change must be re-selected too.
            selections = [
                ("centerCode", "centre", combo.get("centre")),
                ("selectedSubvisaCategory", "category", combo.get("category")),
                ("visaCategoryCode", "sub_category", combo.get("sub_category")),
            ]
            ok = True
            fail_detail = None
            cascade_changed = False
            for control, key, value in selections:
                if not value:
                    continue
                # Re-select if this level's value differs OR a parent changed
                # (which reset this dependent dropdown).
                if not cascade_changed and prev.get(key) == value:
                    logging.debug(f"  (unchanged) {key} = '{value}' — skipping re-select")
                    continue
                cascade_changed = True
                if not VfsBot._select_mat_dropdown(page, control, value):
                    ok = False
                    friendly = {"centre": "centre", "category": "category",
                                "sub_category": "sub-category"}.get(key, key)
                    fail_detail = f"could not select {friendly} '{value}'"
                    break

            prev = {
                "centre": combo.get("centre"),
                "category": combo.get("category"),
                "sub_category": combo.get("sub_category"),
            }

            if not ok:
                # 'ERROR:' prefix marks a real slot-search failure (as opposed to
                # genuine 'no availability'), so the supervisor can flag the route
                # as failed and name the combo + reason in the run summary.
                message = f"ERROR: {fail_detail or 'could not select this combination'}"
            else:
                message = VfsBot._read_slot_message(page) or "No slot message shown (no availability?)."

            logging.info(f"  -> {message}")
            results.append((label, message))
            VfsBot._take_screenshot(page, f"slot_{len(results)}")
            page.wait_for_timeout(1000)

        # Disabled combos are carried in the results with a 'DISABLED' marker so
        # the run summary can show them; no date in the text keeps them out of
        # the slot report and the slot count.
        for combo in all_combos:
            if combo.get("disabled"):
                label = combo.get("label") or " / ".join(
                    filter(None, [combo.get("centre"), combo.get("category"),
                                  combo.get("sub_category")])
                )
                results.append((label, "DISABLED"))

        # Expose the per-combo results for the supervisor's run summary, then send
        # the (unchanged) per-route slot report to the success chat.
        self.slot_results = results
        self._send_slot_report(results)

    @staticmethod
    def _read_slot_message(page, timeout: int = 12000) -> str:
        """
        Returns the 'Earliest available slot ...' banner text on the Appointment
        Details step, or "" if none is shown (e.g. no availability for the chosen
        combination).
        """
        try:
            VfsBot._wait_for_loader(page)
            slot = page.get_by_text("Earliest available slot", exact=False).first
            slot.wait_for(timeout=timeout)
            return slot.inner_text().strip()
        except Exception:
            return ""

    def _send_slot_report(self, results: list) -> None:
        """Formats this route's slot results and sends them to Telegram.

        The message layout lives in src/utils/telegram_message.py — edit it there.
        It is route-aware, so each portal's message shows the correct destination.
        """
        from src.utils import telegram, telegram_message

        url_key = f"{self.source_country_code}-{self.destination_country_code}"
        login_url = get_config_value("vfs-url", url_key, "")
        report = telegram_message.slot_report(
            self.source_country_code,
            self.destination_country_code,
            results,
            login_url,
        )

        # Only notify when there's an actual slot. slot_report() returns "" when
        # no combination has availability, so we skip sending entirely.
        if not report:
            logging.info("No available slots for this route — no Telegram message sent.")
            return

        logging.info("Slot report:\n" + report)
        if telegram.is_configured():
            telegram.send_message(report)
        else:
            logging.warning(
                "Telegram not configured — slot report logged only. "
                "Set [telegram] bot_token and chat_id in config.ini to receive it."
            )

    # ------------------------------------------------------------------ #
    # Shared UI helpers (loader / captcha / wait dialogs / dropdowns)    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wait_for_loader(page, timeout: int = 30000) -> None:
        """
        Waits for VFS's ngx-ui-loader overlay to clear. This full-screen spinner
        intercepts pointer events, so clicking while it is up times out. Returns
        immediately if no loader is present.

        Also clears any Cloudflare 'Verify Captcha' dialog and the VFS 'please
        wait before continuing' reminder first — either can pop up at any step
        and blocks the form until dismissed.
        """
        VfsBot._dismiss_captcha(page)
        VfsBot._dismiss_wait_dialog(page)
        try:
            page.locator("ngx-ui-loader .ngx-overlay.loading-foreground").wait_for(
                state="hidden", timeout=timeout
            )
        except Exception:
            pass  # loader absent or already cleared

    @staticmethod
    def _dismiss_wait_dialog(page) -> None:
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

    @staticmethod
    def _dismiss_captcha(page) -> None:
        """
        Dismisses the Cloudflare 'Verify Captcha' dialog (`app-cloudflare-dialog`)
        if it is showing. The Turnstile widget auto-solves, so we just need to
        click its 'Submit' button. Returns immediately (and silently) when no
        dialog is present.
        """
        try:
            dialog = page.locator("app-cloudflare-dialog")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
        except Exception:
            return

        VfsBot._do_dismiss_captcha(page)

    @staticmethod
    def _wait_with_captcha_check(page, total_ms: int, step_ms: int = 3000) -> None:
        """
        Sleeps for `total_ms`, checking for (and dismissing) the Cloudflare captcha
        dialog every `step_ms`. Use for long idle waits where the dialog could pop
        up while the bot is otherwise doing nothing.
        """
        elapsed = 0
        while elapsed < total_ms:
            page.wait_for_timeout(min(step_ms, total_ms - elapsed))
            elapsed += step_ms
            VfsBot._dismiss_captcha(page)

    @staticmethod
    def _do_dismiss_captcha(page) -> None:
        """
        Clears a confirmed-visible Cloudflare captcha dialog.

        The Turnstile widget shows 'Verifying...' for a few seconds and only
        populates its hidden `cf-turnstile-response` token once solved — clicking
        Submit before then does nothing. So we wait for that token to appear,
        then click Submit, and retry the whole cycle a few times if the dialog
        is still up (it can require more than one round).
        """
        dialog = page.locator("app-cloudflare-dialog")
        logging.info("Cloudflare 'Verify Captcha' dialog detected — handling it.")

        for attempt in range(1, 4):  # up to 3 Submit cycles
            VfsBot._wait_for_turnstile_token(page)
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
                VfsBot._take_screenshot(page, "ERROR_captcha")
                return

            # Did the dialog go away?
            try:
                dialog.first.wait_for(state="hidden", timeout=12000)
                logging.debug("Captcha dialog cleared.")
                VfsBot._take_screenshot(page, "captcha_handled")
                return
            except Exception:
                # Still visible (e.g. token wasn't ready yet) — loop and retry.
                if not VfsBot._captcha_visible(page):
                    return  # raced away on its own
                logging.debug(
                    f"Captcha still visible after Submit (attempt {attempt}); retrying..."
                )

        logging.warning(
            "Captcha dialog still visible after retries — it may need a manual solve."
        )
        VfsBot._take_screenshot(page, "ERROR_captcha_persist")

    @staticmethod
    def _await_dashboard_handling_captcha(page, timeout_ms: int = 90000) -> bool:
        """
        Waits for the dashboard URL while continuously dismissing the Cloudflare
        'Verify Captcha' dialog, which can pop up at any time during the post-
        Sign-In redirect and blocks it until its Submit is clicked.

        Returns True as soon as the URL reaches /dashboard, else False on timeout.
        """
        step_ms = 2000
        waited = 0
        while waited < timeout_ms:
            # Already there?
            try:
                if "/dashboard" in (page.url or ""):
                    return True
            except Exception:
                pass
            # Stop early if VFS served its 'Permission Issues (403203)' geo-block
            # page — retrying won't help (same IP), so raise to abort immediately.
            if VfsBot._is_geo_blocked(page):
                VfsBot._take_final_screenshot(page, "geo_blocked")
                raise GeoBlockedError(
                    "VFS 'Permission Issues (403203)' — IP outside permitted "
                    "location or rate-limited. Not retrying."
                )
            # Stop early on any account/route block page (429002 denied / 429202
            # locked / 429001 restricted) — a block, not a slow dashboard.
            VfsBot._raise_if_blocked(page)
            # Stop early if this email isn't registered for this site — no point
            # waiting; the caller skips this URL for this account.
            if VfsBot._email_not_registered(page):
                return False
            # Stop early on a wrong-credentials banner too (no point waiting 90s).
            if VfsBot._invalid_credentials(page):
                return False
            # Clear the captcha dialog if it's blocking the redirect.
            VfsBot._dismiss_captcha(page)
            page.wait_for_timeout(step_ms)
            waited += step_ms
            if waited % 20000 == 0:
                logging.debug(f"Waiting for dashboard (handling captcha)... ({waited/1000:.0f}s)")
        # Final check.
        try:
            return "/dashboard" in (page.url or "")
        except Exception:
            return False

    @staticmethod
    def _is_geo_blocked(page) -> bool:
        """
        True if the current page is VFS's 'Permission Issues (403203)' block
        (shown when the IP is outside the permitted location / rate-limited).

        Detected by page content so it works regardless of URL — looks for the
        403203 code or the 'Permission Issues' heading.
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        return "403203" in body or "permission issues" in body

    @staticmethod
    def _page_text(page) -> str:
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

    @staticmethod
    def _ip_blocked(page) -> bool:
        """
        True if VFS returned code 403201 — an IP-based block (this IP made too
        many requests / is flagged). VFS renders it as JSON like
        {"code":"403201"}, so we scan the full page text (HTML + frames).
        """
        return "403201" in VfsBot._page_text(page)

    @staticmethod
    def _email_not_registered(page) -> bool:
        """
        True if the login page is showing the 'email id is not registered'
        banner — meaning the current account isn't registered for this portal.

        Detected by the banner text so it works regardless of exact markup.
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        return "not registered with us" in body

    @staticmethod
    def _invalid_credentials(page) -> bool:
        """
        True if the login page is showing an incorrect email/password error —
        meaning the account's credentials are wrong.

        Detected by the banner/toast text so it works regardless of exact markup
        (VFS shows variants like 'The email or password you have entered is
        incorrect.'). The separate 'not registered' banner is excluded here — it
        is a different, already-handled case (EmailNotRegisteredError).
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        if "not registered with us" in body:
            return False
        return (
            "email or password" in body
            or "password you have entered is incorrect" in body
            or "incorrect email or password" in body
            or "invalid username or password" in body
        )

    @staticmethod
    def _access_restricted(page) -> bool:
        """
        True if VFS is showing its 'Access Restricted' block page — the portal
        has cut off this route for the current account/session. Detected by page
        text so it works regardless of URL or exact markup.
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        return "access restricted" in body

    @staticmethod
    def _access_denied(page) -> bool:
        """
        True if VFS is showing its 'Access Denied Due to Unauthorised Activity
        (429002)' block page — the account is blocked (usually after repeated
        wrong-credential attempts) and needs a manual fix. Detected by text.
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        return "429002" in body or "access denied due to unauthorised activity" in body

    @staticmethod
    def _raise_if_blocked(page, cause: Exception = None) -> None:
        """
        If the page is a known VFS account/route block page, raise the SPECIFIC
        error so it's classified (and the account handled) correctly:

          429002 'Access Denied ... Unauthorised Activity' -> AccountBlockedError
                                                               (manual fix needed)
          429202 'Account Locked'                          -> AccountLockedError
          429001 / 'Access Restricted'                     -> AccessRestrictedError

        No-op if the page isn't a block page. `cause` (if given) is chained.
        """
        if VfsBot._ip_blocked(page):
            VfsBot._take_final_screenshot(page, "ip_blocked_403201")
            raise IpBlockedError("VFS 403201 — IP blocked (too many requests / "
                                 "flagged IP). Rotate to a different IP.") from cause
        if VfsBot._access_denied(page):
            VfsBot._take_final_screenshot(page, "access_denied")
            raise AccountBlockedError(VfsBot._landing_status(page)) from cause
        if VfsBot._account_locked(page):
            VfsBot._take_final_screenshot(page, "account_locked")
            raise AccountLockedError(VfsBot._landing_status(page)) from cause
        if VfsBot._access_restricted(page):
            VfsBot._take_final_screenshot(page, "access_restricted")
            raise AccessRestrictedError(VfsBot._landing_status(page)) from cause

    @staticmethod
    def _account_locked(page) -> bool:
        """
        True if VFS is showing its 'Account Locked (429202)' page — too many
        requests in a short window, temporary cooldown. Detected by text so it
        works regardless of exact markup.
        """
        try:
            body = (page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or "").lower()
        except Exception:
            return False
        return "429202" in body or "account locked" in body

    @staticmethod
    def _landing_status(page) -> str:
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

    @staticmethod
    def _captcha_visible(page) -> bool:
        """True if the Cloudflare captcha dialog is currently showing."""
        try:
            dialog = page.locator("app-cloudflare-dialog")
            return dialog.count() > 0 and dialog.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _wait_for_turnstile_token(page, timeout_ms: int = 15000) -> None:
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

    @staticmethod
    def _select_mat_dropdown(page, control_name: str, value: str,
                             attempts: int = 3, option_timeout_ms: int = 20000) -> bool:
        """
        Selects an option in an Angular Material dropdown (`mat-select`).

        Opens the dropdown identified by its `formcontrolname`, WAITS for the
        options to actually load (they arrive async, often behind a spinner), then
        clicks the option whose visible text contains `value` (case-insensitive
        substring).

        Robust to slow option loading: instead of a fixed 1s wait it polls up to
        `option_timeout_ms` for THIS option to become visible, and retries the
        whole open→select a few times. On any failure it presses Escape to close a
        half-open overlay first — otherwise a stuck overlay would intercept clicks
        and break the NEXT dropdown too.

        Returns:
            bool: True if the option was selected, False otherwise.
        """
        for attempt in range(1, attempts + 1):
            try:
                VfsBot._wait_for_loader(page)  # lists load behind a full-screen spinner
                trigger = page.locator(
                    f"mat-select[formcontrolname='{control_name}']"
                ).first
                trigger.scroll_into_view_if_needed(timeout=10000)
                trigger.click(timeout=10000)

                # The overlay opens, then its options load in async. Clear any
                # spinner, then wait for THIS option to actually render before
                # clicking it (the old fixed 1s wait was too short on slow loads).
                page.wait_for_timeout(400)
                VfsBot._wait_for_loader(page)
                option = page.get_by_role("option", name=value, exact=False).first
                option.wait_for(state="visible", timeout=option_timeout_ms)
                option.scroll_into_view_if_needed(timeout=5000)
                option.click(timeout=10000)

                logging.debug(
                    f"Selected '{value}' (dropdown: '{control_name}')"
                    + (f" on attempt {attempt}" if attempt > 1 else "")
                )
                page.wait_for_timeout(1000)
                VfsBot._wait_for_loader(page)  # let the dependent dropdown reload
                return True
            except Exception as e:
                logging.warning(
                    f"Attempt {attempt}/{attempts}: could not select '{value}' for "
                    f"'{control_name}': {e}"
                )
                # Close any half-open overlay so it can't block the next dropdown.
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(700)
                except Exception:
                    pass
                if attempt < attempts:
                    VfsBot._wait_for_loader(page)
                    page.wait_for_timeout(1500)

        VfsBot._take_screenshot(page, "ERROR_dropdown")
        return False

    # ------------------------------------------------------------------ #
    # Browser activity logging                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _attach_activity_logging(page) -> None:
        """
        Attaches verbose Playwright event listeners to `page` so that browser
        activity — frame navigations, network requests/responses, console
        messages and page/request errors — is logged at DEBUG level.

        No-op unless browser-activity logging is enabled (BROWSER_ACTIVITY_LOG
        env var or [logging] browser_activity in config). The network listeners
        are intentionally chatty, so they log at DEBUG: set the log level to
        DEBUG to actually see them.
        """
        if not _browser_activity_enabled():
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

    # ------------------------------------------------------------------ #
    # Screenshots                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _take_screenshot(page, name: str):
        """Per-step screenshot — a no-op unless SCREENSHOTS_ENABLED is True."""
        if not SCREENSHOTS_ENABLED:
            return
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _take_final_screenshot(page, name: str = "final"):
        """Always writes one screenshot (used at the end of the run)."""
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _write_screenshot(page, name: str, fixed_name: bool = False):
        # fixed_name=True writes exactly <name>.png (overwritten each run) so you
        # can always find e.g. Loginformloaded.png; otherwise it's timestamped.
        if fixed_name:
            path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
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
