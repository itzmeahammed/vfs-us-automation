"""One-line web-app errors (src/utils/pe.py), against the REAL
failure texts the system produced on 2026-10-22."""

from __future__ import annotations

import pytest

from src.utils import public_errors as pe

REAL_2026_10_22 = [
    ("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://visa.vfsglobal.com/are/en/nor/login\nCall log:\n  - navigating",
     pe.FALLBACK),
    ("LoginFormNotReadyError: Login form never appeared within 15s (Cloudflare spinner / 403 / slow load).",
     "VFS's login page did not load."),
    ("Account za***@travnook.com is DISABLED (wrong credentials or unauthorised) — refusing to sign in. Fix the account, then: python -m src.utils.account_health clear x",
     "VFS rejected this account's login (email or password)."),
    ("Login page: 'The entered email id is not registered with us' — skipping this URL for this account.",
     "VFS says this email is not registered on its portal."),
    ("stopped at 'select_slot' before payment: no date inside the window",
     "VFS has no appointment available in the requested dates."),
    ("stopped at 'payment' on the payment step: Never reached the payment gateway — expected a URL containing 'secureacceptance.cybersource.com'",
     "VFS did not open its payment page after the appointment was selected."),
    ("stopped at 'appointment_details' before payment: Could not select 'Abu Dhabi' in dropdown 'centerCode'.",
     "VFS did not offer the requested option on its booking form."),
    ("SignInDisabledError: Cloudflare 'Verify you are human' did not pass after 1 refresh(es) — token never populated.",
     "VFS's security check (Cloudflare) did not let the login through."),
    ("AccountLockedError: Account Locked () — It seems like you have made multiple requests within a short period which exceeds the defined threshold",
     "VFS temporarily locked this account for too many requests."),
    ("Account mu***@travnook.com is benched on AE-NOR for another ~658 min by the circuit breaker (a block or repeated failures).",
     "VFS recently blocked this account; it is paused before the next try."),
    ("stopped at 'payment' on the payment step: Could not select billing country: Locator.select_option: Timeout 15000ms exceeded.",
     "VFS's payment page did not accept the payment details."),
    ("payment submitted but the gateway's answer was unknown",
     "The payment was sent to VFS's gateway and VFS has not confirmed the result yet. Check the VFS account before trying again."),
    ("Page.wait_for_timeout: Target page, context or browser has been closed",
     "VFS's page closed before the booking finished."),
    # The job the operator flagged (5be1dfd4299ec338): VFS 403'd and
    # redirected to page-not-found; the run used to blame a taken slot.
    ("stopped at 'select_slot' before payment: VFS ended the session — GeoBlockedError: Permission Issues (403); VFS redirected to its 'page not found' page (step 'select_slot' could not continue: No time slots are offered for this date.)",
     "VFS ended the session and redirected to its 'page not found' page."),
]


@pytest.mark.parametrize("raw,expected", REAL_2026_10_22)
def test_real_failures_become_one_vfs_sentence(raw, expected):
    message = pe.public_message(raw)
    assert message == expected
    assert "\n" not in message and len(message) <= 200


@pytest.mark.parametrize("raw", [
    "something nobody has seen before", "KeyError: 'combo'", "", None])
def test_unknown_errors_get_the_operators_fallback(raw):
    assert pe.public_message(raw) == pe.FALLBACK


@pytest.mark.parametrize("raw", [
    "PAYMENT DECLINED: The gateway reported the payment FAILED (PaymentStatus='False')",
    "The payment was submitted and the 3-D Secure challenge was not completed",
    "A submit is unresolved (status pending/unknown). Verify on the VFS account",
])
def test_money_and_unconfirmed_submits_never_say_try_again(raw):
    """The fallback promises a retry. For a payment that may have been taken,
    or a registration VFS may already hold, that is how things happen twice."""
    message = pe.public_message(raw)
    assert message != pe.FALLBACK
    assert "check the vfs account" in message.lower()


def test_pending_rows_are_unconfirmed_whatever_their_reason_says():
    assert pe.public_reason("", "unknown") == pe.UNCONFIRMED
    assert pe.public_reason("timeout", "pending") == pe.UNCONFIRMED


def test_ok_rows_keep_their_note():
    assert pe.public_reason("registered\nfine", "success") == "registered fine"


def test_history_translates_errors_and_leaves_notes():
    out = pe.public_history([
        {"at": "t1", "event": "created", "detail": "parked"},
        {"at": "t2", "event": "attempt_failed",
         "detail": "LoginFormNotReadyError: Login form never appeared"},
    ])
    assert out[0]["detail"] == "parked"
    assert out[1]["detail"] == "VFS's login page did not load."


