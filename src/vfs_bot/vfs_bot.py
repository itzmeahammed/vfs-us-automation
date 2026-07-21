import logging
import os
import time
from abc import ABC

from playwright.sync_api import sync_playwright

from src.settings import settings
from src.utils.config_reader import get_config_value
from src.utils.route_schema import get_route_schema
from src.vfs_bot import (
    block_detection,
    browser_setup,
    diagnostics,
    otp_flow,
    session,
    slot_check,
    turnstile,
)
from src.vfs_bot.dom_utils import fill_field
# Exceptions live in errors.py; re-exported so existing imports such as
# `from src.vfs_bot.vfs_bot import LoginError` keep working unchanged.
from src.vfs_bot.errors import (  # noqa: F401
    AccessRestrictedError,
    AccountBlockedError,
    AccountLockedError,
    CdpConnectError,
    DashboardNotReachedError,
    EmailNotRegisteredError,
    GeoBlockedError,
    InvalidCredentialsError,
    IpBlockedError,
    LoginError,
    LoginFormNotReadyError,
    OtpVerificationError,
    RetryableError,
    SignInDisabledError,
    SlotCheckError,
)

# Default field selectors. A route can override any of these WITHOUT a code
# change by adding a "selectors" object to its config/routes/<ROUTE>.json, e.g.
#   "selectors": { "username": "...", "password": "...", "otp": "..." }
# When VFS changes their markup you edit JSON, not Python. self.selectors (built
# in run()) merges the route's overrides over these defaults.
DEFAULT_USERNAME_SELECTOR = (
    "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
)
DEFAULT_PASSWORD_SELECTOR = (
    "input[formcontrolname='password'], #mat-input-1, input[type='password']"
)
# The OTP entry field shown after Sign In on routes with "otp": true. Several
# strategies because the exact markup hasn't been pinned down yet ([i] = case-
# insensitive attribute match).
DEFAULT_OTP_INPUT_SELECTOR = (
    "input[formcontrolname*='otp' i], input[autocomplete='one-time-code'], "
    "input[name*='otp' i], input[id*='otp' i], input[placeholder*='otp' i]"
)


