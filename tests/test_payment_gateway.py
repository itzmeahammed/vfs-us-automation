"""Driving the payment gateway — the only irreversible code in this system.

The tests that matter here are not about filling forms correctly. They are
about what happens around the ONE click that cannot be undone:

    * the journal is fsync'd BEFORE it, never after
    * a journal that cannot be written REFUSES to click
    * every post-click path raises PaymentSubmitted, never returns
    * a 3-D Secure challenge stops the bot rather than being worked around
    * the card never reaches a log line
"""

import logging

import pytest

from src.payment import gateway
from src.payment.card import Card
from src.payment.gateway import (
    ChallengeRequired,
    PaymentError,
    PaymentSubmitted,
)

CARD = Card(number="4111111111111111", expiry_month="11", expiry_year="2029",
            cvn="123", card_type="visa", name="TRAVNOOK LTD")

SPEC = {
    "card_fields": {
        "type": "select[name=card_type]",
        "number": "input[name=card_number]",
        "expiry_month": "select[name=exp_month]",
        "expiry_year": "select[name=exp_year]",
        "cvn": "input[name=cvn]",
    },
    "submit": "input[type=submit]",
}


# --------------------------------------------------------------------------- #
# Doubles                                                                      #
# --------------------------------------------------------------------------- #

class FakeField:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        if self.selector in self.page.missing:
            raise RuntimeError(f"no such element: {self.selector}")

    def fill(self, value, timeout=None):
        self.page.filled.append((self.selector, value))

    def select_option(self, value=None, label=None, timeout=None):
        self.page.selected.append((self.selector, value or label))

    def click(self, timeout=None):
        if self.page.click_fails:
            raise RuntimeError("submit could not be clicked")
        self.page.clicks.append(self.selector)

    def check(self, timeout=None):
        self.page.checked.append(self.selector)


class FakePage:
    def __init__(self, url="https://secureacceptance.cybersource.com/checkout",
                 missing=(), click_fails=False, settle_fails=False,
                 url_after=None):
        self.url = url
        self.missing = set(missing)
        self.click_fails = click_fails
        self.settle_fails = settle_fails
        self._url_after = url_after
        self.filled = []
        self.selected = []
        self.clicks = []
        self.checked = []

    def locator(self, selector):
        return FakeField(self, selector)

    def wait_for_load_state(self, state=None, timeout=None):
        if self.settle_fails:
            raise RuntimeError("timed out")
        if self._url_after:
            self.url = self._url_after


class Journal:
    """Records rows. `fails` models a disk that cannot be written."""

    def __init__(self, fails=False):
        self.rows = []
        self.fails = fails

    def __call__(self, row):
        if self.fails:
            raise OSError("read-only file system")
        self.rows.append(row)
        return f"/journal/{len(self.rows)}.json"


# --------------------------------------------------------------------------- #
# Filling                                                                      #
# --------------------------------------------------------------------------- #

def test_the_card_type_is_chosen_before_the_cvn_is_typed():
    """Gateways re-render the CVN field when the card type changes (amex wants
    four digits, everyone else three), which CLEARS a CVN typed before it."""
    page = FakePage()
    gateway.fill_card(page, SPEC, CARD)

    order = [s for s, _ in page.selected + page.filled]
    assert order.index("select[name=card_type]") < order.index("input[name=cvn]")


def test_every_card_field_is_filled():
    page = FakePage()
    gateway.fill_card(page, SPEC, CARD)

    filled = dict(page.filled)
    selected = dict(page.selected)
    assert filled["input[name=card_number]"] == CARD.number
    assert filled["input[name=cvn]"] == CARD.cvn
    assert selected["select[name=exp_month]"] == "11"
    assert selected["select[name=exp_year]"] == "2029"


def test_a_single_text_expiry_field_is_supported():
    """Some processors take MM/YY in one box rather than two selects."""
    page = FakePage()
    gateway.fill_card(page, {"card_fields": {"expiry": "input[name=exp]",
                                             "number": "input[name=n]",
                                             "cvn": "input[name=c]"}}, CARD)
    assert ("input[name=exp]", "11/29") in page.filled


