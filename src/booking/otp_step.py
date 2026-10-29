"""The OTP page VFS shows IN THE MIDDLE OF A BOOKING, after Your Details.

Not to be confused with src/vfs_bot/otp_flow.py, which does the OTP at SIGN IN
for routes flagged "otp": true (Greece, Italy, Austria...). Same idea, same
mailbox reader (src/utils/otp_service.py), different place in the flow and
different stakes: this one sits between a filled application form and the slot
picker, so failing it loses a form that has already been submitted.

2026-10-29, AE-FRA job 4cb31459180a1750: 'your_details' and 'details_summary'
both submitted, then VFS presented "One-time Password (OTP) — A One-time
password (OTP) will be sent to the registered email address". The walk had no
step for it, so 'select_slot' waited 45s for a book-appointment URL that was
never coming and the run stopped three pages deep.

*** GENERATE IS RATIONED. ***

The page states it plainly: "You have left 3 attempt of generating new OTP.
After crossing limit You will be logged off." So this clicks Generate ONCE per
step and, if VFS rejects the code, RE-READS THE SAME EMAIL rather than asking
for a new one — the same discipline otp_flow uses at sign-in, for a harder
reason: spending the third attempt here logs the session off and abandons a
completed application form.

The code is also short-lived ("valid for 3 minutes"), which is why the mailbox
read is given its own, shorter deadline rather than otp_service's 120s default:
a code that arrives after the page has expired it is worse than useless,
because submitting it burns a verify attempt.
"""

import logging
import time
from typing import Any, Dict, Optional

from src.waitlist.errors import WaitlistStepError

log = logging.getLogger(__name__)

#: VFS's own wording when a submitted code is wrong. Shared with otp_flow,
#: which matches the same sentence on the login page.
REJECTED_TEXT = "valid one time password"

#: How many times to submit a reading of the SAME email before giving up.
#: Not a regeneration count — see the module docstring.
DEFAULT_SUBMIT_ATTEMPTS = 2

#: How long to wait for the OTP input to render after clicking Generate.
FIELD_TIMEOUT_MS = 30000

#: How long to wait for the mail. Shorter than otp_service's own default on
#: purpose: the page expires the code after 3 minutes, so a read that takes
#: longer than this produces a code that is already dead.
DEFAULT_MAIL_WAIT_SECONDS = 100

#: Tried in order for the code input. VFS gives it no formcontrolname on the
#: France page; the placeholder is literally "OTP".
INPUT_SELECTORS = (
    "input[formcontrolname='otp']",
    "input[placeholder='OTP']",
    "input[placeholder*='One-time' i]",
    "input[name='otp']",
    "input[type='text'][maxlength='6']",
)


def _locate(page, spec, fallbacks=(), timeout_ms: int = 10000):
    """A control from a {role,name} or {selector} spec, or the first fallback
    selector that is actually on the page. None when nothing matches."""
    if isinstance(spec, dict) and spec.get("selector"):
        candidates = [spec["selector"]]
    elif isinstance(spec, dict) and spec.get("role"):
        try:
            found = page.get_by_role(
                spec["role"], name=spec.get("name"),
                exact=bool(spec.get("exact"))).filter(visible=True).first
            found.wait_for(state="visible", timeout=timeout_ms)
            return found
        except Exception:                                   # noqa: BLE001
            return None
    else:
        candidates = []

    # Each miss costs the full timeout, and there are several fallbacks, so a
    # caller that is only ASKING ("is the field up yet?") passes a short one.
    # With the 3s constant this used to burn ~15s before every Generate click,
    # on a page whose code expires in three minutes.
    for selector in list(candidates) + list(fallbacks):
        try:
            found = page.locator(selector).filter(visible=True).first
            found.wait_for(state="visible", timeout=timeout_ms)
            return found
        except Exception:                                   # noqa: BLE001
            continue
    return None