def test_log_lines_keep_progress_and_one_sentence_per_vfs_error():
    raw = [
        "18:05:59 | INFO     | run_id=5a8aa9708825ffb1  (logs/app.jsonl)",
        "",
        "====================================================================",
        "18:06:00 | INFO     | === Booking COMMIT (books and PAYS): AE-NOR ===",
        "18:06:03 | ERROR    | Probe failed: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x",
        "Call log:",
        "  - navigating to \"https://x\"",
        "Traceback (most recent call last):",
        '  File "probe.py", line 588, in run_probe',
        "18:06:03 | WARNING  | Manual booking r -> waiting: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED",
        "18:06:04 | WARNING  | some internal housekeeping nobody needs",
        "18:12:14 | INFO     | Clicking the slot did not take: Locator.click: Timeout 4000ms exceeded.",
    ]
    assert pe.public_log_lines(raw) == [
        "18:05:59 | INFO | run_id=5a8aa9708825ffb1 (logs/app.jsonl)",
        "18:06:00 | INFO | === Booking COMMIT (books and PAYS): AE-NOR ===",
        f"18:06:03 | ERROR | {pe.FALLBACK}",
    ]


# --------------------------------------------------------------------------- #
# The job record the web app reads                                             #
# --------------------------------------------------------------------------- #

def _record(status, **kw):
    from datetime import datetime, timezone

    from src.api.modules.jobs.manager import JobRecord, JobStatus

    return JobRecord(job_id="j1", status=JobStatus(status), command=[],
                     started_at=datetime(2026, 10, 22, 14, 31, 30,
                                         tzinfo=timezone.utc), **kw)


def test_a_failed_booking_job_says_why_not_its_exit_code(monkeypatch):
    from src.booking import requests as req_store

    class Req:
        data = {"history": [
            {"at": "2026-10-22T10:00:00Z", "event": "attempt_failed",
             "detail": "old failure from an earlier run"},
            {"at": "2026-10-22T14:33:22Z", "event": "attempt_failed",
             "detail": "stopped at 'select_slot': VFS ended the session — "
                       "GeoBlockedError; VFS redirected to its 'page not "
                       "found' page"}]}

    monkeypatch.setattr(req_store, "get", lambda rid: Req())
    record = _record("failed", detail="Process exited with code 1.",
                     payload={"request_id": "trav-nook-ae-hun-af0da4"})

    assert record.to_public()["detail"] == \
        "VFS ended the session and redirected to its 'page not found' page."
    assert record.to_dict()["detail"] == "Process exited with code 1."


def test_a_succeeded_job_keeps_its_detail():
    record = _record("succeeded", detail="all done")
    assert record.to_public()["detail"] == "all done"


def test_an_unexpected_crash_reaches_the_web_app_as_unidentified():
    record = _record("failed", detail="Process exited with code 1.",
                     error={"code": "unexpected_error", "message": "'selector'"})
    public = record.to_public()
    assert public["detail"] == pe.UNIDENTIFIED
    assert public["error"]["message"] == pe.UNIDENTIFIED


# --------------------------------------------------------------------------- #
# The fallback must not invent a cause                                          #
# --------------------------------------------------------------------------- #
#
# 2026-10-28: the client feed showed "Proxy connection failed trying after 15
# minutes again" for an AE-HUN run that stopped because VFS had no slots. No
# proxy was involved. The sentence names a cause and a remedy, so an unmatched
# message does not read as "we do not know" — it reads as a proxy outage.

NO_SLOTS_STOP = (
    "MANUAL (x@y.com): stopped at 'appointment_details' before payment: "
    "Step 'appointment_details': VFS says there is nothing to book here — "
    "\"We are sorry but no appointment slots are currently available. New "
    "slots open at regular intervals, please try again later\" The portal "
    "disabled Continue for that reason, so the run stops.")

STUCK_FORM = (
    "Step 'details_summary': page never showed 'Add another applicant' or "
    "'Your Details Summary'. The page is showing: Your Details | Applicant 1.")

DISCARDED_SUBMIT = (
    "Step 'your_details': the submit was clicked but the page did not move — "
    "still at https://visa.vfsglobal.com/are/en/hun/your-details after 20s.")


def test_no_slots_is_not_reported_as_a_proxy_failure():
    message = pe.public_message(NO_SLOTS_STOP)

    assert message != pe.FALLBACK
    assert "proxy" not in message.lower()
    assert "no appointment slots" in message


def test_no_slots_says_no_money_moved():
    """An agent reading this must not have to ask whether the card was hit."""
    message = pe.public_message(NO_SLOTS_STOP)

    assert "nothing was charged" in message.lower()


def test_a_stuck_form_is_not_reported_as_a_proxy_failure():
    for raw in (STUCK_FORM, DISCARDED_SUBMIT):
        message = pe.public_message(raw)
        assert message != pe.FALLBACK, raw
        assert "proxy" not in message.lower(), raw


def test_every_no_slots_phrase_the_walk_knows_has_a_public_sentence():
    """The two lists must not drift: a phrase walk.py stops on but
    public_errors does not recognise becomes a false proxy report."""
    from src.vfs_bot.turnstile import NO_SLOTS_PHRASES

    for phrase in NO_SLOTS_PHRASES:
        message = pe.public_message(f"Step 'x': VFS says {phrase}.")
        assert message != pe.FALLBACK, (
            f"{phrase!r} stops a run but has no public sentence, so the feed "
            "reports it as a proxy failure")