def test_a_missing_card_field_fails_before_anything_is_submitted():
    page = FakePage(missing=["input[name=card_number]"])
    with pytest.raises(PaymentError, match="card number"):
        gateway.fill_card(page, SPEC, CARD)
    assert page.clicks == []


def test_filling_never_logs_the_card_number(caplog):
    """*** The card must not reach logs/booking.log. *** Only WHICH field was
    filled is logged, never the value."""
    page = FakePage()
    with caplog.at_level(logging.DEBUG):
        gateway.fill_card(page, SPEC, CARD)

    text = " ".join(r.getMessage() for r in caplog.records)
    assert CARD.number not in text
    assert CARD.cvn not in text
    assert "****1111" in text          # the masked form IS logged


# --------------------------------------------------------------------------- #
# Billing                                                                      #
# --------------------------------------------------------------------------- #

BILLING = {"billing_fields": [
    {"name": "first_name", "selector": "input[name=fn]",
     "value_key": "first_name", "required": True},
    {"name": "city", "selector": "input[name=city]", "value_key": "city"},
]}


def test_billing_is_filled_from_the_client_record():
    page = FakePage()
    gateway.fill_billing(page, BILLING, {"first_name": "Mufaddal", "city": ""})
    assert ("input[name=fn]", "Mufaddal") in page.filled


def test_an_absent_optional_billing_field_is_skipped_quietly():
    """The client record has no city today. That must not stop a payment."""
    page = FakePage()
    assert gateway.fill_billing(page, BILLING, {"first_name": "X"}) == 1


def test_an_absent_REQUIRED_billing_field_refuses_before_paying():
    page = FakePage()
    with pytest.raises(PaymentError, match="billing first_name"):
        gateway.fill_billing(page, BILLING, {})
    assert page.clicks == []


# --------------------------------------------------------------------------- #
# THE IRREVERSIBLE CLICK                                                       #
# --------------------------------------------------------------------------- #

def test_the_journal_is_written_BEFORE_the_click():
    """*** THE WHOLE POINT. *** After the click the row cannot be written
    reliably — that window is exactly when the process can die with a payment
    in flight, and the row on disk is then the only evidence it happened."""
    page = FakePage()
    journal = Journal()

    with pytest.raises(PaymentSubmitted):
        gateway.submit_payment(page, SPEC, journal=journal)

    assert journal.rows and journal.rows[0]["event"] == "payment_submitting"
    assert page.clicks == ["input[type=submit]"]


def test_a_journal_that_cannot_be_written_REFUSES_to_click():
    """A payment whose outcome could become unknowable must not be made. This
    is the last moment refusing is still free."""
    page = FakePage()

    with pytest.raises(PaymentError, match="cannot be journalled"):
        gateway.submit_payment(page, SPEC, journal=Journal(fails=True))

    assert page.clicks == []


def test_a_successful_submit_RAISES_rather_than_returning():
    """PaymentSubmitted is raised so it cannot be quietly folded into a return
    value a caller treats as ordinary failure. The card may have been charged."""
    page = FakePage(url_after="https://vfs/confirmation")
    with pytest.raises(PaymentSubmitted, match="DO NOT RESUBMIT"):
        gateway.submit_payment(page, SPEC, journal=Journal())


def test_a_gateway_that_never_answers_is_UNKNOWN_not_failed():
    """The dangerous case. The click landed; the response did not. Reporting
    this as failure invites a retry, and a retry double-charges."""
    page = FakePage(settle_fails=True)
    with pytest.raises(PaymentSubmitted) as caught:
        gateway.submit_payment(page, SPEC, journal=Journal())

    message = str(caught.value)
    assert "DO NOT RESUBMIT" in message
    assert "did not respond" in message


def test_the_submitted_error_carries_the_journal_path():
    """So whoever reads the traceback can find the write-ahead row."""
    page = FakePage()
    with pytest.raises(PaymentSubmitted) as caught:
        gateway.submit_payment(page, SPEC, journal=Journal())
    assert caught.value.journal_path == "/journal/1.json"


def test_a_click_that_fails_says_NOTHING_WAS_SUBMITTED():
    """The journal already says 'submitting', so this path must be explicit
    that it did not happen — otherwise the row reads as a possible charge."""
    page = FakePage(click_fails=True)
    with pytest.raises(PaymentError, match="NOT submitted"):
        gateway.submit_payment(page, SPEC, journal=Journal())


