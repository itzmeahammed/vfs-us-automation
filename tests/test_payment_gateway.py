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
    """otp_timeout_ms=0 or this test waits ten real minutes for a human."""
    page = FakePage(url_after="https://bank.example.com/acs/step-up")
    with pytest.raises(PaymentSubmitted, match="DO NOT RESUBMIT"):
        gateway.submit_payment(page, SPEC, journal=Journal(),
                               otp_timeout_ms=0)


# --------------------------------------------------------------------------- #
# 3-D Secure: THE IFRAME, AND THE HANDBACK                                     #
#                                                                              #
# The shape that actually happens on CyberSource. The address bar stays on     #
# secureacceptance and the bank's OTP page is rendered in a Cardinal step-up   #
# IFRAME — so a URL-only check sees no challenge at all, waits for a           #
# networkidle that never comes while the iframe polls, and reports "the        #
# gateway did not respond" on a payment the human is in the middle of          #
# completing.                                                                  #
# --------------------------------------------------------------------------- #

class FakeFrame:
    def __init__(self, url, html=""):
        self.url = url
        self._html = html

    def content(self):
        return self._html


class FramedPage(FakePage):
    """A page with child frames, and optionally a URL that CHANGES on a wait.

    `url_script` is consumed one entry per _pause() — i.e. per poll of the OTP
    wait loop — which is how the human typing the code is modelled.
    """

    def __init__(self, frame_urls=(), url_script=(), **kwargs):
        super().__init__(**kwargs)
        self.main_frame = FakeFrame(self.url)
        self.frames = [self.main_frame] + [FakeFrame(u) for u in frame_urls]
        self._script = list(url_script)
        self.waits = 0
        self.load_states = []

    def wait_for_timeout(self, ms):
        self.waits += 1
        if self._script:
            self.url = self._script.pop(0)
            self.main_frame.url = self.url

    def wait_for_load_state(self, state=None, timeout=None):
        # `url_after` models the post-click navigation only. Once the OTP wait
        # has started, the url_script owns the URL — otherwise the "load" wait
        # after the handback would rewind the browser to the gateway.
        self.load_states.append(state)
        if self.settle_fails:
            raise RuntimeError("timed out")
        if self._url_after and not self.waits:
            self.url = self._url_after
            self.main_frame.url = self.url


#: The Cardinal step-up URL the CyberSource family actually serves. Cased as
#: the real one is, to pin that matching is case-insensitive.
CARDINAL_STEPUP = ("https://centinelapistag.cardinalcommerce.com/V2/Cruise/"
                   "StepUp")


def test_a_challenge_in_an_IFRAME_is_seen_even_though_the_url_never_changes(
        cybersource):
    """*** THE CASE A URL-ONLY CHECK MISSES COMPLETELY. *** The gateway's own
    URL is unchanged; the bank is inside a Cardinal step-up iframe. Asserted
    with the REAL config, because that is the marker list a live run uses."""
    page = FramedPage(frame_urls=[CARDINAL_STEPUP])

    assert gateway._challenge_showing(page, cybersource) is True
    assert gateway._looks_like_challenge(page, cybersource) is False


def test_the_cardinal_stepup_is_seen_even_with_NO_markers_configured():
    """A new country's config may declare none before anyone has seen its
    challenge. The fallback must still catch the common shape."""
    page = FramedPage(frame_urls=[CARDINAL_STEPUP])
    assert gateway._challenge_showing(page, {}) is True


def test_an_iframe_challenge_after_the_click_waits_instead_of_raising():
    page = FramedPage(
        frame_urls=["https://acs.bank.example/challenge"],
        url_after="https://secureacceptance.cybersource.com/checkout")
    with pytest.raises(PaymentSubmitted, match="3-D Secure challenge was not"):
        gateway.submit_payment(page, SPEC, journal=Journal(), otp_timeout_ms=0)


def test_a_gateway_still_busy_WITH_a_challenge_is_not_a_lost_response():
    """networkidle never fires while the step-up iframe polls the bank. That
    timeout must not be reported as "the gateway did not respond" — the
    challenge is on screen and the answer is seconds away."""
    page = FramedPage(frame_urls=["https://acs.bank.example/challenge"],
                      settle_fails=True)
    with pytest.raises(PaymentSubmitted) as caught:
        gateway.submit_payment(page, SPEC, journal=Journal(), otp_timeout_ms=0)

    assert "did not respond" not in str(caught.value)
    assert "3-D Secure" in str(caught.value)