class VfsBot(ABC):
    """
    Slot-check bot for the VFS Malta portal.

    This orchestrates the flow (login -> Cloudflare -> OTP -> dashboard ->
    slot check) end to end; each stage's actual page automation lives in its
    own module next to this one:

        session.py         cookie consent + cross-run cookie hygiene
        turnstile.py        Cloudflare Turnstile + 'Verify Captcha' + loader
        otp_flow.py          the emailed one-time-password step
        slot_check.py        appointment dropdowns, slot read, Telegram report
        block_detection.py   classifying VFS/Cloudflare block pages
        browser_setup.py     CDP attach vs. launching a fresh browser
        diagnostics.py       screenshots + verbose activity logging

    This class itself only holds per-run instance state (selected credential,
    schema, byte/request counters, results) and the handful of methods that
    genuinely need that state; everything else is a free function in the
    modules above. It only reaches Step 1 (Appointment Details), reads the
    'Earliest available slot' banner for each configured combination, and
    reports the results via Telegram — the booking/payment machinery is
    intentionally absent. This project never books anything.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None
        self.schema = {}
        # Field selectors; run() rebuilds this from the route schema. Defaults here
        # keep the bot usable even if a caller skips run()'s setup.
        self.selectors = {
            "username": DEFAULT_USERNAME_SELECTOR,
            "password": DEFAULT_PASSWORD_SELECTOR,
            "otp": DEFAULT_OTP_INPUT_SELECTOR,
        }
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
        # How many resource requests the bandwidth filter aborted this run.
        self._blocked_requests = 0
        # Wire bytes the browser received this run (counted with OR without a
        # proxy, so local-IP and proxy runs can be compared on the same metric).
        self._net_bytes = 0

    def set_credential(self, email: str, password: str) -> None:
        """Supervisor-provided credential to use for this run (overrides self-select)."""
        self._cred_override = (email, password)

    # ------------------------------------------------------------------ #
    # Per-run network instrumentation (genuinely instance-stateful, so    #
    # these stay methods rather than moving to browser_setup.py)         #
    # ------------------------------------------------------------------ #

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
        block_detection.raise_if_blocked(page)  # DOM: 403201 / 403203 / 429xxx (specific)
        if self._block_code:
            raise IpBlockedError(
                "VFS HTTP 403 — IP blocked (too many requests / flagged IP). Rotate IP.")

    def _install_resource_blocking(self, context) -> None:
        """Abort billed-but-useless resource types (image/media/font by default)
        before they leave the browser, so their bytes never hit the metered proxy.

        Installed on the CONTEXT so it also covers the Turnstile iframe. Kept
        deliberately narrow: JS and CSS are never blocked (CSS positions the
        Turnstile checkbox for the coordinate click). Fully fail-safe — any error
        in the filter lets the request through rather than breaking the flow.
        """
        blocked = settings().bandwidth.blocked_types
        if not blocked:
            return

        def _filter(route):
            try:
                if route.request.resource_type in blocked:
                    self._blocked_requests += 1
                    route.abort()
                    return
            except Exception:
                pass
            try:
                route.continue_()
            except Exception:
                pass

        try:
            context.route("**/*", _filter)
            logging.debug(f"Bandwidth: blocking resource types {sorted(blocked)}.")
        except Exception as e:
            logging.warning(f"Could not install resource blocking (continuing): {e}")

    def _install_traffic_meter(self, context) -> None:
        """Sum the wire bytes of every finished request so a run's data usage can
        be measured in BOTH proxy and local-IP modes (the proxy forwarder only
        meters proxied runs). Covers all frames incl. the Turnstile iframe.
        Best-effort — never breaks the flow.
        """
        if not settings().bandwidth.log_usage:
            return

        def _count(request):
            try:
                s = request.sizes()
                self._net_bytes += (s.get("responseBodySize") or 0)
                self._net_bytes += (s.get("responseHeadersSize") or 0)
            except Exception:
                pass

        try:
            context.on("requestfinished", _count)
        except Exception as e:
            logging.debug(f"Could not install traffic meter (continuing): {e}")

    def _instrument_page(self, page, context) -> None:
        """Wires up everything that observes (but never drives) the flow:
        activity logging, the 403 block watcher, and the two bandwidth-saving
        installs. Called once per run, right after the page/context exist."""
        diagnostics.attach_activity_logging(page)
        # Watch for HTTP 403 (IP/access blocks) at the network level, so we
        # catch 403201 even when it's an XHR/JSON that never renders (or the
        # page dies right after).
        self._block_code = None
        self._attach_block_watcher(page)

        # Bandwidth: abort unneeded resource types (image/media/font) so their
        # bytes never egress through the metered proxy. Installed on the CONTEXT
        # so it also covers the Cloudflare Turnstile iframe. JS + CSS are kept.
        self._install_resource_blocking(context)

        # Bandwidth: count wire bytes the browser receives — works WITH or
        # WITHOUT a proxy, so local-IP and proxy runs compare on one metric.
        self._install_traffic_meter(context)

    # ------------------------------------------------------------------ #
    # run() setup steps                                                  #
    # ------------------------------------------------------------------ #

    def _load_schema_and_selectors(self) -> None:
        """Loads the route's flow schema and merges any selector overrides
        it declares over the module defaults."""
        self.schema = get_route_schema(
            self.source_country_code, self.destination_country_code
        )
        sel = self.schema.get("selectors", {}) if isinstance(self.schema, dict) else {}
        self.selectors = {
            "username": sel.get("username") or DEFAULT_USERNAME_SELECTOR,
            "password": sel.get("password") or DEFAULT_PASSWORD_SELECTOR,
            "otp": sel.get("otp") or DEFAULT_OTP_INPUT_SELECTOR,
        }

    def _resolve_credential(self, url_key: str) -> tuple:
        """Returns the (email, password) to use for this run: the supervisor-
        injected one if set, else this bot's own self-selection.

        Raises LoginError if no credential is registered for the route at all.
        """
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
        return email_id, password

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

        self._load_schema_and_selectors()

        browser_type = get_config_value("browser", "type", "chromium")
        headless_mode = get_config_value("browser", "headless", "True")
        url_key = self.source_country_code + "-" + self.destination_country_code
        vfs_url = get_config_value("vfs-url", url_key)
        if not vfs_url:
            logging.error(
                f"No VFS URL configured for '{url_key}'. Add it to config/vfs_urls.ini"
            )
            return False

        email_id, password = self._resolve_credential(url_key)
        self.active_email = email_id

        # Tag this run's timestamped screenshots with the destination code, e.g.
        # 20260721_090048_GRC_turnstile_fail_1.png.
        diagnostics.set_route(self.destination_country_code)
        os.makedirs(diagnostics.SCREENSHOT_DIR, exist_ok=True)

        with sync_playwright() as p:
            cdp_url = get_config_value("browser", "cdp_url")
            browser, context, page = browser_setup.launch_or_attach(
                p, browser_type, headless_mode, cdp_url
            )
            self._instrument_page(page, context)

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
            page.goto(vfs_url, timeout=settings().timeouts.page_load_ms,
                      wait_until="domcontentloaded")

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
                diagnostics.take_final_screenshot(page, "final")
                raise
            except Exception as e:
                # Anything unclassified (incl. Playwright TargetClosedError when
                # the page/browser died mid-flow) is also retryable.
                diagnostics.take_final_screenshot(page, "final")
                raise RetryableError(f"Unexpected flow error: {e}") from e
            finally:
                # Report browser data usage on success AND failure (matches the
                # proxy forwarder's per-route line; this one also works on local IP).
                if settings().bandwidth.log_usage and self._net_bytes:
                    logging.info(
                        f"Browser traffic this route: "
                        f"{self._net_bytes / (1024 * 1024):.1f} MB "
                        f"(all requests, proxy or local IP)."
                    )

            # Single final screenshot capturing the successful end state.
            diagnostics.take_final_screenshot(page, "final")
            if self._blocked_requests:
                logging.info(
                    f"Bandwidth: aborted {self._blocked_requests} asset request(s) "
                    f"(image/media/font) — those bytes never hit the proxy."
                )
            logging.info("Slot check complete. Run finished.")
            return True

    # ------------------------------------------------------------------ #
    # Pre-login: cookies & session                                       #
    # ------------------------------------------------------------------ #

    def pre_login_steps(self, page) -> None:
        """
        Accept ALL cookies on the consent banner. We deliberately ACCEPT (never
        reject) — both as the desired behaviour and because the banner overlays
        the bottom of the page and blocks the login fields until cleared.
        """
        session.accept_cookies(page, attempts=8, interval_ms=1000)

    # ------------------------------------------------------------------ #
    # Login                                                              #
    # ------------------------------------------------------------------ #

    def _wait_for_login_form(self, page) -> None:
        """Polls for the login form OR a block page (doesn't block 120s) so a
        403201 / 429xxx page is caught within ~1.5s and we fail fast. Raises
        LoginFormNotReadyError if the form never appears."""
        login_wait_ms = settings().timeouts.login_wait_ms
        waited, form_ready = 0, False
        while waited < login_wait_ms:
            self._check_blocked(page)  # raises IpBlocked/Geo/etc if a block appeared
            try:
                el = page.locator(self.selectors["username"]).first
                if el.count() > 0 and el.is_visible():
                    form_ready = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(750)
            waited += 750
        if not form_ready:
            self._check_blocked(page)
            raise LoginFormNotReadyError(
                "Login form never appeared within 120s (Cloudflare spinner / 403 / "
                "slow load)."
            )
        logging.debug("Login form loaded")

    def _pass_turnstile(self, page) -> None:
        """
        Checks the Cloudflare 'Verify you are human' challenge BEFORE touching
        the credentials — no point filling a form we can't submit. We gate on
        the TURNSTILE TOKEN, not the Sign In button: VFS keeps Sign In disabled
        until the token exists AND the fields are filled, so waiting on the
        button here would deadlock (fields are only filled after this returns).

        Inner retry: a stuck Turnstile is often unstuck by a page reload (it
        re-runs the challenge with more signals), which is far cheaper than
        relaunching the whole browser. Raises SignInDisabledError if it never
        passes after all reload attempts.
        """
        passed = False
        refresh_attempts = settings().retry.turnstile_refresh_attempts
        for turn_attempt in range(1, refresh_attempts + 2):  # 1 + N reloads
            # Fail FAST if VFS served a block page instead of a solvable Turnstile
            # (403201 IP block / 429xxx / a seen HTTP 403) — no point waiting or
            # refreshing for a challenge that will never appear.
            self._check_blocked(page)
            logging.debug(
                f"Waiting for Cloudflare Turnstile to pass "
                f"(try {turn_attempt}/{refresh_attempts + 1})..."
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
                if turnstile.wait_for_turnstile_passed(page, timeout_ms=manual_wait * 1000):
                    passed = True
                break  # don't auto-refresh in manual mode

            # Give it ~10s to auto-solve first.
            if turnstile.wait_for_turnstile_passed(page, timeout_ms=10000):
                passed = True
                break

            # Didn't auto-solve — try clicking the checkbox by coordinates, then
            # wait a short while again for the token.
            turnstile.click_turnstile_by_coords(page)
            if turnstile.wait_for_turnstile_passed(page, timeout_ms=10000):
                passed = True
                break

            # Not passed — reload and re-run the challenge, unless out of tries.
            if turn_attempt <= refresh_attempts:
                logging.debug("Turnstile not passed — refreshing the page to retry.")
                diagnostics.take_final_screenshot(page, f"turnstile_fail_{turn_attempt}")
                try:
                    page.reload(timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    logging.warning(f"Reload failed: {e}")
                try:
                    page.wait_for_selector(self.selectors["username"], timeout=120000)
                except Exception as e:
                    raise LoginFormNotReadyError(
                        f"Login form did not reappear after reload: {e}"
                    ) from e
                self.pre_login_steps(page)

        if not passed:
            diagnostics.take_final_screenshot(page, "turnstile_failed_final")
            raise SignInDisabledError(
                "Cloudflare 'Verify you are human' did not pass after "
                f"{refresh_attempts} refresh(es) — token never populated."
            )

    def _fill_credentials(self, page, email_id: str, password: str) -> None:
        """Fills the email/password fields (Turnstile must already have passed)."""
        email_input = page.locator(self.selectors["username"]).first
        password_input = page.locator(self.selectors["password"]).first

        fill_field(page, email_input, email_id)
        page.wait_for_timeout(500)
        logging.debug("Email entered; filling password field...")

        fill_field(page, password_input, password)
        page.wait_for_timeout(800)
        logging.debug("Password entered; waiting for Sign In to enable...")

    def _click_sign_in(self, page) -> float:
        """Waits for Sign In to enable and clicks it (normal -> force -> JS
        dispatch fallback ladder, since even a force-click can time out on a
        slow EC2 box). Returns the time.time() recorded just before the click,
        so the caller can filter out stale OTP emails from earlier attempts."""
        # Now that the token exists AND the fields are filled, Sign In should
        # enable. Wait for it (this is the correct point to wait on the button).
        sign_in = page.get_by_role("button", name="Sign In").first
        if not sign_in.is_enabled():
            if not turnstile.wait_for_signin_enabled(page, sign_in, timeout_ms=20000):
                raise SignInDisabledError(
                    "Sign In stayed disabled after Turnstile passed and credentials "
                    "were filled."
                )
        logging.debug("Sign In enabled; clicking it.")

        # Recorded just before Sign In: only OTP emails received AFTER this
        # moment count, so a stale code from a previous run can never be used.
        otp_since = time.time()

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
        return otp_since

    @staticmethod
    def _raise_known_login_errors(page) -> None:
        """Raises EmailNotRegisteredError / InvalidCredentialsError if the
        login page is showing either banner; no-op otherwise. Checked both
        right after the Sign In click and again if the dashboard wait times out."""
        if block_detection.is_email_not_registered(page):
            raise EmailNotRegisteredError(
                "Login page: 'The entered email id is not registered with us' — "
                "skipping this URL for this account."
            )
        if block_detection.is_invalid_credentials(page):
            raise InvalidCredentialsError(
                "Login page: email or password is incorrect — stopping this run "
                "for this account (no retries)."
            )

    def login(self, page, email_id: str, password: str) -> None:
        """
        Fills the login form, signs in, and — once on the dashboard — clicks
        Start New Booking and runs the slot check.
        """
        self._wait_for_login_form(page)
        # Dismiss the cookie banner (it overlays the form and blocks fields).
        self.pre_login_steps(page)

        # Turnstile must pass before the form can be submitted at all.
        self._pass_turnstile(page)

        logging.debug("Turnstile passed. Filling email field...")
        self._fill_credentials(page, email_id, password)
        otp_since = self._click_sign_in(page)
        diagnostics.take_final_screenshot(page, "after_signin")

        # The Sign In API is the most common place VFS returns 403201 (IP block).
        # Check both the network 403 and the page before waiting on the dashboard.
        page.wait_for_timeout(2000)
        self._check_blocked(page)
        self._raise_known_login_errors(page)

        # Routes flagged "otp": true in config/routes/<ROUTE>.json require an
        # emailed one-time password after Sign In before the dashboard loads.
        if self.schema.get("otp"):
            # "otp_mode": "text" (e.g. Greece) reads the code straight from the
            # email text — no AI; default "image" uses the OpenAI PNG reader.
            otp_flow.verify_otp(page, self.selectors["otp"], email_id, password,
                                otp_since, otp_mode=self.schema.get("otp_mode", "image"))

        # After Sign In, Cloudflare often shows the 'Verify Captcha' dialog
        # (app-cloudflare-dialog with a Submit button) that BLOCKS the redirect
        # to the dashboard. It can appear at any moment during this wait, so we
        # poll: dismiss the dialog if present AND check whether we've landed on
        # the dashboard, for up to ~90s, instead of a single blind wait_for_url.
        # We also keep checking for the 'not registered' banner during the wait.
        if not turnstile.await_dashboard_handling_captcha(
                page, timeout_ms=settings().timeouts.dashboard_ms):
            self._raise_known_login_errors(page)
            block_detection.raise_if_blocked(page)
            # Not a known state — report what we ACTUALLY landed on, not a guess.
            raise DashboardNotReachedError(
                f"Did not reach dashboard — landed on: {block_detection.landing_status(page)} "
                f"(URL: {page.url})."
            )

        logging.info(f"Reached dashboard: {page.url}")
        # start_new_booking() below does its own settle-wait before clicking —
        # no need to also wait here (that used to be a redundant back-to-back
        # 2s+2s pause doing the same job).
        slot_check.start_new_booking(page)
        self.slot_results = slot_check.run_slot_check(
            page, self.schema, self.source_country_code, self.destination_country_code
        )