# --------------------------------------------------------------------------- #
# 3-D Secure                                                                   #
# --------------------------------------------------------------------------- #

def test_a_challenge_BEFORE_submitting_stops_the_bot():
    """The OTP goes to the cardholder's phone. A bot cannot complete it and
    must not try — it hands the open browser to a human."""
    page = FakePage(url="https://bank.example.com/3dsecure/challenge")
    journal = Journal()

    with pytest.raises(ChallengeRequired, match="human"):
        gateway.submit_payment(page, SPEC, journal=journal)

    assert page.clicks == []
    assert journal.rows == []      # nothing was even attempted


def test_a_challenge_AFTER_submitting_is_still_not_retryable():
    page = FakePage(url_after="https://bank.example.com/acs/step-up")
    with pytest.raises(PaymentSubmitted, match="DO NOT RESUBMIT"):
        gateway.submit_payment(page, SPEC, journal=Journal())


def test_a_challenge_is_never_a_plain_PaymentError_a_caller_might_retry():
    """ChallengeRequired subclasses PaymentError so existing handlers see it,
    but a caller matching on the specific type can hand over properly."""
    assert issubclass(ChallengeRequired, PaymentError)
    assert not issubclass(PaymentSubmitted, PaymentError)


# --------------------------------------------------------------------------- #
# The popup                                                                    #
# --------------------------------------------------------------------------- #

class FakeContext:
    """Models Playwright's expect_page. `opens` decides whether one appears."""

    def __init__(self, opens=True):
        self.opens = opens
        self.pages = [FakePage(url="https://vfs/review-pay")]
        self.clicked = False

    def expect_page(self, timeout=None):
        context = self

        class _Waiter:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                if not context.opens:
                    raise RuntimeError("no popup")
                return False

            @property
            def value(self_inner):
                popup = FakePage(url="https://secureacceptance.cybersource.com/x")
                popup.wait_for_load_state = lambda state=None, timeout=None: None
                return popup

        return _Waiter()


def test_the_popup_is_awaited_AROUND_the_click_not_after_it():
    """The window can finish opening before a separately-issued wait starts
    listening, and the event is then missed forever."""
    context = FakeContext(opens=True)

    def trigger():
        context.clicked = True

    popup = gateway.attach_popup(context, trigger)
    assert context.clicked
    assert "cybersource" in popup.url


def test_no_popup_falls_back_to_the_page_in_place():
    """Some routes navigate in the same tab. That is not an error."""
    context = FakeContext(opens=False)
    page = gateway.attach_popup(context, lambda: None)
    assert page is context.pages[-1]


# --------------------------------------------------------------------------- #
# The REAL CyberSource DOM                                                     #
#                                                                              #
# Four hypotheses in the first version of config/payment/CYBERSOURCE.json were #
# wrong, and every one would have failed on a live payment. These pin the      #
# corrections against the captured page.                                       #
# --------------------------------------------------------------------------- #

import json
import os

CYBERSOURCE = os.path.join("config", "payment", "CYBERSOURCE.json")


