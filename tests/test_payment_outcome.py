"""Recording what the gateway ANSWERED, not just that we asked.

    ═══════════════ THE GAP THIS CLOSES ═══════════════

A real payment ran 2026-09-29. Every CyberSource selector worked; VFS returned:

    .../nor/confirmation?PaymentStatus=False&RequestRefNo=1049157557
                        &TransactionId=7906718876126295804106&token=

The journal held exactly one row — `payment_submitting` — and nothing after it,
because submit_payment raised PaymentSubmitted on every post-click path and
never wrote a result. So `unanswered()` reported that payment as unresolved,
and WOULD HAVE DONE SO FOREVER, for every payment ever made.

That report is the one an operator checks after a crash: "which payments have
no recorded outcome". A report that lists every payment is the same as no
report at all.
"""

import pytest

from src.payment import gateway
from src.payment.gateway import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_UNKNOWN,
    PaymentDeclined,
    PaymentError,
    PaymentSubmitted,
)

# The real URL, from the live run.
REAL_FAILURE_URL = (
    "https://visa.vfsglobal.com/are/en/nor/confirmation"
    "?PaymentStatus=False&RequestRefNo=1049157557"
    "&TransactionId=7906718876126295804106&token="
)


class FakePage:
    def __init__(self, url):
        self.url = url


# --------------------------------------------------------------------------- #
# Reading the outcome                                                          #
# --------------------------------------------------------------------------- #

def test_the_real_failure_url_is_read_as_FAILED():
    result = gateway.read_outcome(FakePage(REAL_FAILURE_URL))

    assert result["outcome"] == OUTCOME_FAILED
    assert result["requestrefno"] == "1049157557"
    assert result["transactionid"] == "7906718876126295804106"


def test_the_references_are_kept_because_a_human_quotes_them():
    """These are what you give VFS or the bank when asking what happened."""
    result = gateway.read_outcome(FakePage(REAL_FAILURE_URL))

    for key in ("requestrefno", "transactionid"):
        assert result.get(key), f"{key} was dropped"


@pytest.mark.parametrize("value,expected", [
    ("True", OUTCOME_SUCCESS),
    ("true", OUTCOME_SUCCESS),
    ("False", OUTCOME_FAILED),
    ("false", OUTCOME_FAILED),
    ("", OUTCOME_UNKNOWN),
    ("weird", OUTCOME_UNKNOWN),
])
def test_status_values(value, expected):
    page = FakePage(f"https://x/confirmation?PaymentStatus={value}")
    assert gateway.read_outcome(page)["outcome"] == expected


def test_a_missing_parameter_is_UNKNOWN_not_success():
    """Unknown must never collapse into success: that would silently resolve a
    journal row for a payment nobody confirmed."""
    assert gateway.read_outcome(
        FakePage("https://x/confirmation"))["outcome"] == OUTCOME_UNKNOWN


def test_read_outcome_never_raises():
    """It runs AFTER an irreversible click. It must not be able to fail."""
    class Hostile:
        @property
        def url(self):
            raise RuntimeError("target closed")

    assert gateway.read_outcome(Hostile())["outcome"] == OUTCOME_UNKNOWN


# --------------------------------------------------------------------------- #
# A decline is not retryable                                                   #
# --------------------------------------------------------------------------- #

def test_declined_is_a_PaymentSubmitted_not_a_PaymentError():
    """*** THE HIERARCHY IS THE SAFETY PROPERTY. ***

    Anything catching PaymentError to retry would, if PaymentDeclined were one,
    retry a payment that may already have taken money. VFS's own failure page
    says funds may have been deducted despite the failure.
    """
    assert issubclass(PaymentDeclined, PaymentSubmitted)
    assert not issubclass(PaymentDeclined, PaymentError)


