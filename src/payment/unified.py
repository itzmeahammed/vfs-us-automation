"""CyberSource UNIFIED CHECKOUT — the gateway AE/HUN lands on after Pay Online.

    ═══════════════ NOT THE SAME GATEWAY AS NORWAY'S, AND WHY IT MATTERS ═══════════════

Norway's Pay Online goes to secureacceptance.cybersource.com/checkout: one page,
plain inputs, a Pay button. Hungary's goes to a VFS-hosted page
(online.vfsglobal.com/PG-Component/Payment/CyberSourceNew) that loads
CyberSource's Unified Checkout JavaScript, which draws everything inside
cross-origin iframes from up.cybersource.com:

    #buttonPaymentListContainer iframe   the payment-method buttons, among them
                                         "Checkout with card"
                                         (button[data-testid=launcher-mce-and-ctp])
    #embeddedPaymentContainer iframe     "mce" — manual card entry, then the
                                         "review and confirm" step
                                         (button[data-testid=step-review-continue-btn])

The page script then calls up.complete(token) — THAT is where the card is
charged — and sends the browser to VFS's callback handler with ?status=...
(AUTHORIZED / DECLINED / PENDING_AUTHENTICATION / ...). Captured by the
operator 2026-10-22; see docs/hungary_payment.txt/payment_selector.txt.

    ═══════════════ WHICH CLICK SPENDS THE MONEY ═══════════════

The capture context — a JWT the page carries in #captureContext — says whether
a confirmation step follows the card form (`showConfirmationStep`). Hungary's
says true, so the card form's own button only tokenizes and the REVIEW
continue is the irreversible click. This module reads that flag off the page
rather than assuming it: if it is false, or unreadable, the card form's button
is treated as the irreversible click instead and goes through
gateway.submit_payment's write-ahead journal. Guessing "safe" about a click
that charges a card is the one guess this code may not make.

    ═══════════════ WHAT IS STILL A HYPOTHESIS ═══════════════

The card-entry fields themselves. The operator's capture ends at the review
step and does not include the mce form's inner DOM, so `card_fields` in
config/payment/CYBERSOURCE_UC.json is a ranked list of candidates (CyberSource
Microform's own names first, standard autocomplete attributes next). Two
things keep that honest:

    * the EMPTY form's DOM is captured before a single digit is typed, so the
      first live run leaves the real selectors on disk whether it pays or not;
    * nothing that cannot be found is guessed at — a missing field stops the
      run BEFORE the irreversible click, which is still a free stop.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FLOW = "unified_checkout"

#: Hosts whose frames belong to Unified Checkout. Card fields are ONLY looked
#: for inside these, never on VFS's own page: a stray `input[type=tel]` on the
#: parent must not receive a card number.
DEFAULT_FRAME_HOSTS = ("up.cybersource.com", "flex.cybersource.com")

#: CyberSource's network names, by card.py's card_type.
NETWORKS = {"visa": "VISA", "mastercard": "MASTERCARD", "amex": "AMEX"}

LAUNCH_TIMEOUT_MS = 45000
FIELD_TIMEOUT_MS = 20000
REVIEW_TIMEOUT_MS = 30000
#: Grace for fields a form MAY have (expiry selects, name). The form is already
#: drawn by then — the number field was found — so this is short.
OPTIONAL_FIELD_MS = 1500


def is_unified(spec: Dict[str, Any]) -> bool:
    return (spec or {}).get("flow") == FLOW


def _gateway():
    from src.payment import gateway

    return gateway


def _error(message: str):
    return _gateway().PaymentError(message)


# --------------------------------------------------------------------------- #
# The capture context                                                          #
# --------------------------------------------------------------------------- #

def decode_capture_context(jwt: str) -> Dict[str, Any]:
    """The settings a Unified Checkout page was started with. {} if unreadable.

    The signature is NOT verified and does not need to be: this is read only
    to decide how cautious to be, never to trust anything. Every field is
    optional in the result.
    """
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        data = (claims.get("ctx") or [{}])[0].get("data") or {}
    except Exception:                                       # noqa: BLE001
        return {}

    mandate = data.get("captureMandate") or {}
    amount = (data.get("orderInformation") or {}).get("amountDetails") or {}
    out: Dict[str, Any] = {
        "networks": [str(n).upper() for n in data.get("allowedCardNetworks") or []],
        "payment_types": list(data.get("allowedPaymentTypes") or []),
        "amount": amount.get("totalAmount"),
        "currency": amount.get("currency"),
        "billing_type": mandate.get("billingType"),
    }
    if "showConfirmationStep" in mandate:
        out["show_confirmation"] = bool(mandate["showConfirmationStep"])
    return out


def read_capture_context(page, spec: Dict[str, Any]) -> Dict[str, Any]:
    selector = spec.get("capture_context") or "input#captureContext"
    try:
        value = page.locator(selector).first.get_attribute("value",
                                                           timeout=5000)
    except Exception as e:                                  # noqa: BLE001
        log.debug(f"Unified Checkout capture context unreadable: {e}")
        return {}
    return decode_capture_context(value or "")


# --------------------------------------------------------------------------- #
# Finding things across frames                                                 #
# --------------------------------------------------------------------------- #

def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def _uc_frames(page, spec: Dict[str, Any]) -> List[Any]:
    hosts = [h.lower() for h in _as_list(spec.get("frame_hosts"))
             or list(DEFAULT_FRAME_HOSTS)]
    out = []
    try:
        frames = list(getattr(page, "frames", None) or [])
    except Exception:                                       # noqa: BLE001
        return out
    for frame in frames:
        try:
            host = (urlparse(frame.url or "").hostname or "").lower()
        except Exception:                                   # noqa: BLE001
            continue
        if host and any(host == h or host.endswith("." + h) for h in hosts):
            out.append(frame)
    return out


def _visible(scope, selector: str):
    try:
        locator = scope.locator(selector)
        if locator.count() < 1:
            return None
        first = locator.first
        if first.is_visible() and first.is_enabled():
            return first
    except Exception:                                       # noqa: BLE001
        pass
    return None


def find(page, spec: Dict[str, Any], selectors: List[str],
         timeout_ms: int, include_page: bool = False) -> Optional[Tuple[Any, Any, str]]:
    """(scope, locator, selector) for the first visible match, polled until
    `timeout_ms`. Frames are re-read every pass: Unified Checkout swaps them
    as it moves between screens."""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    while True:
        scopes = _uc_frames(page, spec) + ([page] if include_page else [])
        for scope in scopes:
            for selector in selectors:
                hit = _visible(scope, selector)
                if hit is not None:
                    return scope, hit, selector
        if time.monotonic() >= deadline:
            return None
        _gateway()._pause(page, 500)


# --------------------------------------------------------------------------- #
# Card form                                                                    #
# --------------------------------------------------------------------------- #

def _fields(spec: Dict[str, Any]) -> Dict[str, Any]:
    return spec.get("card_fields") or {}


def open_card_form(page, spec: Dict[str, Any],
                   timeout_ms: int = LAUNCH_TIMEOUT_MS) -> None:
    """Click "Checkout with card" — unless the card form is already showing."""
    number = _as_list(_fields(spec).get("number"))
    if find(page, spec, number, 0):
        log.info("  card form already open")
        return

    hit = find(page, spec, _as_list(spec.get("launcher")), timeout_ms,
               include_page=True)
    if hit is None:
        raise _error(
            "Unified Checkout never offered 'Checkout with card' within "
            f"{timeout_ms // 1000}s. Nothing was typed or charged.")
    _, button, selector = hit
    button.click(timeout=15000)
    log.info(f"  clicked 'Checkout with card' ({selector})")

    if find(page, spec, number, FIELD_TIMEOUT_MS) is None:
        raise _error(
            "Clicked 'Checkout with card' but no card-number field appeared. "
            "Nothing was typed or charged. The empty form's DOM is under "
            "runs/<route>/payment/ — put the real selector in "
            "config/payment/CYBERSOURCE_UC.json card_fields.number.")


def _type(locator, value: str, delay_ms: int) -> None:
    """Type like a person. Microform formats as it goes ("4111 1111 ..."),
    and fill() on a formatted field can bypass its listeners."""
    locator.click(timeout=10000)
    try:
        locator.fill("", timeout=5000)
    except Exception:                                       # noqa: BLE001
        pass
    locator.press_sequentially(str(value), delay=delay_ms, timeout=30000)


def _digits(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _field(page, spec, key: str, what: str, required: bool = True,
           timeout_ms: int = FIELD_TIMEOUT_MS):
    selectors = _as_list(_fields(spec).get(key))
    if not selectors:
        return None
    hit = find(page, spec, selectors,
               timeout_ms if required else OPTIONAL_FIELD_MS)
    if hit is None and required:
        raise _error(
            f"Unified Checkout: no {what} field matched "
            f"card_fields.{key} in config/payment/CYBERSOURCE_UC.json. "
            "Nothing was submitted or charged.")
    return hit[1] if hit else None


def fill_billing(page, spec: Dict[str, Any],
                 values: Optional[Dict[str, Any]] = None) -> int:
    """Fill the billing fields the card form itself asks for. Count filled.

    Unified Checkout's card form carries its own 'First name' / 'Last name'
    (billTo.firstName / billTo.lastName) next to the card fields. The
    2026-10-23 AE-HUN run left both empty and an operator typed them by hand,
    because card_fields had only a 'cardholderName' candidate, which this form
    does not have.

    A field that is NOT on the form is skipped — Unified Checkout renders a
    different set per merchant profile. A field that IS on the form with no
    value refuses here, which is still free: nothing has been charged.
    """
    values = values or {}
    filled = 0
    for field in spec.get("billing_fields") or []:
        name = field.get("name", "?")
        selectors = _as_list(field.get("selectors"))
        if not selectors:
            continue
        hit = find(page, spec, selectors, OPTIONAL_FIELD_MS)
        if hit is None:
            log.debug(f"  billing {name} is not on this card form — skipped")
            continue
        value = str(values.get(field.get("value_key") or name, "") or "").strip()
        if not value:
            if field.get("required"):
                raise _error(
                    f"The card form asks for a billing {name} and the client "
                    "record has none. Nothing was typed or charged. Add it "
                    "to the booking request and run again.")
            continue
        _type(hit[1], value, int(spec.get("typing_delay_ms") or 60))
        filled += 1
        log.info(f"  filled billing {name}")
    return filled


def fill_card(page, spec: Dict[str, Any], card,
              context: Optional[Dict[str, Any]] = None) -> None:
    """Number, expiry, CVN (and name, where the form asks). Never logs a value."""
    context = context or {}
    networks = context.get("networks") or [
        str(n).upper() for n in _as_list(spec.get("accepted_networks"))]
    network = NETWORKS.get(card.card_type, str(card.card_type).upper())
    if networks and network not in networks:
        raise _error(
            f"This checkout accepts {', '.join(networks)} and the configured "
            f"card is {network}. Nothing was typed or charged.")

    delay = int(spec.get("typing_delay_ms") or 60)

    number = _field(page, spec, "number", "card number")
    _type(number, card.number, delay)
    try:
        typed = _digits(number.input_value(timeout=3000))
    except Exception:                                       # noqa: BLE001
        typed = card.number
    if typed != card.number:
        raise _error(
            "The card number did not land as typed (the field shows "
            f"{len(typed)} digits, expected {len(card.number)}). Nothing was "
            "submitted or charged.")
    log.info("  filled card number")

    month = _field(page, spec, "expiry_month", "expiry month", required=False)
    year = _field(page, spec, "expiry_year", "expiry year", required=False)
    if month is not None and year is not None:
        month.select_option(value=card.expiry_month, timeout=10000)
        try:
            year.select_option(value=card.expiry_year, timeout=10000)
        except Exception:                                   # noqa: BLE001
            year.select_option(value=card.expiry_year[-2:], timeout=10000)
        log.info("  selected expiry")
    else:
        expiry = _field(page, spec, "expiry", "expiry date")
        _type(expiry, card.expiry_month + card.expiry_year[-2:], delay)
        # A masked field inserts the slash itself; a plain one needs it typed.
        try:
            shown = expiry.input_value(timeout=3000)
        except Exception:                                   # noqa: BLE001
            shown = "/"
        if "/" not in shown:
            _type(expiry, card.expiry_mm_yy, delay)
        log.info("  filled expiry")

    cvn = _field(page, spec, "cvn", "security code")
    _type(cvn, card.cvn, delay)
    log.info("  filled security code")

    if card.name:
        name = _field(page, spec, "name", "cardholder name", required=False)
        if name is not None:
            _type(name, card.name, delay)
            log.info("  filled cardholder name")

    log.info(f"  card details entered ({card.masked})")


def _click_first(page, spec, key: str, what: str, timeout_ms: int) -> None:
    hit = find(page, spec, _as_list(spec.get(key)), timeout_ms)
    if hit is None:
        raise _error(f"Unified Checkout: could not find the {what} "
                     f"({key} in config/payment/CYBERSOURCE_UC.json).")
    hit[1].click(timeout=15000)
    log.info(f"  clicked {what} ({hit[2]})")


def _form_errors(page, spec) -> str:
    texts = []
    for frame in _uc_frames(page, spec):
        for selector in _as_list(spec.get("error_selectors")):
            try:
                for text in frame.locator(selector).all_inner_texts():
                    if text.strip():
                        texts.append(" ".join(text.split()))
            except Exception:                               # noqa: BLE001
                continue
    return "; ".join(dict.fromkeys(texts))[:300]


# --------------------------------------------------------------------------- #
# The whole sequence                                                           #
# --------------------------------------------------------------------------- #

def prepare(page, spec: Dict[str, Any], card,
            capture: Optional[Callable[[Any, str], Any]] = None,
            values: Optional[Dict[str, Any]] = None
            ) -> Callable[[], None]:
    """Everything BEFORE the irreversible click. Returns that click.

    The returned callable is handed to gateway.submit_payment, which journals
    and fsyncs BEFORE calling it — the same discipline as Norway's Pay button.
    Every failure in here raises PaymentError: nothing has been charged.
    """
    context = read_capture_context(page, spec)
    if context:
        log.info(
            f"  Unified Checkout: {context.get('amount')} "
            f"{context.get('currency')}, networks "
            f"{', '.join(context.get('networks') or []) or '?'}, "
            f"confirmation step {context.get('show_confirmation', '?')}")
    billing = context.get("billing_type")
    if billing and str(billing).upper() != "NONE":
        raise _error(
            f"This Unified Checkout asks for billing details ({billing}), "
            "which this flow does not fill yet. Nothing was typed or charged.")

    open_card_form(page, spec)

    # THE EMPTY FORM, BEFORE ANY DIGIT IS TYPED. Safe to keep as DOM for
    # exactly that reason, and the record that turns card_fields from
    # candidates into observed selectors.
    if capture is not None:
        try:
            capture(page, "uc_card_form_empty")
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  could not capture the empty card form: {e}")

    fill_card(page, spec, card, context)
    # AFTER the card: the form asks for the billing name below the card
    # fields, and filling in page order keeps any re-render the card inputs
    # trigger from clearing what was typed above them.
    fill_billing(page, spec, values)

    def click_card_continue() -> None:
        _click_first(page, spec, "card_submit", "card form Continue", 15000)

    if context.get("show_confirmation") is not True:
        # No confirmation step, or unknown: the card form's own button may be
        # the one that charges. Hand it to submit_payment's write-ahead path.
        log.warning("  no confirmation step is declared — the card form's "
                    "button is treated as the payment click.")
        return click_card_continue

    click_card_continue()
    if find(page, spec, _as_list(spec.get("review_submit")),
            REVIEW_TIMEOUT_MS) is None:
        problem = _form_errors(page, spec)
        raise _error(
            "The card form did not move on to the review step"
            + (f" — it says: {problem}" if problem else "")
            + ". Nothing was charged: the review Continue was never clicked.")
    log.info("  review step reached — the next click charges the card")

    def click_review() -> None:
        _click_first(page, spec, "review_submit", "review Continue (PAY)",
                     15000)

    return click_review
