"""CyberSource Unified Checkout (AE/HUN) — src/payment/unified.py and the
gateway changes it needs, against the operator's real capture where one exists.

The capture (docs/hungary_payment.txt/payment_selector.txt, 2026-10-22) covers
the landing page, the "Checkout with card" launcher, the review step and the
callback script. It does NOT cover the card form's inner DOM, so the fakes
below use the config's first-ranked candidates for those fields.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from src.payment import gateway, unified
from src.payment.card import Card

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(ROOT, "docs", "hungary_payment.txt",
                       "payment_selector.txt")
UC_CONFIG = os.path.join(ROOT, "config", "payment", "CYBERSOURCE_UC.json")

LANDING = ("https://online.vfsglobal.com/PG-Component/Payment/CyberSourceNew"
           "?initiateResult=ZTzSisM8dANTvI%2FpBHPj5w%3D%3D")
CALLBACK = ("https://online.vfsglobal.com/PG-Component/Payment/"
            "UnifiedCheckoutCallBackHandlder")
UC_FRAME = "https://up.cybersource.com/uc/v1/assets/aHV75WwnMhzVd1nQvNwDa7fq"


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    """The fake page never sleeps, so a grace period is just a busy loop."""
    monkeypatch.setattr(unified, "OPTIONAL_FIELD_MS", 0)


def _spec():
    with open(UC_CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _card(card_type="visa"):
    number = {"visa": "4111111111111111",
              "mastercard": "5555555555554444"}[card_type]
    return Card(number=number, expiry_month="11", expiry_year="2029",
                cvn="123", card_type=card_type)


# --------------------------------------------------------------------------- #
# Doubles: a page whose UC iframes hold elements, keyed by selector            #
# --------------------------------------------------------------------------- #

class El:
    def __init__(self, log, name, visible=True, on_click=None, mask=False):
        self.log, self.name, self.visible = log, name, visible
        self.on_click, self.mask, self.value = on_click, mask, ""

    first = property(lambda self: self)

    def count(self):
        return 1

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def click(self, timeout=None):
        self.log.append(("click", self.name))
        if self.on_click:
            self.on_click()

    def fill(self, value, timeout=None):
        self.value = value

    def press_sequentially(self, value, delay=None, timeout=None):
        self.log.append(("type", self.name))
        self.value += value
        if self.mask and len(self.value) == 4 and "/" not in self.value:
            self.value = self.value[:2] + "/" + self.value[2:]

    def input_value(self, timeout=None):
        return self.value

    def select_option(self, value=None, timeout=None):
        self.log.append(("select", self.name))

    def get_attribute(self, name, timeout=None):
        return self.value

    def all_inner_texts(self):
        return [self.value] if self.value else []


class Missing:
    first = property(lambda self: self)

    def count(self):
        return 0


class Frame:
    def __init__(self, url, elements=None):
        self.url, self.elements = url, dict(elements or {})

    def locator(self, selector):
        return self.elements.get(selector, Missing())


class Page(Frame):
    def __init__(self, url=LANDING, frames=(), elements=None):
        super().__init__(url, elements)
        self.main_frame = self
        self.frames = [self] + list(frames)
        self.pauses = 0

    def wait_for_timeout(self, ms):
        self.pauses += 1


def _uc_page(spec, show_confirmation=True, number_present=True,
             networks=("VISA", "MASTERCARD")):
    log = []
    jwt = _jwt({"captureMandate": {"showConfirmationStep": show_confirmation,
                                   "billingType": "NONE"},
                "allowedCardNetworks": list(networks),
                "orderInformation": {"amountDetails": {
                    "totalAmount": "115.66", "currency": "AED"}}})
    fields = spec["card_fields"]
    mce = Frame(UC_FRAME + "/mce/mce.html")
    buttons = Frame(UC_FRAME + "/buttonlist/buttonlist.html")

    def open_form():
        if number_present:
            mce.elements[fields["number"][0]] = El(log, "number")
        mce.elements[fields["expiry"][0]] = El(log, "expiry", mask=True)
        mce.elements[fields["cvn"][0]] = El(log, "cvn")
        mce.elements[spec["card_submit"][0]] = El(
            log, "card_submit", on_click=show_review)

    def show_review():
        mce.elements[spec["review_submit"][0]] = El(log, "review_submit")

    buttons.elements[spec["launcher"][0]] = El(log, "launcher",
                                               on_click=open_form)
    context = El(log, "captureContext")
    context.value = jwt
    page = Page(frames=[buttons, mce],
                elements={spec["capture_context"]: context})
    return page, log


def _jwt(data):
    import base64

    claims = {"ctx": [{"data": data}]}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return "e30." + body.rstrip("=") + ".sig"


# --------------------------------------------------------------------------- #
# The real capture                                                             #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def capture_text():
    if not os.path.isfile(CAPTURE):
        pytest.skip("operator capture not present")
    with open(CAPTURE, encoding="utf-8") as fh:
        return fh.read()


def test_the_real_capture_context_says_the_review_click_is_the_charge(
        capture_text):
    jwt = re.search(r'id="captureContext" value="([^"]+)"',
                    capture_text).group(1)
    ctx = unified.decode_capture_context(jwt)

    assert ctx["show_confirmation"] is True
    assert ctx["billing_type"] == "NONE"
    assert "VISA" in ctx["networks"] and "MASTERCARD" in ctx["networks"]
    assert (ctx["amount"], ctx["currency"]) == ("115.66", "AED")


def test_observed_selectors_are_in_the_config(capture_text):
    spec = _spec()
    assert "data-testid=\"launcher-mce-and-ctp\"" in capture_text
    assert "data-testid=\"step-review-continue-btn\"" in capture_text
    assert "button[data-testid='launcher-mce-and-ctp']" in spec["launcher"]
    assert spec["review_submit"][0] == \
        "button[data-testid='step-review-continue-btn']"
    assert spec["submit"] == spec["review_submit"][0]
    assert spec["url_contains"].lower() in LANDING.lower()


def test_real_uc_frame_urls_are_never_mistaken_for_a_challenge(capture_text):
    spec = _spec()
    urls = re.findall(r'src="(https://up\.cybersource\.com[^"]+)"',
                      capture_text)
    assert urls
    for url in urls:
        assert not gateway.url_matches_marker(url, spec)


def test_route_hun_uses_unified_checkout():
    from src.booking import config as booking_config

    step = next(s for s in booking_config.steps_for("AE-HUN", "new")
                if s.get("type") == "payment")
    assert step["processor"] == "CYBERSOURCE_UC"


# --------------------------------------------------------------------------- #
# Marker matching                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://up.cybersource.com/uc/v1/assets/x/stepup/stepup.html",
    "https://centinelapi.cardinalcommerce.com/V1/Cruise/StepUp",
    "https://acs.mashreq.com/BrowserChallenge/",
    "https://secureacceptance.cybersource.com/payer_authentication/hybrid",
])
def test_token_mode_still_sees_real_challenges(url):
    assert gateway.url_matches_marker(url, _spec())


def test_token_mode_ignores_markers_buried_in_random_tokens():
    url = UC_FRAME + "/mce/mce.html?cr=xx3dsACSccaYY#%7B%22ctx%22%3Aacs3ds"
    assert not gateway.url_matches_marker(url, _spec())


def test_norway_substring_mode_is_unchanged():
    spec = {"challenge_markers": ["cca"]}
    assert gateway.url_matches_marker(
        "https://secureacceptance.cybersource.com/x?ccaAction=load", spec)


# --------------------------------------------------------------------------- #
# Outcome                                                                      #
# --------------------------------------------------------------------------- #

class UrlPage:
    def __init__(self, url):
        self.url = url


@pytest.mark.parametrize("status,outcome", [
    ("AUTHORIZED", gateway.OUTCOME_SUCCESS),
    ("DECLINED", gateway.OUTCOME_FAILED),
    ("INVALID_DATA", gateway.OUTCOME_FAILED),
    ("PENDING_AUTHENTICATION", gateway.OUTCOME_UNKNOWN),
])
def test_callback_status_is_read(status, outcome):
    url = (f"{CALLBACK}?status={status}&requestRef=1049978916"
           "&decodeJson=%7B%22card%22%3A%22411111XXXXXX1111%22%7D")
    result = gateway.read_outcome(UrlPage(url))

    assert result["outcome"] == outcome
    assert result["requestref"] == "1049978916"
    assert "decodeJson" not in result["url"] and "4111" not in result["url"]


def test_a_status_param_off_the_callback_is_not_an_outcome():
    page = UrlPage("https://visa.vfsglobal.com/are/en/hun/x?status=AUTHORIZED")
    assert gateway.read_outcome(page)["outcome"] == gateway.OUTCOME_UNKNOWN


def test_norway_return_url_still_reads_the_same():
    page = UrlPage("https://visa.vfsglobal.com/are/en/nor/confirmation"
                   "?PaymentStatus=False&RequestRefNo=1&TransactionId=2")
    result = gateway.read_outcome(page)
    assert result["outcome"] == gateway.OUTCOME_FAILED
    assert result["requestrefno"] == "1"


@pytest.mark.parametrize("url,back", [
    (LANDING, False),
    (f"{CALLBACK}?status=PENDING_AUTHENTICATION&requestRef=1", False),
    (f"{CALLBACK}?status=AUTHORIZED&requestRef=1", True),
    ("https://visa.vfsglobal.com/are/en/hun/confirmation", True),
])
def test_handback_needs_a_final_status_or_vfs(url, back):
    page = UrlPage(url)
    page.frames = []
    assert gateway._returned_to_merchant(page, _spec()) is back


# --------------------------------------------------------------------------- #
# prepare(): everything before the charging click                              #
# --------------------------------------------------------------------------- #

def test_prepare_opens_types_tokenizes_and_returns_the_review_click():
    spec = _spec()
    page, log = _uc_page(spec)
    captures = []

    click = unified.prepare(page, spec, _card(),
                            capture=lambda p, label: captures.append(
                                (label, list(log))))

    assert captures == [("uc_card_form_empty", [("click", "launcher")])]
    assert ("type", "number") in log and ("type", "cvn") in log
    assert log[-1] == ("click", "card_submit")
    assert ("click", "review_submit") not in log      # NOT charged yet
    click()
    assert log[-1] == ("click", "review_submit")


def test_without_a_confirmation_step_the_card_button_is_the_charge():
    spec = _spec()
    page, log = _uc_page(spec, show_confirmation=False)

    click = unified.prepare(page, spec, _card())

    assert ("click", "card_submit") not in log        # held for the journal
    click()
    assert log[-1] == ("click", "card_submit")


def test_a_missing_card_field_stops_before_anything_is_typed(monkeypatch):
    monkeypatch.setattr(unified, "FIELD_TIMEOUT_MS", 0)
    spec = _spec()
    page, log = _uc_page(spec, number_present=False)

    with pytest.raises(gateway.PaymentError):
        unified.prepare(page, spec, _card())
    assert not any(kind == "type" for kind, _ in log)


def test_a_card_network_the_checkout_refuses_is_caught_before_typing():
    spec = _spec()
    page, log = _uc_page(spec, networks=("MASTERCARD",))

    with pytest.raises(gateway.PaymentError, match="VISA"):
        unified.prepare(page, spec, _card("visa"))
    assert not any(kind == "type" for kind, _ in log)


def test_expiry_is_typed_as_digits_into_a_masked_field():
    spec = _spec()
    page, _ = _uc_page(spec)
    unified.prepare(page, spec, _card())

    expiry = page.frames[2].elements[spec["card_fields"]["expiry"][0]]
    assert expiry.value == "11/29"


def test_card_fields_are_never_looked_for_on_vfs_own_page():
    spec = _spec()
    trap = El([], "trap")
    page = Page(elements={spec["card_fields"]["number"][0]: trap})
    assert unified.find(page, spec, spec["card_fields"]["number"], 0) is None


# --------------------------------------------------------------------------- #
# submit_payment with the UC click                                             #
# --------------------------------------------------------------------------- #

def test_the_journal_is_written_before_the_uc_click_and_the_outcome_read():
    spec = _spec()
    order = []
    page = Page()

    def journal(row):
        order.append(row["event"])
        return "/j"

    def click():
        order.append("CLICK")
        page.url = f"{CALLBACK}?status=AUTHORIZED&requestRef=7"

    with pytest.raises(gateway.PaymentSubmitted):
        gateway.submit_payment(page, spec, journal=journal, click=click,
                               timeout_ms=1000, otp_timeout_ms=0)

    assert order == ["payment_submitting", "CLICK", "payment_result"]


def test_a_uc_decline_raises_payment_declined():
    spec = _spec()
    page = Page()

    def click():
        page.url = f"{CALLBACK}?status=DECLINED&requestRef=7"

    with pytest.raises(gateway.PaymentDeclined):
        gateway.submit_payment(page, spec, journal=lambda r: "/j",
                               click=click, timeout_ms=1000, otp_timeout_ms=0)


# --------------------------------------------------------------------------- #
# The runner picks whichever gateway VFS actually lands on                     #
# --------------------------------------------------------------------------- #

class ArrivingPage:
    def __init__(self, url):
        self.url = url

    def wait_for_url(self, predicate, timeout=None):
        if not predicate(self.url):
            raise TimeoutError("never arrived")

    def wait_for_load_state(self, *a, **k):
        pass


class Ctx:
    def __init__(self, processor):
        self.step = {"processor": processor}


def test_a_cybersource_route_never_switches_to_unified_checkout():
    """Unified Checkout is HUN's alone. A route configured for Secure
    Acceptance that lands on a UC page stops — it does not adapt."""
    from src.booking import runner
    from src.booking.errors import BookingStepError
    from src.payment import config as payment_config

    with pytest.raises(BookingStepError, match="Nothing was charged"):
        runner._arrive_at_gateway(ArrivingPage(LANDING), Ctx("CYBERSOURCE"),
                                  payment_config.load("CYBERSOURCE"))


def test_hun_landing_gets_the_unified_spec():
    from src.booking import runner
    from src.payment import config as payment_config

    spec = runner._arrive_at_gateway(ArrivingPage(LANDING),
                                     Ctx("CYBERSOURCE_UC"),
                                     payment_config.load("CYBERSOURCE_UC"))
    assert unified.is_unified(spec)


@pytest.mark.parametrize("route", ["AE-NOR", "AE-FRA", "AE-NLD", "AE-SWE"])
def test_every_other_paying_route_stays_on_secure_acceptance(route):
    from src.booking import config as booking_config

    step = next(s for s in booking_config.steps_for(route, "new")
                if s.get("type") == "payment")
    assert step.get("processor") == "CYBERSOURCE"
    assert not step.get("alternate_processors")


def test_no_gateway_at_all_is_a_clean_stop():
    from src.booking import runner
    from src.booking.errors import BookingStepError
    from src.payment import config as payment_config

    with pytest.raises(BookingStepError, match="Nothing was charged"):
        runner._arrive_at_gateway(
            ArrivingPage("https://visa.vfsglobal.com/are/en/hun/page-not-found"),
            Ctx("CYBERSOURCE_UC"), payment_config.load("CYBERSOURCE_UC"))