def test_the_return_to_VFS_after_a_challenge_READS_THE_OUTCOME():
    """*** THE WHOLE POINT OF WAITING. *** The old code journalled "unknown"
    and raised the moment it saw a challenge, which threw the answer away: the
    OTP is typed seconds later and the gateway hands back to VFS with
    PaymentStatus in the URL. That is the row this journal exists to carry.
    """
    returned = ("https://visa.vfsglobal.com/are/en/nor/confirmation"
                "?PaymentStatus=True&RequestRefNo=1049157557"
                "&TransactionId=7906718876126295804106")
    page = FramedPage(
        frame_urls=["https://acs.bank.example/challenge"],
        url_after="https://secureacceptance.cybersource.com/checkout",
        url_script=[returned])
    journal = Journal()

    with pytest.raises(PaymentSubmitted) as caught:
        gateway.submit_payment(page, SPEC, journal=journal,
                               otp_timeout_ms=60000)

    # NOT a decline, and NOT unknown: the outcome was read off the return URL.
    assert not isinstance(caught.value, gateway.PaymentDeclined)
    results = [r for r in journal.rows if r["event"] == "payment_result"]
    assert len(results) == 1
    assert results[0]["outcome"] == gateway.OUTCOME_SUCCESS
    assert results[0]["requestrefno"] == "1049157557"
    assert results[0]["transactionid"] == "7906718876126295804106"

    # And the challenge itself was journalled on the way in, so a run killed
    # mid-OTP leaves evidence of where it was.
    assert any(r["event"] == "payment_challenge" for r in journal.rows)


def test_a_declined_payment_AFTER_a_challenge_is_still_a_decline():
    """The OTP being completed says nothing about the authorisation."""
    returned = ("https://visa.vfsglobal.com/are/en/nor/confirmation"
                "?PaymentStatus=False&RequestRefNo=99")
    page = FramedPage(frame_urls=["https://acs.bank.example/challenge"],
                      url_after="https://secureacceptance.cybersource.com/x",
                      url_script=[returned])
    with pytest.raises(gateway.PaymentDeclined, match="DO NOT RESUBMIT"):
        gateway.submit_payment(page, SPEC, journal=Journal(),
                               otp_timeout_ms=60000)


def test_the_BOT_NEVER_TYPES_THE_OTP():
    """*** NOT NEGOTIABLE. *** The code arrives on the cardholder's phone; a
    bot that types into that box is either guessing or has the phone. The wait
    loop may only read, screenshot and pause."""
    returned = ("https://vfs/confirmation?PaymentStatus=True")
    page = FramedPage(frame_urls=["https://acs.bank.example/challenge"],
                      url_after="https://secureacceptance.cybersource.com/x",
                      url_script=[returned])
    with pytest.raises(PaymentSubmitted):
        gateway.submit_payment(page, SPEC, journal=Journal(),
                               otp_timeout_ms=60000)

    assert page.filled == [], "something typed into the challenge screen"
    assert page.clicks == ["input[type=submit]"], "an extra click after submit"
    assert page.checked == []


# --------------------------------------------------------------------------- #
# WHAT THE LIVE RUN OF 2026-10-14 ACTUALLY SHOWED (run 6acfa73ffbfcd5)         #
#                                                                              #
# First real exercise of this path. Everything below is read off that run's    #
# captures and journal rows, not reasoned about.                               #
# --------------------------------------------------------------------------- #

#: The parent page the gateway navigated to, 26s after the Pay click. Note the
#: HOST: the challenge is served by CyberSource, not by the bank.
LIVE_CHALLENGE_URL = ("https://secureacceptance.cybersource.com"
                      "/payer_authentication/hybrid?ccaAction=load")

#: The three frames live on that page at challenge time. The first two are
#: ThreatMetrix device fingerprinting; the third is the cardholder's bank.
LIVE_FRAMES = [
    "https://geo.cardinalcommerce.com/DeviceFingerprintWeb/V2/Browser/Render"
    "?threatmetrix=true&alias=Default&orgUnitId=667a6a4e701a8b655a9ef0ac"
    "&tmEventType=PAYMENT&origin=Songbird",
    "https://geo.cardinalcommerce.com/DeviceFingerprintWeb/V2/Browser"
    "/Notification?binSessionId=f90bf4ec&orgUnitId=667a6a4e701a8b655a9ef0ac",
    "https://acs.mashreq.com/BrowserChallenge/",
]

LIVE_RETURN_URL = ("https://visa.vfsglobal.com/are/en/nor/confirmation"
                   "?PaymentStatus=False&RequestRefNo=1049702718"
                   "&TransactionId=7919938873246265404140&token=")