def _rejected(page) -> bool:
    """Is VFS's 'please enter a valid one time password' banner showing?"""
    try:
        return REJECTED_TEXT in (page.inner_text("body", timeout=5000) or "").lower()
    except Exception:                                       # noqa: BLE001
        return False


def _still_asking(page, step: Dict[str, Any]) -> bool:
    """Is the OTP panel still on screen?

    How acceptance is detected: VFS replaces the panel (and enables Continue)
    once the code is verified. Read from the panel rather than from the
    Continue button's `disabled` attribute, for the same reason the no-slots
    check is — a force-enable script removes the very attribute the button
    state would be read from.
    """
    marker = step.get("present_when") or step.get("panel_text") \
        or "One-time Password"
    try:
        if step.get("present_when"):
            return page.locator(marker).first.is_visible(timeout=3000)
        return page.get_by_text(marker, exact=False).first.is_visible(
            timeout=3000)
    except Exception:                                       # noqa: BLE001
        return False


def is_asked(page, step: Dict[str, Any]) -> bool:
    """Is VFS asking for an OTP on this page right now?

    Used to skip the step cleanly on the sessions where VFS does not ask —
    it does not ask on every booking, and a step that is absent must cost a
    second, not a step timeout.
    """
    return _still_asking(page, step)


def verify(page, step: Dict[str, Any], *, account_email: str,
           mailbox_password: str, dry_run: bool = False) -> str:
    """Complete the mid-booking OTP. Returns a one-line detail for the report.

    Raises WaitlistStepError with a plain reason — nothing is booked or charged
    at this point in the flow, so the honest outcome is a stop the operator can
    act on, never a guess that walks on with the page unverified.
    """
    from src.utils import otp_service

    if dry_run:
        log.info("  [dry-run] VFS is asking for an OTP — not requesting one.")
        return "OTP page reached; not requested (dry run)"

    if not account_email:
        raise WaitlistStepError(
            "VFS is asking for an email OTP but this run does not know which "
            "account it signed in as, so it cannot read the mailbox. Nothing "
            "was booked and nothing was charged.")
    if not mailbox_password:
        # The same refusal credentials.email_password_for makes, raised here so
        # the message names the cause instead of surfacing as an IMAP auth
        # failure. email_password NEVER falls back to the VFS password.
        raise WaitlistStepError(
            f"VFS is asking for an email OTP but no MAILBOX password is "
            f"configured for {account_email} — the bot cannot read the code. "
            f"Add its email_password in config/credentials.local.ini. Nothing "
            f"was booked and nothing was charged.")

    # BEFORE the click, always: a code read from an email that predates this
    # request is a code for someone else's session.
    since_epoch = time.time()

    generate = _locate(page, step.get("generate") or {"role": "button",
                                                      "name": "Generate OTP"})
    field_spec = step.get("input") or {}
    #: Short on purpose: this only asks whether a code was ALREADY requested.
    #: The real wait happens after the Generate click, below.
    field = _locate(page, field_spec, INPUT_SELECTORS,
                    timeout_ms=int(step.get("field_probe_ms") or 600))

    if generate is not None and field is None:
        # ONE click. See the module docstring on why this is never retried.
        log.info("  requesting an OTP (VFS allows a limited number)...")
        generate.click()
    elif field is not None:
        log.info("  the OTP field is already showing — not requesting a new "
                 "code, VFS rations them.")
    else:
        raise WaitlistStepError(
            "The OTP panel is on screen but neither a 'Generate OTP' button "
            "nor an OTP input could be found on it. VFS has changed this page; "
            "nothing was booked and nothing was charged.")

    if field is None:
        field = _locate(page, field_spec, INPUT_SELECTORS,
                        timeout_ms=FIELD_TIMEOUT_MS)
    if field is None:
        raise WaitlistStepError(
            "Clicked 'Generate OTP' but no OTP input appeared within "
            f"{FIELD_TIMEOUT_MS / 1000:.0f}s. A code may have been sent and "
            "spent. Nothing was booked and nothing was charged.")

    wait_seconds = int(step.get("mail_wait_seconds")
                       or DEFAULT_MAIL_WAIT_SECONDS)
    attempts = max(1, int(step.get("submit_attempts")
                          or DEFAULT_SUBMIT_ATTEMPTS))

    mail = _wait_for_mail(otp_service, account_email, mailbox_password,
                          since_epoch, wait_seconds)
    otp_len = otp_service.otp_length()

    mode = str(step.get("otp_mode") or "").lower()
    read_attempts = max(1, int(step.get("read_attempts") or 1))
    rejected: list = []
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            code = _read_code(mail, otp_len, mode, attempt, read_attempts,
                              rejected)
        except Exception as e:                              # noqa: BLE001
            last_error = f"could not read the code from the email: {e}"
            log.warning(f"  OTP read attempt {attempt}/{attempts}: {e}")
            continue

        if attempt > 1:
            # The page may have moved on since the last attempt — a human can
            # click Continue while this is running. Re-locate with a SHORT
            # timeout: a stale locator here is what burned 30s on
            # b698e31e5c818f07 before failing a booking that had succeeded.
            field = _locate(page, field_spec, INPUT_SELECTORS, timeout_ms=2000)
            if field is None:
                log.info("  the OTP field is gone — the page has moved past "
                         "verification.")
                return f"OTP accepted (page advanced before attempt {attempt})"

        log.info(f"  entering the {len(code)}-digit code "
                 f"(attempt {attempt}/{attempts})...")
        fill_ms = int(step.get("fill_timeout_ms") or 10000)
        field.fill("", timeout=fill_ms)
        field.fill(code, timeout=fill_ms)

        submit = _locate(page, step.get("verify") or {"role": "button",
                                                      "name": "Verify"})
        if submit is None:
            raise WaitlistStepError(
                "Entered the OTP but found no 'Verify' button to submit it. "
                "VFS has changed this page; nothing was booked or charged.")
        submit.click()
        page.wait_for_timeout(int(step.get("verify_wait_ms") or 4000))

        if _rejected(page):
            rejected.append(code)
            last_error = "VFS rejected the code"
            log.warning(f"  VFS rejected the OTP on attempt {attempt}. "
                        "Re-reading the SAME email rather than requesting a "
                        "new code — regenerating is rationed and logs the "
                        "session off.")
            continue

        # *** ACCEPTANCE IS THE ABSENCE OF A REJECTION, NOT THE PANEL GOING. ***
        #
        # This used to require the "One-time Password (OTP)" panel to disappear.
        # It does not: VFS leaves the whole panel on screen and simply ENABLES
        # the page's Continue button, which is what moves to Book Appointment.
        #
        # 2026-10-29, b698e31e5c818f07: code 366635 was read and accepted on
        # attempt 1, the panel stayed, this read that as "not verified", and
        # attempt 2 then tried to re-fill an OTP box on a page that had already
        # advanced — a 30s Locator.fill timeout that failed a booking VFS had
        # accepted. The operator had to click Continue by hand and still lost
        # the run.
        #
        # So verification ends here and the step's own "submit" (Continue) is
        # left to walk.py, which already waits for a disabled button to become
        # enabled and then checks that the page advanced. If VFS silently
        # refused the code, Continue never enables and THAT is the failure
        # reported — at the right step, with the right reason.
        # Positive evidence, where the page offers any: VFS enables the page's
        # Continue once the code is accepted. Absence of a rejection says only
        # that no banner appeared; Continue going live says the portal agreed.
        live = _continue_is_live(page, step)
        if live is False:
            # NOT a retry: a genuinely wrong code produces the rejection banner
            # handled above, so this is an odd state rather than a known one.
            # Said out loud here because the submit that follows would
            # otherwise fail as "never reached book-appointment", which reads
            # as a slot-picker problem three steps from the real cause.
            log.warning(
                "  no rejection shown, but the page's Continue is still "
                "disabled. If the run stops at the next step, VFS did not "
                "accept this code.")
        else:
            log.info("  OTP accepted" + (" (Continue is live)" if live else "")
                     + " — handing over to the step's Continue.")
        return f"OTP verified on attempt {attempt}"

    raise WaitlistStepError(
        f"Could not get past VFS's OTP page after {attempts} attempt(s): "
        f"{last_error}. The application form was submitted but no slot was "
        f"picked — nothing was booked and nothing was charged. Re-running "
        f"starts a fresh session and a fresh code.")


