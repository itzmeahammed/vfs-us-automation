"""TEST MODE: --stop-before-otp-submit (src/payment/gateway.py).

The full payment flow runs, but at the bank's 3-D Secure screen the SMS code
is read and logged MASKED, never typed, and the bank's submit is never
clicked. CLI only — the API must never be able to turn it on.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.payment import gateway
from tests.test_payment_otp_fill import NOW, SPEC, FakeFrame, FakePage


@pytest.fixture
def test_mode(monkeypatch):
    monkeypatch.setattr(gateway, "TEST_STOP_BEFORE_OTP_SUBMIT", True)


def _bank():
    return FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])


def _inbox(code="483920"):
    from src.otp_relay.inbox import OtpInbox
    from src.otp_relay.relayion import Message

    inbox = OtpInbox(poll_s=1, fetch=lambda s, n: [
        Message(id="m1", sender="BANK", body=f"Your OTP is {code}",
                received_at=NOW + timedelta(seconds=30))])
    inbox.arm(NOW)
    return inbox


def test_the_code_is_read_but_never_typed_or_submitted(test_mode, caplog):
    bank = _bank()
    rows = []
    with pytest.raises(gateway.StoppedForTesting) as caught:
        gateway._await_challenge(FakePage([bank]), SPEC, rows.append, None,
                                 "", timeout_ms=60000, otp=_inbox())

    assert bank.filled == [] and bank.clicked == []
    assert gateway.TEST_STOP_MESSAGE in str(caught.value)
    assert "4****0" in str(caught.value)
    # The code itself never reaches the log or the journal.
    assert "483920" not in caplog.text
    assert all("483920" not in str(r) for r in rows)


def test_the_submit_is_answered_so_it_is_not_left_unknown(test_mode):
    rows = []
    with pytest.raises(gateway.StoppedForTesting):
        gateway._await_challenge(FakePage([_bank()]), SPEC, rows.append, None,
                                 "", timeout_ms=60000, otp=_inbox())
    result = [r for r in rows if r["event"] == "payment_result"]
    assert result and result[-1]["outcome"] == gateway.OUTCOME_TEST_STOPPED


def test_no_relay_stops_at_once(test_mode):
    bank = _bank()
    with pytest.raises(gateway.StoppedForTesting, match="no SMS code"):
        gateway._await_challenge(FakePage([bank]), SPEC, None, None, "",
                                 timeout_ms=60000, otp=None)
    assert bank.filled == [] and bank.clicked == []


def test_off_by_default():
    assert gateway.TEST_STOP_BEFORE_OTP_SUBMIT is False


def test_the_api_cannot_send_the_flag():
    import inspect

    from src.api.modules.booking import schemas

    assert "stop-before-otp-submit" not in inspect.getsource(schemas)


def test_a_test_stop_puts_the_request_back_to_waiting():
    from src.booking import autobook
    from src.booking import requests as store
    from src.booking.walk import StepReport, WalkResult

    walk = WalkResult(steps=[StepReport(name="review", ok=True),
                             StepReport(name="payment", ok=True)],
                      stopped_at="payment", reason="Stopping ...",
                      test_stopped=True)

    class Result:
        pass

    result = Result()
    result.walk = walk
    outcome = autobook.classify(result, "payment",
                                [{"event": "payment_submitting"},
                                 {"event": "payment_result",
                                  "outcome": "test_stopped"}])
    assert outcome.status == store.WAITING
    assert outcome.event == "test_stopped"
    assert not outcome.notify


def test_the_cli_flag_needs_commit(capsys):
    from src.booking.__main__ import main

    assert main(["probe", "-sc", "AE", "-dc", "NOR", "--walk",
                 "--stop-before-otp-submit"]) == 1
    assert "needs --walk --commit" in capsys.readouterr().out
    assert gateway.TEST_STOP_BEFORE_OTP_SUBMIT is False