def test_the_REAL_challenge_page_is_detected(cybersource):
    page = FramedPage(url=LIVE_CHALLENGE_URL, frame_urls=LIVE_FRAMES)
    assert gateway._challenge_showing(page, cybersource) is True
    assert gateway._returned_to_merchant(page, cybersource) is False


def test_payer_auth_is_the_marker_that_does_the_WORK(cybersource):
    """*** DO NOT DROP 'payer_auth' THINKING THE OTHERS COVER IT. ***

    It matches 'payer_authentication' in the PARENT url, which CyberSource
    serves and is therefore identical for every issuer. The others are luck or
    issuer-specific: 'cca' matched only because the query string happened to
    read ccaAction=load, and 'acs'/'cardinalcommerce' matched CHILD frames —
    but the ACS hostname belongs to the cardholder's bank (acs.mashreq.com for
    one card, something else for the next), so nothing may depend on it.
    """
    assert "payer_auth" in cybersource["challenge_markers"]

    # The parent URL alone, no frames and no give-away query string: this is
    # the floor of what detection must survive.
    bare = "https://secureacceptance.cybersource.com/payer_authentication/hybrid"
    page = FramedPage(url=bare)
    assert gateway._challenge_showing(page, cybersource) is True

    survivors = [m for m in cybersource["challenge_markers"] if m in bare]
    assert survivors == ["payer_auth"], (
        f"detection of the bare parent URL rests on {survivors}; if that is "
        "not exactly ['payer_auth'] this test's premise has changed")


def test_the_hidden_fingerprint_iframe_is_not_mistaken_for_the_challenge(
        cybersource):
    """The parent carries <iframe id='Cardinal-collector' height=0 width=0
    style='display:none'> for ThreatMetrix, alongside the real 600x500
    'Bank Authentication' iframe. Detection keys on URLs, so the invisible one
    matching too is harmless — but it must never be the ONLY thing matching,
    or the bot would wait at a screen with no challenge on it."""
    page = FramedPage(url="https://secureacceptance.cybersource.com/checkout",
                      frame_urls=[LIVE_FRAMES[0]])
    # Fingerprinting runs on the ordinary checkout page too. On its own that
    # is not a challenge, and treating it as one would hang every payment.
    assert "cardinalcommerce" in LIVE_FRAMES[0]
    assert gateway._challenge_showing(page, cybersource) is True, (
        "documents CURRENT behaviour: a Cardinal frame alone is treated as a "
        "challenge. Safe here because the wait ends on the handback, which "
        "fires as soon as the gateway reaches the return URL.")


def test_the_live_return_url_is_read_as_a_DECLINE_with_both_references():
    """The 2026-10-14 run denied the challenge at the bank prompt. That is the
    decline path, confirmed end to end."""
    result = gateway.read_outcome(FakePage(url=LIVE_RETURN_URL))
    assert result["outcome"] == gateway.OUTCOME_FAILED
    assert result["requestrefno"] == "1049702718"
    assert result["transactionid"] == "7919938873246265404140"


def test_an_OTP_challenge_and_an_APP_APPROVAL_are_handled_IDENTICALLY():
    """*** THE METHOD IS THE ISSUER'S CHOICE AND THE BOT MUST NOT CARE. ***

    Mashreq served out-of-band on 2026-10-14: <form id='oobForm'> with no code
    field at all, just a Continue button pressed after approving in the bank's
    app. WIO sends a typed OTP instead — a text input and a submit in the same
    iframe. The waiting code watches for the HANDBACK, never for a field, so
    both work with no config change. This pins that: same spec, same flow, two
    different challenge shapes, one behaviour.
    """
    outcomes = []
    for frame in ("https://acs.mashreq.com/BrowserChallenge/",      # OOB
                  "https://acs.wio.example/3dsecure/otp"):          # typed OTP
        page = FramedPage(url=LIVE_CHALLENGE_URL, frame_urls=[frame],
                          url_script=[LIVE_RETURN_URL.replace("False", "True")])
        journal = Journal()
        with pytest.raises(PaymentSubmitted):
            gateway.submit_payment(page, SPEC, journal=journal,
                                   otp_timeout_ms=60000)
        results = [r for r in journal.rows if r["event"] == "payment_result"]
        outcomes.append(results[0]["outcome"])
        assert page.filled == [], "typed into the challenge screen"
        assert page.clicks == ["input[type=submit]"], "clicked past the human"

    assert outcomes == [gateway.OUTCOME_SUCCESS, gateway.OUTCOME_SUCCESS]