def _wait_for_mail(otp_service, email: str, password: str,
                   since_epoch: float, wait_seconds: int):
    """The OTP email, or a WaitlistStepError saying which part failed.

    Separated so a mailbox problem reads as a mailbox problem. Reported as
    'the OTP never arrived' it sends the operator to VFS, which is the one
    place the fault is not.
    """
    log.info(f"  waiting up to {wait_seconds}s for the OTP email...")
    try:
        return otp_service.wait_for_otp_mail(
            email, password, since_epoch, timeout_seconds=wait_seconds)
    except Exception as e:                                  # noqa: BLE001
        raise WaitlistStepError(
            f"VFS asked for an email OTP and no code arrived in the mailbox "
            f"for {email} within {wait_seconds}s ({e}). The code expires in 3 "
            f"minutes, so a later one is of no use. Nothing was booked and "
            f"nothing was charged.") from e


def _read_code(mail, otp_len: int, mode: str, attempt: int,
               read_attempts: int, rejected: list) -> str:
    """One reading of the code, by the same two paths otp_flow uses at sign-in.

    "text"  — the code is plain text in the body. src/utils/text_otp.py reads
              it with NO AI, which is deterministic, so a re-read of the same
              mail cannot differ. France's mail is this shape; so is Greece's.
    default — otp_service reads the body first (free) and falls back to the
              PNG via OpenAI. On a re-read after a rejection the temperature is
              raised and the refused code excluded, so the second reading can
              actually differ from the first.
    """
    if mode == "text":
        from src.utils import text_otp
        code = text_otp.extract_code(mail, otp_len)
        if code in rejected:
            # Deterministic read: a second attempt would produce the same
            # digits, so say so rather than spending another verify.
            raise RuntimeError(
                f"the email's text gives {code!r}, which VFS already rejected "
                f"— re-reading it cannot produce anything different")
        return code

    from src.utils import otp_service
    return otp_service.extract_code(
        mail, otp_len, read_attempts,
        min_temperature=0.0 if attempt == 1 else 0.4,
        exclude=rejected or None)


