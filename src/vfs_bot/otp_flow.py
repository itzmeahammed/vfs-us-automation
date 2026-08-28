"""OTP verification for routes flagged "otp": true in their route schema.

Runs after Sign In and before the dashboard: waits for the emailed one-time-
password field, reads the code from the account's mailbox (src/utils/
otp_service.py — same email/password as the VFS login), enters it and submits.
VFS occasionally rejects a submitted OTP (a misread digit, or the image
render lagging the actual code); on rejection this re-reads the SAME email
for a fresh reading and resubmits, up to settings().otp.submit_attempts times,
before giving up with OtpVerificationError (retryable — a fresh browser
re-triggers Sign In, which sends a fresh OTP).
"""

import logging

from src.settings import settings
from src.vfs_bot import block_detection, diagnostics, turnstile
from src.vfs_bot.dom_utils import fill_field
from src.vfs_bot.errors import (
    EmailNotRegisteredError,
    InvalidCredentialsError,
    LoginBouncedError,
    OtpReadError,
    OtpVerificationError,
    TurnstileRejectedError,
)

# Text VFS shows when a submitted OTP is wrong.
OTP_REJECTED_TEXT = "valid one time password"


def _raise_login_banner_errors(page) -> None:
    """Raise if the LOGIN page is showing its 'email not registered' / 'invalid
    credentials' banner. On an OTP route these can appear AFTER Sign In (in place
    of the OTP field), so we must classify them here too — otherwise the OTP wait
    times out and mislabels a not-registered account as a retryable OTP failure
    (it should be a neutral skip / a hard stop). No-op on a normal OTP page."""
    if block_detection.is_email_not_registered(page):
        raise EmailNotRegisteredError(
            "Login page: 'The entered email id is not registered with us' shown "
            "after Sign In — skipping this URL for this account."
        )
    if block_detection.is_invalid_credentials(page):
        raise InvalidCredentialsError(
            "Login page: email or password is incorrect (shown after Sign In) — "
            "stopping this run for this account (no retries)."
        )


def _raise_login_bounced(page, waited_ms: int) -> None:
    """Report a silent login bounce as what it is, never as an OTP failure.

    The distinction is not cosmetic: OtpVerificationError tells the supervisor
    'do not retry, and strike the account', which is precisely wrong for a login
    VFS refused. LoginBouncedError instead reloads and re-solves on this IP,
    then rotates — and leaves the account's health alone."""
    logging.warning(
        f"Sign In bounced back to the login form after {waited_ms / 1000:.0f}s "
        "— no OTP was ever sent; this is a login failure, not an OTP failure."
    )
    diagnostics.take_final_screenshot(page, "login_bounced")
    raise LoginBouncedError(
        "Route is flagged otp=true but Sign In returned to the login form with "
        "no error shown — the login was silently refused, so no OTP arrived."
    )


def submit_otp(page) -> bool:
    """Click the OTP confirm button (label ladder + force/JS fallback).
    Returns True if a click was issued, False if no button was found.

    Raises TurnstileRejectedError when the OTP page's OWN Cloudflare Turnstile
    never passes (Sign In stays disabled). The caller then refreshes and
    re-logs-in on the same IP — exactly like the login page — instead of
    force-clicking a dead button and waiting ~2 min for a dashboard that never
    loads (the old behaviour that produced a spurious DashboardNotReachedError)."""
    for label in ("Verify", "Submit", "Confirm", "Sign In", "Continue"):
        try:
            btn = page.get_by_role("button", name=label).first
            if btn.count() == 0 or not btn.is_visible():
                continue
            if not btn.is_enabled():
                # This step has its OWN Cloudflare Turnstile (a separate widget
                # from the login page's), which VFS gates the button on. Gate on
                # the TOKEN — same as the login page — not a blind force-click:
                # an auto-solve window, then coordinate-click, then wait for the
                # token, after which the button enables on its own.
                if not turnstile.wait_for_turnstile_passed(page, timeout_ms=10000):
                    turnstile.click_turnstile_by_coords(page)
                    turnstile.wait_for_turnstile_passed(page, timeout_ms=20000)
                turnstile.wait_for_signin_enabled(page, btn, timeout_ms=10000)
                if not btn.is_enabled():
                    # The token never landed — the OTP-page Turnstile did not
                    # pass. Bail fast so the caller can refresh + re-login,
                    # instead of clicking a disabled button and hanging.
                    diagnostics.take_final_screenshot(page, "otp_turnstile_failed")
                    raise TurnstileRejectedError(
                        "OTP-page Cloudflare Turnstile did not pass — Sign In "
                        "stayed disabled after solving."
                    )
            try:
                btn.click(timeout=10000)
            except Exception:
                try:
                    btn.click(force=True, timeout=10000)
                except Exception:
                    btn.evaluate("el => el.click()")
            logging.info(f"Submitted OTP via '{label}'.")
            diagnostics.take_screenshot(page, "otp_submitted")
            return True
        except TurnstileRejectedError:
            raise  # never swallowed by the label ladder — the caller retries login
        except Exception:
            continue
    return False


