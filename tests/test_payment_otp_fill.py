"""Typing the 3-D Secure code into the bank's screen, and the guards on it.

    ═══════════════════════ WHAT IS ACTUALLY AT RISK ═══════════════════════

By the time any of this runs the payment is SUBMITTED. The danger is no
longer "will it pay" — it is:

    1. clicking the gateway's Pay button a second time      -> double charge
    2. typing a code into the wrong screen                  -> wasted attempt
    3. pressing Continue on an app-approval challenge       -> failed auth
    4. an exception escaping the challenge loop             -> abandoned
                                                               payment with an
                                                               unknown outcome

Each has a test below. The fakes are deliberately crude — a locator that
reports what it was asked for — because what is being tested is the DECISION
to click, not Playwright.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.payment import otp as payment_otp

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

#: The real shape, from config/payment/CYBERSOURCE.json.
SPEC = {
    "url_contains": "secureacceptance.cybersource.com",
    "submit": "input[type='submit'][name='commit']",
    "challenge_markers": ["payer_auth", "acs", "step-up"],
    "otp_field": ["input[autocomplete='one-time-code']",
                  "input[name*='otp' i]"],
    "otp_submit": ["input[type='submit']", "button[type='submit']"],
    "otp_digits": 6,
}


class FakeElement:
    def __init__(self, name="el"):
        self.name = name


class FakeLocator:
    """Reports a configurable count/visibility and records what happened."""

    def __init__(self, selector, matches, frame):
        self.selector = selector
        self.matches = matches
        self.frame = frame

    def count(self):
        return 1 if self.matches else 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return bool(self.matches)

    def is_enabled(self):
        return True

    def element_handle(self):
        # Identity by selector so the veto can compare two locators.
        return f"{self.frame.url}::{self.selector}"

    def fill(self, value, timeout=None):
        self.frame.filled.append((self.selector, value))

    def click(self, timeout=None):
        self.frame.clicked.append(self.selector)


class FakeFrame:
    def __init__(self, url, selectors=()):
        self.url = url
        self.selectors = set(selectors)
        self.filled = []
        self.clicked = []

    def locator(self, selector):
        return FakeLocator(selector, selector in self.selectors, self)


class FakePage:
    def __init__(self, frames, url="https://x/payer_authentication/hybrid"):
        self.frames = frames
        self.url = url

    def wait_for_timeout(self, ms):
        pass


# --------------------------------------------------------------------------- #
# Finding the field                                                            #
# --------------------------------------------------------------------------- #

def test_the_code_field_is_found_in_the_challenge_iframe():
    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']"])
    page = FakePage([bank])
    found = payment_otp.find_code_field(page, SPEC)
    assert found is not None
    assert found[0] is bank


def test_a_frame_that_matches_no_challenge_marker_is_never_searched():
    """GUARD 1, AND THE MOST IMPORTANT ONE. The checkout form carries inputs
    that would match a loose OTP selector; it must not be reachable at all."""
    checkout = FakeFrame("https://secureacceptance.cybersource.com/pay",
                         ["input[autocomplete='one-time-code']"])
    page = FakePage([checkout])
    assert payment_otp.find_code_field(page, SPEC) is None


def test_an_out_of_band_challenge_reports_no_field():
    """OBSERVED LIVE 2026-10-14: Mashreq's oobForm has hidden fields and a
    Continue button, and NO code input. None is the correct answer and means
    'leave this to the cardholder'."""
    oob = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                    ["input[type='hidden']", "button[name='oobContinue']"])
    page = FakePage([oob])
    assert payment_otp.find_code_field(page, SPEC) is None


def test_an_out_of_band_challenge_is_never_submitted():
    """The approval is the cardholder's. Clicking Continue ahead of them
    fails the authentication outright."""
    oob = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                    ["button[name='oobContinue']", "button[type='submit']"])
    page = FakePage([oob])
    assert payment_otp.fill_and_submit(page, SPEC, "483920") is False
    assert oob.clicked == []
    assert oob.filled == []


def test_an_invisible_field_is_not_filled():
    """An ACS commonly ships a hidden code input next to the OOB form. Filling
    it looks like success and submits nothing."""
    frame = FakeFrame("https://acs.bank.com/challenge/", [])
    page = FakePage([frame])
    assert payment_otp.find_code_field(page, SPEC) is None


def test_a_blank_frame_url_is_skipped():
    page = FakePage([FakeFrame("about:blank", ["input[name*='otp' i]"]),
                     FakeFrame("", ["input[name*='otp' i]"])])
    assert payment_otp.find_code_field(page, SPEC) is None


# --------------------------------------------------------------------------- #
# Filling and submitting                                                       #
# --------------------------------------------------------------------------- #

def test_the_code_is_typed_and_the_banks_submit_is_clicked():
    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])
    assert payment_otp.fill_and_submit(page, SPEC, "483920") is True
    assert bank.filled == [("input[autocomplete='one-time-code']", "483920")]
    assert bank.clicked == ["input[type='submit']"]


def test_the_submit_is_only_looked_for_in_the_fields_own_frame():
    """GUARD 2. A submit in a DIFFERENT frame — the checkout's Pay button,
    for instance — must never be clicked."""
    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']"])
    other = FakeFrame("https://acs.other.com/step-up/",
                      ["input[type='submit']"])
    page = FakePage([bank, other])
    assert payment_otp.fill_and_submit(page, SPEC, "483920") is False
    assert other.clicked == []
    # The code WAS typed — reported honestly as a partial, because many ACS
    # forms auto-submit on the last digit.
    assert bank.filled


def test_a_control_that_is_also_the_pay_button_is_vetoed():
    """GUARD 3. Should be unreachable given guards 1 and 2 — which is not a
    property anybody can verify at 2am on a live payment."""
    spec = dict(SPEC, submit="input[type='submit']")
    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])
    assert payment_otp.fill_and_submit(page, spec, "483920") is False
    assert bank.clicked == []


def test_no_field_means_no_fill_and_no_click():
    page = FakePage([FakeFrame("https://acs.bank.com/challenge/", [])])
    assert payment_otp.fill_and_submit(page, SPEC, "483920") is False


def test_selectors_may_be_a_bare_string_in_the_config():
    """Config values are hand-edited; a string where a list was expected must
    not be a crash on the payment path."""
    spec = dict(SPEC, otp_field="input#code", otp_submit="input#go")
    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input#code", "input#go"])
    page = FakePage([bank])
    assert payment_otp.fill_and_submit(page, spec, "483920") is True


def test_a_processor_with_no_otp_config_falls_back_to_defaults():
    spec = {"challenge_markers": ["acs"], "submit": "input#pay"}
    bank = FakeFrame("https://acs.bank.com/x/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])
    assert payment_otp.fill_and_submit(page, spec, "483920") is True


def test_a_page_whose_frames_cannot_be_read_does_not_raise():
    class Hostile:
        url = "https://x/payer_authentication/"

        @property
        def frames(self):
            raise RuntimeError("browser closed")

    assert payment_otp.find_code_field(Hostile(), SPEC) is None
    assert payment_otp.fill_and_submit(Hostile(), SPEC, "483920") is False


# --------------------------------------------------------------------------- #
# The gateway loop                                                             #
# --------------------------------------------------------------------------- #

def _gateway_spec():
    return dict(SPEC, otp_max_attempts=2)


def test_the_attempt_cap_is_clamped_whatever_the_config_says():
    """A config typo must not be able to spend the card's retry budget — a
    bank locks the card after a few wrong codes."""
    from src.payment import gateway

    assert gateway._otp_max_attempts({"otp_max_attempts": 99}) == 3
    assert gateway._otp_max_attempts({"otp_max_attempts": -5}) == 0
    assert gateway._otp_max_attempts({"otp_max_attempts": "nonsense"}) == 2
    assert gateway._otp_max_attempts({}) == 2


def test_the_challenge_loop_stops_filling_after_the_cap():
    """An endless-resend bank, or a parsing bug, must not keep typing."""
    from src.payment import gateway
    from src.otp_relay.inbox import OtpInbox
    from src.otp_relay.relayion import Message

    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])

    counter = {"n": 0}

    def always_new(since, sender):
        counter["n"] += 1
        return [Message(id=f"m{counter['n']}", sender="BANK",
                        body=f"Your OTP is 48392{counter['n']}",
                        received_at=NOW + timedelta(seconds=30))]

    inbox = OtpInbox(poll_s=1, fetch=always_new)
    inbox.arm(NOW)

    with pytest.raises(gateway.PaymentSubmitted):
        gateway._await_challenge(page, _gateway_spec(), None, None, "",
                                 timeout_ms=0, otp=inbox)

    assert len(bank.filled) <= 2, bank.filled


def test_the_handback_is_checked_before_any_code_is_typed():
    """If the gateway has already returned to VFS the challenge is over, and
    typing into a dead form is at best pointless."""
    from src.payment import gateway
    from src.otp_relay.inbox import OtpInbox
    from src.otp_relay.relayion import Message

    bank = FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage(
        [bank],
        url="https://visa.vfsglobal.com/are/en/nor/confirmation"
            "?PaymentStatus=True")

    inbox = OtpInbox(poll_s=1, fetch=lambda s, n: [
        Message(id="m1", sender="BANK", body="Your OTP is 483920",
                received_at=NOW + timedelta(seconds=30))])
    inbox.arm(NOW)

    gateway._await_challenge(page, _gateway_spec(), None, None, "",
                             timeout_ms=5000, otp=inbox)
    assert bank.filled == []


def test_no_inbox_means_the_old_human_only_behaviour():
    """The fallback path is the one that predates auto-fill, and it must be
    untouched: wait, then raise PaymentSubmitted."""
    from src.payment import gateway

    page = FakePage([FakeFrame("https://acs.mashreq.com/BrowserChallenge/")])
    with pytest.raises(gateway.PaymentSubmitted, match="not completed"):
        gateway._await_challenge(page, _gateway_spec(), None, None, "",
                                 timeout_ms=0, otp=None)


def test_the_whole_chain_with_the_real_rakbank_sms():
    """*** END TO END, with the exact SMS the live card's issuer sends. ***

    Relay message -> armed freshness check -> extraction -> the bank's field
    -> the bank's submit. Every layer this feature has, in the order a live
    payment exercises them, driven by the wording the operator supplied on
    2026-10-15 rather than by a constructed example.

    The one thing it cannot prove is that RAKBANK's real challenge page uses
    a selector in `otp_field`. Nothing offline can prove that — only the
    first live typed-OTP challenge will.
    """
    from src.payment import gateway
    from src.otp_relay.inbox import OtpInbox
    from src.otp_relay.relayion import Message

    sms = ("Use OTP 632007 to complete your 83.3 AED txn at RAKBANK on card "
           "ending with 9340 (Ref: XWE). Valid for 5 mins. Don't share it. "
           "Not You? Call 600 500 946.")

    bank = FakeFrame("https://acs.rakbank.ae/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])

    inbox = OtpInbox(digits=6, poll_s=1, fetch=lambda since, sender: [
        Message(id="rak-1", sender="RAKBANK", body=sms,
                received_at=NOW + timedelta(seconds=20))])
    inbox.arm(NOW)

    # timeout_ms=0 so the wait ends immediately after the fill attempt; the
    # assertion is about what reached the bank's form, not about the outcome.
    with pytest.raises(gateway.PaymentSubmitted):
        gateway._await_challenge(page, _gateway_spec(), None, None, "",
                                 timeout_ms=0, otp=inbox)

    assert bank.filled == [("input[autocomplete='one-time-code']", "632007")]
    assert bank.clicked == ["input[type='submit']"]


def test_a_stale_rakbank_sms_from_a_previous_attempt_is_never_typed():
    """The same issuer, the same wording, received BEFORE the Pay click. It
    must not reach the form: it is already spent, the bank will reject it,
    and rejections are what lock the card."""
    from src.payment import gateway
    from src.otp_relay.inbox import OtpInbox
    from src.otp_relay.relayion import Message

    sms = ("Use OTP 632007 to complete your 83.3 AED txn at RAKBANK on card "
           "ending with 9340 (Ref: XWE). Valid for 5 mins.")

    bank = FakeFrame("https://acs.rakbank.ae/BrowserChallenge/",
                     ["input[autocomplete='one-time-code']",
                      "input[type='submit']"])
    page = FakePage([bank])

    inbox = OtpInbox(digits=6, poll_s=1, grace_s=90,
                     fetch=lambda since, sender: [
                         Message(id="rak-old", sender="RAKBANK", body=sms,
                                 received_at=NOW - timedelta(minutes=10))])
    inbox.arm(NOW)

    with pytest.raises(gateway.PaymentSubmitted):
        gateway._await_challenge(page, _gateway_spec(), None, None, "",
                                 timeout_ms=0, otp=inbox)

    assert bank.filled == []
    assert bank.clicked == []


def test_a_relay_that_throws_does_not_abandon_the_payment():
    """*** An exception escaping here reports an unknown outcome for a payment
    the browser is about to tell us about. *** It must degrade to the human
    wait, not propagate."""
    from src.payment import gateway
    from src.otp_relay.inbox import OtpInbox

    page = FakePage([FakeFrame("https://acs.mashreq.com/BrowserChallenge/",
                               ["input[autocomplete='one-time-code']"])])

    def boom(since, sender):
        raise RuntimeError("relay exploded")

    inbox = OtpInbox(poll_s=1, fetch=boom)
    inbox.arm(NOW)

    with pytest.raises(gateway.PaymentSubmitted, match="not completed"):
        gateway._await_challenge(page, _gateway_spec(), None, None, "",
                                 timeout_ms=0, otp=inbox)
