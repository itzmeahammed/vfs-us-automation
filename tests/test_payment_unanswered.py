"""`journal.unanswered()` must never under-report a possibly-charged card.

WHY THIS FILE EXISTS
--------------------
This function answers one question: "did we submit a payment and never learn
what the gateway said?" Every row it returns is money that may have moved. The
console turns an empty result into a positive reassurance —

    "Every payment has a recorded outcome — nothing was submitted to the
     payment gateway without its result being written down."

— so a false negative here is not a missing feature, it is the system telling
an operator a card was not charged when it may have been.

It used to correlate submits to results on `booking_ref`. That field is not a
reference: runner.py writes `reference or slot` and the walk leaves the
reference empty, so in practice it holds the appointment slot string. Two runs
that pick the same first-available slot share it — which is exactly what a
"the payment failed, try again" retry does. The earlier run's result then
marked the later run's submit as answered.

These tests pin the pairing rules, not the storage format. They stub
`read_all` rather than writing files: the rules are the fragile part, and the
append path has its own fsync-and-raise guarantees tested elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.payment import journal  # noqa: E402

SUBMIT = "payment_submitting"
RESULT = "payment_result"


@pytest.fixture()
def rows(monkeypatch):
    """Let a test declare the journal's contents in order."""
    store: list = []
    monkeypatch.setattr(journal, "read_all", lambda: store)
    return store


def _submit(run_id, ref="2026-11-01 09:00", at="1"):
    return {"event": SUBMIT, "run_id": run_id, "booking_ref": ref, "at": at}


def _result(run_id, ref="2026-11-01 09:00", at="2"):
    return {"event": RESULT, "run_id": run_id, "booking_ref": ref, "at": at}


def test_a_clean_submit_and_result_is_answered(rows):
    rows.extend([_submit("runA"), _result("runA")])
    assert journal.unanswered() == []


def test_a_submit_with_no_result_is_reported(rows):
    rows.append(_submit("runA"))
    assert [r["run_id"] for r in journal.unanswered()] == ["runA"]


def test_a_retry_on_the_same_slot_is_not_masked_by_the_first_runs_result(rows):
    """The regression this file exists for.

    runA submits and gets an answer. runB retries, picks the SAME
    first-available slot, submits, and dies before the gateway answers. Both
    rows carry the identical `booking_ref`, so the old `booking_ref` matching
    reported nothing at all.
    """
    rows.extend([
        _submit("runA", at="1"),
        _result("runA", at="2"),
        _submit("runB", at="3"),       # died here — no result
    ])
    assert [r["run_id"] for r in journal.unanswered()] == ["runB"]


def test_empty_booking_refs_do_not_collapse_together(rows):
    """The walk hard-codes `reference = ""`, so this is the common shape."""
    rows.extend([
        _submit("runA", ref="", at="1"),
        _result("runA", ref="", at="2"),
        _submit("runB", ref="", at="3"),
    ])
    assert [r["run_id"] for r in journal.unanswered()] == ["runB"]


def test_two_submits_in_one_run_need_two_results(rows):
    """A result answers ONE submit, not every submit sharing its run.

    Set membership on `run_id` would mark both answered as soon as either was,
    reintroducing the same class of false negative one level down.
    """
    rows.extend([
        _submit("runA", at="1"),
        _submit("runA", at="2"),
        _result("runA", at="3"),
    ])
    assert len(journal.unanswered()) == 1

    rows.append(_result("runA", at="4"))
    assert journal.unanswered() == []


def test_a_submit_without_a_run_id_is_still_reported(rows):
    """`append()` always stamps a run_id, so this is a hand-edited or legacy
    row. It must not be dropped from the one report that exists to catch it."""
    rows.append({"event": SUBMIT, "booking_ref": "x", "at": "1"})
    assert len(journal.unanswered()) == 1


def test_a_result_cannot_answer_a_different_run(rows):
    rows.extend([_submit("runA", at="1"), _result("runB", at="2")])
    assert [r["run_id"] for r in journal.unanswered()] == ["runA"]


def test_rows_are_reported_oldest_first(rows):
    rows.extend([_submit("runB", at="2026-10-02T00:00:00Z"),
                 _submit("runA", at="2026-10-01T00:00:00Z")])
    assert [r["run_id"] for r in journal.unanswered()] == ["runA", "runB"]


def test_unrelated_events_are_ignored(rows):
    rows.extend([{"event": "card_loaded", "run_id": "runA", "at": "0"},
                 _submit("runA", at="1"),
                 _result("runA", at="2")])
    assert journal.unanswered() == []