def test_a_real_proxy_failure_still_says_proxy():
    """The wording is right where it is true — only the catch-all was wrong."""
    assert "Proxy" in pe.public_message(
        "net::ERR_TUNNEL_CONNECTION_FAILED at https://visa.vfsglobal.com")


def test_3d_secure_is_not_reported_as_an_otp_failure():
    """2026-10-29, run 2efca90bd38f9f18: the bank's check says "an OTP to type",
    the OTP rules match "otp" as a bare substring, and the web app was told
    "VFS's one-time password (OTP) step could not be completed" while the card
    was being charged on a booking that then completed and was confirmed."""
    said = pe.public_reason(
        "3-D SECURE: FINISH THE BANK'S CHECK IN THE OPEN BROWSER. "
        "(an OTP to type, or an approval in your banking app)",
        "attempt_failed")

    assert "one-time password" not in said.lower()
    assert "bank" in said.lower()
    assert "charged" in said.lower(), "the operator must be told money may have moved"


def test_a_payment_challenge_says_a_human_is_needed():
    said = pe.public_reason("payment_challenge", "attempt_failed")

    assert "browser" in said.lower()
    assert "retried" in said.lower(), "a challenged payment is never auto-retried"


def test_the_otp_rules_still_work_under_the_new_one():
    """Putting money first must not shadow the OTP messages themselves."""
    booking = pe.public_reason(
        "VFS is asking for an email OTP but no MAILBOX password is configured",
        "attempt_failed")
    signin = pe.public_reason(
        "Could not get past VFS's OTP page after 2 attempt(s)", "attempt_failed")

    assert "nothing was charged" in booking.lower()
    assert "one-time password" in signin.lower()


# ===== both lanes write a log, both have to be readable ====================
#
# The booking lane writes "12:07:54 | INFO | ...". The waitlist lane writes
# "[2026-10-29 16:27:50,445] INFO [runner.py:393] ...". Only the first was
# parsed, so GET /v1/jobs/{id}/logs answered `log_available: true` with
# `lines: []` for every registration ever run - the web app showed an empty
# live-log panel and no reason for it. Seen 2026-10-29 on AE-CHE
# (138200ffc7d31a2b, 72 lines on disk, 0 served) and on the AE-GRC run that
# actually succeeded.

BOOKING_LINE = "12:07:54 | INFO     | run_id=924e9604 Reached dashboard."
WAITLIST_LINE = ("[2026-10-29 16:27:50,464] INFO [runner.py:393] "
                 "=== Waitlist run: AE-CHE ===")


def test_the_waitlist_lanes_format_is_read():
    """*** THE ONE THAT SERVED AN EMPTY PANEL. ***"""
    out = pe.public_log_lines([WAITLIST_LINE])

    assert out == ["16:27:50 | INFO | === Waitlist run: AE-CHE ==="]


def test_the_booking_lanes_format_still_is():
    out = pe.public_log_lines([BOOKING_LINE])

    assert len(out) == 1
    assert out[0].startswith("12:07:54 | INFO | ")


def test_one_log_may_hold_both():
    """Nothing forbids it, and a parser that handles each alone but not the mix
    is a parser nobody can reason about."""
    out = pe.public_log_lines([BOOKING_LINE, WAITLIST_LINE])

    assert len(out) == 2


def test_the_source_location_is_not_shown():
    """'[runner.py:393]' is internal detail, and the other format carries no
    equivalent - showing it for one lane only would be a tell."""
    out = pe.public_log_lines([WAITLIST_LINE])

    assert "runner.py" not in out[0]
    assert "[" not in out[0].split("| INFO |")[1] or "===" in out[0]


def test_a_bracketed_debug_line_is_still_dropped():
    out = pe.public_log_lines(
        ["[2026-10-29 16:27:50,464] DEBUG [runner.py:1] chatty internals"])

    assert out == []


def test_a_bracketed_error_still_becomes_one_public_sentence():
    """The level branch must apply to both formats, or the waitlist lane would
    leak a Playwright call log straight to the web app."""
    out = pe.public_log_lines([
        "[2026-10-29 16:30:14,038] ERROR [runner.py:9] "
        "Could not fill field first_name (text): Locator.wait_for: Timeout "
        "15000ms exceeded."])

    assert len(out) == 1
    assert "Locator" not in out[0]
    assert "Timeout" not in out[0]


def test_a_bracketed_line_with_no_source_location_is_read():
    """Not every logger prints one."""
    out = pe.public_log_lines(
        ["[2026-10-29 16:27:50,464] INFO Plain line, no module."])

    assert out == ["16:27:50 | INFO | Plain line, no module."]


def test_a_playwright_call_log_is_still_not_a_line():
    """It has no timestamp in either format, so it never reaches the web app."""
    out = pe.public_log_lines([
        "  - waiting for locator(\"app-dynamic-control\")",
        "*** LIVE MODE - this WILL submit a real waitlist registration. ***"])

    assert out == []
