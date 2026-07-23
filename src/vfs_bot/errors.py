"""Typed exceptions for the VFS bot flow.

Split out of vfs_bot.py so the flow logic and its failure taxonomy can be read
independently. vfs_bot.py re-exports every name here, so existing imports such as
`from src.vfs_bot.vfs_bot import LoginError` keep working unchanged.

The taxonomy encodes the supervisor's response to each failure:
  * RetryableError subclasses  -> tear down Chrome and retry with a fresh browser
  * the standalone Exceptions  -> a specific, non-retryable outcome (skip / stop /
    bench / disable / geo-block), handled case-by-case in supervisor.run().
"""


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


class TurnstileRejectedError(SignInDisabledError):
    """The login API returned a 403 that is NOT a 403201 IP block — almost
    always the server rejecting a stale/failed Cloudflare Turnstile token.

    It is the TOKEN that's bad, not the IP, so this must never be treated as an
    IP block. The bot first refreshes and re-solves Turnstile on the SAME IP a
    couple of times (see VfsBot.login); only if that keeps failing does it bubble
    up. It subclasses SignInDisabledError so the supervisor's existing handling
    then rotates to a different IP (logged only — no Telegram alert for it)."""


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