def otp_rejected(page) -> bool:
    """True if VFS's 'Please enter a valid one time password (OTP).' banner
    is on the page."""
    try:
        return OTP_REJECTED_TEXT in (block_detection.full_page_text(page) or "")
    except Exception:
        return False


def otp_rejected_after_submit(page, timeout_ms: int = 12000) -> bool:
    """Poll briefly after submitting: True if VFS shows the invalid-OTP banner
    (=> re-fetch and retry); False if we progress toward the dashboard or
    nothing rejects within the window (=> treat as accepted)."""
    step_ms = 500
    waited = 0
    while waited < timeout_ms:
        try:
            if "/dashboard" in (page.url or ""):
                return False  # moved on — accepted
        except Exception:
            pass
        block_detection.raise_if_blocked(page)   # an account block can surface here
        if otp_rejected(page):
            return True
        turnstile.dismiss_captcha(page)
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return False


def verify_otp(page, otp_selector: str, email_id: str, password: str,
                since_epoch: float, otp_mode: str = "image",
                on_poll=None) -> None:
    """
    Completes the OTP step on routes flagged "otp": true.

    Waits for the OTP input to appear (skipping cleanly if the portal went
    straight to the dashboard), fetches the code from the account's mailbox,
    types it in and submits. Raises OtpVerificationError (retryable) on any
    failure. `otp_selector` is the route's (possibly overridden) OTP field
    selector; `since_epoch` is the time.time() recorded just before Sign In,
    so a stale code from a previous run can never be used.

    `otp_mode` selects how the code is read from the email:
      * "image" (default) — OpenAI reads the code from the PNG attachment, with
        per-submit re-reads to recover a misread (Italy, etc.).
      * "text"            — the code is plain text in the email body; read it
        directly with NO AI (Greece). See src/utils/greece_otp.py.

    `on_poll` (optional) is called once per wait iteration and may raise. The
    bot passes its network-block classifier here so a VFS 403 that lands AFTER
    the post-Sign-In check — routine on a slow proxy — is read while we wait,
    instead of sitting unexamined in the captured-response list for 60s. Without
    it, a rejected Cloudflare token was reported as a missing OTP field.
    """
    from src.utils import otp_service

    # Wait for the OTP field — or the dashboard, if VFS skipped the step
    # (e.g. a recently-verified session). Captcha can pop here too.
    otp_input = None
    waited = 0
    bounce_grace_ms = settings().timeouts.login_bounce_grace_ms
    while waited < 60000:
        try:
            if "/dashboard" in (page.url or ""):
                logging.info("No OTP step — portal went straight to dashboard.")
                return
        except Exception:
            pass
        # Read any VFS 403 captured since Sign In. A 403201 becomes an IP block
        # and anything else a rejected Turnstile token — both far better answers
        # than the OTP timeout this used to become.
        if on_poll is not None:
            on_poll()
        # The OTP page can be replaced by an account-block page (429002 denied
        # / 429202 locked / 429001 restricted). Classify it here — so it's
        # handled correctly instead of masquerading as an OTP timeout — and
        # bail immediately rather than waiting the full 60s.
        block_detection.raise_if_blocked(page)
        # ...or by the login page's own 'not registered' / 'wrong password'
        # banner (Sign In bounced back instead of sending an OTP) — bail early
        # with the RIGHT error (skip / stop) rather than an OTP-field timeout.
        _raise_login_banner_errors(page)
        try:
            candidate = page.locator(otp_selector).first
            if candidate.count() > 0 and candidate.is_visible():
                otp_input = candidate
                break
        except Exception:
            pass
        # Sign In silently bounced back to the login form: no banner to match
        # above, and no OTP will ever arrive. Bail now rather than waiting out
        # the remaining ~55s only to blame the OTP step for a login failure.
        if waited >= bounce_grace_ms and turnstile.login_form_showing(page):
            _raise_login_bounced(page, waited)
        turnstile.dismiss_captcha(page)
        page.wait_for_timeout(1000)
        waited += 1000
    if otp_input is None:
        # One last classification pass before the generic OTP-timeout error.
        if on_poll is not None:
            on_poll()
        block_detection.raise_if_blocked(page)
        _raise_login_banner_errors(page)
        if turnstile.login_form_showing(page):
            _raise_login_bounced(page, waited)
        diagnostics.take_final_screenshot(page, "otp_field_missing")
        raise OtpVerificationError(
            "Route is flagged otp=true but no OTP input appeared within 60s "
            f"(landed on: {block_detection.landing_status(page)})."
        )
    logging.info("OTP step detected — fetching the code from email.")
    diagnostics.take_screenshot(page, "otp_step")

    # Fetch the email ONCE; we re-read its image on each submit so a VFS
    # rejection can be recovered without waiting for a whole new OTP email.
    try:
        mail = otp_service.wait_for_otp_mail(email_id, password, since_epoch)
    except Exception as e:
        diagnostics.take_final_screenshot(page, "otp_fetch_failed")
        raise OtpVerificationError(f"Could not obtain the OTP email: {e}") from e

    otp_len = otp_service.otp_length()

    # Greece-style routes deliver the OTP as plain text in the email body. Read
    # it directly (no AI, and no re-read loop — the text is deterministic) and
    # submit once. A rejection is retryable: a fresh browser triggers a new OTP.
    if (otp_mode or "").lower() == "text":
        from src.utils import greece_otp
        try:
            code = greece_otp.extract_code(mail, otp_len)
        except Exception as e:
            diagnostics.take_final_screenshot(page, "otp_read_failed")
            raise OtpReadError(f"Could not read the OTP: {e}") from e
        try:
            otp_input = page.locator(otp_selector).first
        except Exception:
            pass
        fill_field(page, otp_input, code)
        page.wait_for_timeout(500)
        logging.info(f"OTP {code} entered (text mode); submitting...")
        if not submit_otp(page):
            diagnostics.take_final_screenshot(page, "otp_submit_missing")
            raise OtpVerificationError(
                "OTP entered but no Verify/Submit button could be clicked."
            )
        if otp_rejected_after_submit(page):
            diagnostics.take_final_screenshot(page, "otp_rejected_final")
            raise OtpVerificationError(
                "VFS rejected the text OTP ('Please enter a valid one time password')."
            )
        return

    read_attempts = settings().otp.read_attempts
    submit_attempts = max(1, settings().otp.submit_attempts)
    rejected = set()

    for attempt in range(1, submit_attempts + 1):
        # Read a valid code VFS hasn't already rejected. After a rejection the
        # temperature is raised so the re-read can differ from the last one.
        try:
            code = otp_service.extract_code(
                mail, otp_len, read_attempts,
                min_temperature=0.0 if attempt == 1 else 0.4,
                exclude=rejected,
            )
        except Exception as e:
            diagnostics.take_final_screenshot(page, "otp_read_failed")
            raise OtpReadError(f"Could not read the OTP: {e}") from e

        if attempt > 1:
            logging.info(
                f"Re-fetched OTP from AI after VFS rejection "
                f"(submit {attempt}/{submit_attempts}): {code}"
            )

        # Re-locate the field (it may re-render after a rejection) and enter it.
        try:
            otp_input = page.locator(otp_selector).first
        except Exception:
            pass
        fill_field(page, otp_input, code)
        page.wait_for_timeout(500)
        logging.info(f"OTP {code} entered; submitting ({attempt}/{submit_attempts})...")

        if not submit_otp(page):
            diagnostics.take_final_screenshot(page, "otp_submit_missing")
            raise OtpVerificationError(
                "OTP entered but no Verify/Submit button could be clicked."
            )

        # Did VFS reject it? Poll briefly for the 'valid OTP' banner.
        if not otp_rejected_after_submit(page):
            return  # accepted (or progressing to the dashboard) — done here

        rejected.add(code)
        logging.warning(
            f"VFS REJECTED the OTP '{code}' — 'Please enter a valid one time "
            f"password (OTP)'. RE-FETCHING a fresh reading from AI and retrying "
            f"(submit {attempt}/{submit_attempts})."
        )
        diagnostics.take_screenshot(page, f"otp_rejected_{attempt}")

    diagnostics.take_final_screenshot(page, "otp_rejected_final")
    raise OtpVerificationError(
        f"VFS rejected the OTP on all {submit_attempts} submit attempt(s) "
        "('Please enter a valid one time password')."
    )