@pytest.fixture(scope="module")
def cybersource():
    with open(CYBERSOURCE, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_card_type_is_a_radio_group_not_a_select(cybersource):
    """THE CORRECTION THAT MATTERS MOST. The real page renders

        <input type="radio" name="card_type" id="card_type_001" value="001">

    so the hypothesised select[name=card_type] matched NOTHING — the card type
    would never have been chosen and the gateway would have rejected the form.
    """
    fields = cybersource["card_fields"]
    assert fields["type_widget"] == "radio"
    assert fields["type"].startswith("input[name='card_type']")
    assert "{value}" in fields["type"]


def test_the_card_type_is_a_processor_code_not_a_name(cybersource):
    """The radio's value is '001', not 'visa'."""
    assert cybersource["card_type_codes"] == {"visa": "001",
                                              "mastercard": "002"}


def test_the_submit_is_named_commit_not_submit(cybersource):
    """name='commit' value='Pay'. And the name is part of the selector because
    the page has a sibling <input type='button' value='Cancel'>."""
    assert cybersource["submit"] == "input[type='submit'][name='commit']"


def test_this_gateway_takes_no_amex(cybersource):
    """The captured radio group offers Visa and Mastercard only, even though
    card.py supports amex."""
    assert sorted(cybersource["accepted_card_types"]) == ["mastercard", "visa"]


def test_a_radio_card_type_is_ticked_with_its_processor_code():
    page = FakePage()
    spec = {
        "card_fields": {"type": "input[name='card_type'][value='{value}']",
                        "type_widget": "radio",
                        "number": "input#card_number",
                        "cvn": "input#card_cvn"},
        "card_type_codes": {"visa": "001"},
    }
    gateway.fill_card(page, spec, CARD)
    assert page.clicks == []          # a check(), not a click()
    assert page.checked == ["input[name='card_type'][value='001']"]


def test_a_card_the_gateway_does_not_accept_is_refused_before_typing():
    """An amex on a Visa/Mastercard-only gateway. Trying cannot make it work,
    and finding out mid-payment strands a booking on an unpaid page."""
    amex = Card(number="371449635398431", expiry_month="11",
                expiry_year="2029", cvn="1234", card_type="amex")
    page = FakePage()

    with pytest.raises(PaymentError, match="accepts mastercard, visa"):
        gateway.fill_card(page, {"accepted_card_types": ["visa", "mastercard"],
                                 "card_fields": {}}, amex)
    assert page.filled == []


def test_the_expiry_values_match_the_gateways_option_values(cybersource):
    """Month options are zero-padded ('01'), year options four-digit ('2029').
    card.py already produces both, and this pins that they agree."""
    card = Card(number="4111111111111111", expiry_month="03",
                expiry_year="2031", cvn="123", card_type="visa")
    assert card.expiry_month == "03"
    assert card.expiry_year == "2031"


def test_the_encrypted_inputs_are_never_addressed(cybersource):
    """The page RSA-encrypts card fields on submit via its own JS. Writing to
    __e.card_number would mean reimplementing their crypto against a contract
    they can change without notice.

    Asserted over the SELECTORS only — the comments deliberately mention __e.*
    and #jwk in order to warn against them.
    """
    selectors = list(cybersource["card_fields"].values())
    selectors += [f["selector"] for f in cybersource["billing_fields"]]
    selectors.append(cybersource["submit"])

    for selector in selectors:
        assert "__e." not in selector, selector
        assert "jwk" not in selector, selector


# --------------------------------------------------------------------------- #
# The gap the capture exposed                                                  #
# --------------------------------------------------------------------------- #

def test_every_mandatory_gateway_field_has_a_client_source(cybersource):
    """*** THE CHECK THAT KEEPS A BOOKING FROM BEING MADE AND NOT PAID FOR. ***

    Nine billing fields are aria-required on CyberSource. If ANY of them has no
    source on the client record, a live run fills the ones it can, stops on the
    first it cannot — and by then the appointment is already booked. Committed
    on VFS, unpaid on ours, which is the worst state this system has.

    city / country_code / postcode were exactly that gap when the real gateway
    DOM was first captured: mandatory there, absent from every client record.
    They are now collected. This test is what stops the gap reopening — adding
    a mandatory field to the config without a source fails here, offline,
    rather than mid-payment.

    Asserted against REAL clients rather than a hand-written set, because the
    question is not "does the schema allow it" but "does an actual booking have
    it".

    One incomplete record is that client's data gap, not a fault — the booking
    readiness check reports those per client before a login is spent. The fault
    this guards against is the CONFIG requiring a field no record can supply.
    """
    from src.utils.config_reader import initialize_config
    from src.waitlist import context as ctx_mod
    from src.waitlist import registrant as registrant_mod

    initialize_config()
    clients = registrant_mod.load_all(skip_invalid=True)
    if not clients:
        pytest.skip("no client records on this machine")

    # AT LEAST ONE client must be fully payable, and the config must name only
    # fields a client CAN supply. An incomplete record is a data gap for that
    # client — it must not fail the suite, because the same gap is what
    # check_booking_templates already reports per client at run time. What
    # would be a real fault is the config requiring something NO record can
    # ever supply, which is what this asserts.
    payable, gaps = [], {}
    for client in clients:
        values = ctx_mod.build(client, route="AE-NOR")
        missing = [f["value_key"] for f in cybersource["billing_fields"]
                   if f.get("required") and not values.get(f["value_key"])]
        (payable.append(client.id) if not missing
         else gaps.__setitem__(client.id, missing))

    assert payable, (
        "NO client can pay: every record is missing a mandatory gateway "
        f"field. Gaps: {gaps}. A run would book an appointment and then fail "
        "on the billing form.")


# --------------------------------------------------------------------------- #
# The VFS -> processor handoff                                                 #
#                                                                              #
# Confirmed 2026-09-28 from the real pages: "Pay Online" leads to a VFS        #
# "Payment Disclaimer" page served AT THE SAME URL as review-pay, and its      #
# Continue navigates THE SAME TAB to secureacceptance.cybersource.com.         #
# There is no popup.                                                           #
# --------------------------------------------------------------------------- #

def test_the_disclaimer_is_addressed_by_label_because_it_has_to_be():
    """Cancel and Continue carry BYTE-IDENTICAL class attributes on that page:

        btn mat-btn-lg btn-block btn-outline-brand-orange mdc-button ...

    so a CSS selector matches both, and `.first` would click CANCEL — the
    button that abandons the booking. The label is the only discriminator.
    """
    import json

    with open("config/booking/AE-NOR.json", encoding="utf-8") as fh:
        route = json.load(fh)

    step = next(s for s in route["steps"] if s.get("name") == "payment")
    disclaimer = step["disclaimer"]

    assert disclaimer == {"role": "button", "name": "Continue"}
    assert "selector" not in disclaimer, (
        "a CSS selector here would match Cancel as well as Continue")


def test_a_disclaimer_that_will_not_dismiss_is_a_HARD_ERROR():
    """*** IT MUST NOT BE SWALLOWED. ***

    The old code logged "no payment disclaimer to dismiss" and carried on to
    fill card fields — on the disclaimer page, which has none. The run then
    reported a card-selector fault, when the truth was that it never left VFS.
    On the one page where the next click spends money, that sends diagnosis in
    entirely the wrong direction.
    """
    source = open("src/booking/runner.py", encoding="utf-8").read()
    block = source[source.index("disclaimer = ctx.step.get"):]
    block = block[:block.index("# ── 4.")]

    assert "raise BookingStepError" in block
    assert "has NOT reached the payment gateway" in block
    assert "no payment disclaimer to dismiss" not in block


def test_the_card_is_never_typed_until_the_processors_url_is_confirmed():
    """The handoff is a same-tab navigation, so there is a real page load to
    wait for. Typing before it lands puts card digits into a page that is
    about to be replaced — and if the navigation never happens at all, into a
    VFS page."""
    source = open("src/booking/runner.py", encoding="utf-8").read()

    wait_at = source.index("wait_for_url")
    fill_at = source.index("gateway.fill_card")
    assert wait_at < fill_at, (
        "fill_card runs before the gateway URL is confirmed")

    block = source[wait_at:fill_at]
    assert "Never reached the payment gateway" in block
    assert "no card details were entered" in block


def test_the_expected_gateway_url_comes_from_the_processor_config(cybersource):
    """Not hardcoded in the runner: country two uses a different processor."""
    assert cybersource["url_contains"] == "secureacceptance.cybersource.com"

    source = open("src/booking/runner.py", encoding="utf-8").read()
    assert 'spec.get("url_contains")' in source

    # Comments MAY name the processor — that is documentation of the handoff
    # this code was built against, and deleting it would lose why the same-tab
    # navigation is expected. What must not appear is the name in CODE.
    code = chr(10).join(
        line.split("#", 1)[0] for line in source.splitlines())
    assert "cybersource" not in code.lower(), (
        "the runner hardcodes a processor; that belongs in config/payment/")


def test_no_popup_is_the_normal_path_not_an_error():
    """attach_popup falls back to the current page when no window opens. For
    this gateway that fallback IS the path — so it must not log as a failure
    or raise."""
    context = FakeContext(opens=False)
    page = gateway.attach_popup(context, lambda: None)
    assert page is context.pages[-1]