def test_the_journal_records_the_result_row():
    """The whole point: submitting must close the row it opened."""
    written = []

    class Page:
        url = REAL_FAILURE_URL

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def click(self, **kwargs):
            return None

        def wait_for_load_state(self, *a, **k):
            return None

        def content(self):
            return ""

    with pytest.raises(PaymentDeclined):
        gateway.submit_payment(
            Page(), {"submit": "input[name='commit']"},
            journal=lambda row: written.append(row) or "logs/payments.jsonl")

    events = [r.get("event") for r in written]
    assert "payment_submitting" in events, "write-ahead row missing"
    assert "payment_result" in events, (
        "no result row was written, so unanswered() reports this payment as "
        "unresolved forever")

    result = [r for r in written if r.get("event") == "payment_result"][0]
    assert result["outcome"] == OUTCOME_FAILED
    assert result["requestrefno"] == "1049157557"


def test_the_write_ahead_row_still_comes_FIRST():
    """Ordering is the crash-safety property and must not drift."""
    written = []

    class Page:
        url = REAL_FAILURE_URL

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def click(self, **kwargs):
            return None

        def wait_for_load_state(self, *a, **k):
            return None

        def content(self):
            return ""

    with pytest.raises(PaymentDeclined):
        gateway.submit_payment(
            Page(), {"submit": "x"},
            journal=lambda row: written.append(row) or "p")

    assert written[0]["event"] == "payment_submitting"


def test_an_unwritable_outcome_row_does_not_mask_the_decline():
    """The click already happened; a journal failure must not swallow the
    result the caller has to act on."""
    calls = []

    def journal(row):
        calls.append(row)
        if row.get("event") == "payment_result":
            raise RuntimeError("disk full")
        return "logs/payments.jsonl"

    class Page:
        url = REAL_FAILURE_URL

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def click(self, **kwargs):
            return None

        def wait_for_load_state(self, *a, **k):
            return None

        def content(self):
            return ""

    with pytest.raises(PaymentDeclined):
        gateway.submit_payment(Page(), {"submit": "x"}, journal=journal)


# --------------------------------------------------------------------------- #
# unanswered() must actually clear                                             #
# --------------------------------------------------------------------------- #

def test_a_result_row_clears_the_unanswered_report(tmp_path, monkeypatch):
    from src.payment import journal as journal_module

    path = tmp_path / "payments.jsonl"
    monkeypatch.setattr(journal_module, "JOURNAL_FILE", str(path))
    monkeypatch.setattr(journal_module, "JOURNAL_DIR", str(tmp_path))

    journal_module.append({"event": "payment_submitting",
                           "booking_ref": "NOR123"})
    assert len(journal_module.unanswered()) == 1

    journal_module.append({"event": "payment_result", "booking_ref": "NOR123",
                           "outcome": OUTCOME_FAILED})
    assert journal_module.unanswered() == [], (
        "a recorded outcome must clear the row, or every payment stays "
        "'unanswered' forever and the report becomes useless")


# --------------------------------------------------------------------------- #
# The journal row must name WHICH booking                                      #
# --------------------------------------------------------------------------- #

def test_the_payment_run_carries_the_picked_slot():
    """The 2026-09-29 live row read `booking_ref: ""`.

    It proved a payment happened but not which booking it was for — the exact
    question asked when a run has to be resolved by hand, and ambiguous the
    moment more than one client runs from this machine.
    """
    from src.booking import walk

    result = walk.WalkResult()
    report = walk.StepReport(name="select_slot")
    report.found = {"chosen_date": "2026-10-14", "chosen_time": "09:00"}
    result.steps.append(report)

    assert walk._picked_slot(result) == "2026-10-14 09:00"


def test_no_slot_step_is_an_empty_string_not_a_crash():
    from src.booking import walk

    assert walk._picked_slot(walk.WalkResult()) == ""


def test_the_stand_in_accepts_the_slot():
    from src.booking.walk import _PaymentRun

    run = _PaymentRun(slot="2026-10-14 09:00", registrant_id="mufaddal-nor")
    assert run.slot == "2026-10-14 09:00"
    assert run.registrant_id == "mufaddal-nor"
    assert run.captures == []