def _continue_is_live(page, step):
    """Is the page's own Continue enabled? None when it cannot be judged.

    Reuses the walk's enable-poll rather than reading `disabled` directly: it
    also treats Angular Material's aria-disabled / mat-mdc-button-disabled as
    off, which is_enabled() alone reports as ready too early.

    Safe to read here, unlike on a step with pre_submit_js: nothing on this
    step force-enables buttons, and walk.py refuses that JS on a commit run
    anyway. Never raises and never blocks for long - it informs a log line, it
    does not decide the outcome.
    """
    spec = step.get("submit")
    if not isinstance(spec, dict) or not spec.get("name"):
        return None

    # NOTE: register._await_enabled does NOT raise when the button stays
    # disabled - it logs a warning and returns - so its completion says
    # nothing. The state has to be read.
    from src.waitlist.register import _looks_disabled

    deadline = time.time() + int(step.get("continue_wait_ms") or 6000) / 1000
    locator = page.get_by_role(spec.get("role", "button"),
                               name=spec["name"]).first
    last = None
    while time.time() < deadline:
        try:
            last = bool(locator.is_enabled()) and not _looks_disabled(locator)
        except Exception:                                   # noqa: BLE001
            return None                 # cannot be judged; say so with None
        if last:
            return True
        page.wait_for_timeout(300)
    return last