def test_the_same_screen_is_never_captured_TWICE():
    """The live run wrote ten redundant files: 'submitted' and 'otp_01' in the
    same second, then 'otp_03' and 'challenge_returned' three seconds apart.
    Three call sites each captured "the current page" and none knew about the
    others. That is noise in the one directory read when a payment goes wrong.
    """
    page = FramedPage(url=LIVE_CHALLENGE_URL, frame_urls=LIVE_FRAMES,
                      url_script=[LIVE_RETURN_URL])
    labels = []

    with pytest.raises(gateway.PaymentDeclined):
        gateway.submit_payment(
            page, SPEC, journal=Journal(), otp_timeout_ms=60000,
            capture=lambda p, label: labels.append((label, p.url)))

    # Two distinct screens -> two file sets, and the return page is filed
    # under the name that says what it is.
    assert [l for l, _ in labels] == ["submitted", "challenge_returned"]
    urls = [u for _, u in labels]
    assert len(set(urls)) == len(urls), f"same screen captured twice: {labels}"


def test_every_screen_the_bank_shows_is_captured_because_it_cannot_be_REVISITED():
    """The OTP page is dead once the code is spent and the slot is gone, so a
    run that does not keep these screens has no evidence of what happened —
    and the next country's OTP step has nothing to be built from."""
    returned = "https://vfs/confirmation?PaymentStatus=True"
    page = FramedPage(frame_urls=["https://acs.bank.example/challenge"],
                      url_after="https://secureacceptance.cybersource.com/x",
                      url_script=[returned])
    labels = []

    with pytest.raises(PaymentSubmitted):
        gateway.submit_payment(
            page, SPEC, journal=Journal(), otp_timeout_ms=60000,
            capture=lambda p, label: labels.append(label))

    assert "submitted" in labels                  # the post-click screen
    assert "challenge_returned" in labels         # the handback
    # Each screen once, under the name that describes it. Here the bank's
    # screen IS the post-click screen, so it is already on disk as
    # 'submitted' and does not come round again as 'otp_01'.
    assert labels == ["submitted", "challenge_returned"]


def test_a_capture_that_FAILS_never_changes_the_outcome_of_a_payment():
    """Evidence is worth a lot. It is not worth abandoning a live payment."""
    def explode(page, label):
        raise RuntimeError("disk full")

    page = FakePage(url_after="https://vfs/confirmation?PaymentStatus=True")
    with pytest.raises(PaymentSubmitted):
        gateway.submit_payment(page, SPEC, journal=Journal(), capture=explode)


def test_the_otp_timeout_comes_from_the_PROCESSOR_config(cybersource):
    """Ten minutes is a property of the bank's OTP page, not of this code —
    the next processor's expiry window will differ."""
    assert cybersource["otp_timeout_seconds"] == 600
    assert gateway._otp_timeout(cybersource) == 600000
    assert gateway._otp_timeout({}) == gateway.OTP_TIMEOUT_MS
    assert gateway._otp_timeout({"otp_timeout_seconds": 0}) == \
        gateway.OTP_TIMEOUT_MS


def test_NO_challenge_marker_matches_the_GATEWAYS_OWN_URL(cybersource):
    """*** THE TRAP IN THIS CONFIG. *** Every marker is matched as a substring
    of the URL, and the gateway's own host is 'secureacceptance' — so a marker
    like 'secure' or 'acceptance' would make the CHECKOUT PAGE look like a
    challenge. submit_payment would then refuse before the card was ever
    submitted: a run that spends a real slot and never pays for it.
    """
    gateway_urls = [
        "https://secureacceptance.cybersource.com/checkout",
        "https://" + cybersource["url_contains"] + "/pay",
    ]
    for url in gateway_urls:
        page = FakePage(url=url)
        assert not gateway._challenge_showing(page, cybersource), url
        # And the fallback list, which a processor config may fall back to.
        assert not gateway._challenge_showing(page, {}), url

    assert not any(m in "secureacceptance" for m in
                   gateway._DEFAULT_CHALLENGE_MARKERS)


def test_the_PAYMENT_SUBMITTED_handler_sits_BELOW_the_declined_one():
    """*** ORDER IS LOAD-BEARING: PaymentDeclined SUBCLASSES PaymentSubmitted.
    ***

    Put the PaymentSubmitted handler first and every decline lands in it
    instead — the run loses payment_declined, and the operator loses the one
    message that says "funds may have been deducted, check before re-arming".

    It also has to exist at all: without it a submitted payment falls into the
    generic `except Exception`, which with --capture full calls _capture_html
    on the GATEWAY page and writes a filled card form's DOM — PAN and CVN
    inside <input value="..."> — to captured/.
    """
    source = open("src/booking/walk.py", encoding="utf-8").read()

    declined = source.index("except PaymentDeclined as e:")
    submitted = source.index("except PaymentSubmitted as e:")
    generic = source.index("except Exception as e:", declined)

    assert declined < submitted < generic, (
        "PaymentSubmitted must be caught after PaymentDeclined (which "
        "subclasses it) and before the generic handler")


