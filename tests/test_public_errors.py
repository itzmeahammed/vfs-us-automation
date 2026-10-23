"""One-line web-app errors (src/utils/public_errors.py), against the REAL
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