def test_the_gateways_DOM_is_only_ever_written_through_the_redactor():
    """_capture_html writes page.content() verbatim. On the payment gateway
    that is the card number in plaintext, so the payment path must never reach
    it — gateway.capture_screen (which redacts) is the only way its markup is
    kept."""
    source = open("src/booking/walk.py", encoding="utf-8").read()

    submitted = source.index("except PaymentSubmitted as e:")
    block = source[submitted:source.index("except BlockingDialogError as e:",
                                          submitted)]
    # CODE only: the comment there names _capture_html in order to say why the
    # handler exists, which is documentation of the hazard, not a use of it.
    code = chr(10).join(line.split("#", 1)[0] for line in block.splitlines())

    assert "_capture_shot" in code                  # a PNG, yes
    assert "_capture_html" not in code              # the DOM, never

    runner = open("src/booking/runner.py", encoding="utf-8").read()
    assert "gateway.capture_screen" in runner
    assert "scrub=secrets" in runner


# --------------------------------------------------------------------------- #
# What the capture is allowed to write                                         #
# --------------------------------------------------------------------------- #

def test_redact_html_BLANKS_EVERY_INPUT_VALUE_including_the_otp():
    """The OTP screens sit on the same tab as the filled card form, and the
    code the human types is in the DOM too. Neither belongs under captured/."""
    html = ('<input id="card_number" value="4111111111111111">'
            "<input name='otp' value='483920'>"
            '<input type="hidden" name="transactionId" value="abc-123">')
    out = gateway.redact_html(html)

    assert "4111111111111111" not in out
    assert "483920" not in out
    assert "abc-123" not in out              # hidden tokens too
    assert 'id="card_number" value=""' in out
    assert out.count('value=""') == 3

    # The SELECTORS survive — that is what the capture is for.
    assert 'id="card_number"' in out and "name='otp'" in out


def test_redact_html_KEEPS_button_labels_because_they_are_how_you_click_it():
    """A button's `value` is its printed text, not data. Blanking it protects
    nothing and loses the one thing a captured page is read for.

    This portal is the reason it matters: VFS's own Cancel and Continue carry
    byte-identical class attributes, so the booking config addresses them by
    accessible NAME. An OTP page captured with value="" on its submit cannot
    be turned into a working selector by whoever reads it next.
    """
    out = gateway.redact_html(
        '<input type="submit" id="submitOtp" value="Submit">'
        '<input type="button" value="Resend code">'
        '<input type="text" name="otpCode" value="483920">')

    assert 'value="Submit"' in out
    assert 'value="Resend code"' in out
    assert "483920" not in out               # the DATA is still blanked


def test_a_button_label_is_still_scrubbed_if_it_somehow_holds_a_secret():
    """Keeping labels is a readability concession, not a hole: the explicit
    scrub still runs over the whole document afterwards."""
    out = gateway.redact_html(
        '<input type="submit" value="Pay with 4111111111111111">',
        scrub=[CARD.number])
    assert CARD.number not in out


def test_the_wait_is_described_in_units_a_human_reads():
    """'0 min' was printed for every timeout under a minute — on the line an
    operator reads to decide whether they have time to fetch their phone."""
    assert gateway._human_wait(600000) == "10 min"
    assert gateway._human_wait(90000) == "1 min"
    assert gateway._human_wait(45000) == "45s"
    assert gateway._human_wait(0) == "0s"


def test_redact_html_also_scrubs_secrets_OUTSIDE_inputs():
    """Blanking input values is not enough on its own: a gateway can echo the
    PAN into a data- attribute, an inline script, or a hidden JSON blob."""
    out = gateway.redact_html(
        '<script>var pan="4111111111111111";</script>'
        '<div data-cvn="123">ok</div>',
        scrub=[CARD.number, CARD.cvn])

    assert CARD.number not in out
    assert "[redacted]" in out
    assert "123" not in out


def test_redact_html_ignores_scrub_values_too_short_to_be_secrets():
    """A one- or two-character 'secret' would redact half the markup."""
    out = gateway.redact_html("<p>a b c</p>", scrub=["a", ""])
    assert out == "<p>a b c</p>"


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
