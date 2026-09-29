"""Drive the payment gateway that VFS hands off to after "Pay Online".

    ══════════════════ THIS IS THE ONLY IRREVERSIBLE CODE HERE ══════════════════

Every other step in this system can be abandoned for free. A booking walk that
dies on page four costs nothing; the slot was never held. This module submits a
payment, and a submitted payment cannot be un-submitted by any code path.

That single fact shapes everything below:

    * `submit_payment` writes a journal row and FSYNCS IT BEFORE clicking.
      Not after — after is useless. If the process is killed between the click
      and the response, the row on disk is the only evidence the click happened.

    * The post-submit path is READ-ONLY. It reads the outcome; it never clicks
      anything, retries anything, or "helps".

    * `PaymentSubmitted` is raised, never returned, and is NOT retryable. A
      caller that catches it must not call this module again for the same
      booking. Retrying a payment whose response was lost is how a client gets
      charged twice.

    ═══════════════════════════ WHAT IT DOES NOT DO ═══════════════════════════

It does not encrypt the card. VFS's gateway RSA-encrypts the card fields in the
browser before submitting (`#jwk`, `RsaOaep.prepareFormForSubmission`), so this
module types into the VISIBLE inputs and lets the page's own JavaScript do what
it does for a human. Touching the encrypted payload directly would mean
reimplementing their crypto against an undocumented, changeable contract — and
getting it subtly wrong produces a failed payment on a live booking.

3-D SECURE. If the issuer challenges the payment, the gateway redirects to the
bank for an OTP that arrives on the cardholder's phone. A bot cannot complete
that and must not try. `submit_payment` detects the redirect and raises
`ChallengeRequired`, leaving the browser open on the bank's page for a human —
the same handover the booking walk uses at its commit boundary.

    ═════════════════════════ ONE COUNTRY, SO FAR ═════════════════════════

Built against Norway's gateway (CyberSource Secure Acceptance) and tested
against its captured DOM. VFS uses different processors per country and per
contract, so nothing here claims to generalise yet: selectors live in
`config/payment/<PROCESSOR>.json` exactly as booking steps live in
`config/booking/<ROUTE>.json`, and country two is a config file plus whatever
this module turns out to have assumed. Extracting a shared abstraction from one
example is how you get an abstraction that fits one example.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: How long to wait for the popup window after "Pay Online" is clicked.
POPUP_TIMEOUT_MS = 3000
#: How long to listen for a NEW WINDOW before concluding there is not going to
#: be one. Three seconds, not thirty.
#:
#: VFS ARE/NOR navigates the SAME TAB — confirmed twice by the operator, from
#: the address bar and from a live run. A popup is therefore not the expected
#: case here and waiting 30s for one to fail is 30s of dead time between
#: clicking Continue and reaching the gateway, on the one page where VFS warns
#: "do not close the browser" and a real page load is already in flight.
#:
#: The listener is kept rather than deleted because it costs almost nothing and
#: the popup shape may be real on another country — but a window that has not
#: opened within 3s of the click is not opening: window.open() during a click
#: handler is synchronous, so the event fires immediately or never.

#: How long to wait for the gateway to answer a submitted payment. Generous on
#: purpose: VFS's own disclaimer page warns that confirmation "can take up to 2
#: hours", and abandoning early is what leaves a paid booking unconfirmed.
SETTLE_TIMEOUT_MS = 180000


class PaymentError(Exception):
    """Payment failed BEFORE anything was submitted. Safe to abandon."""


class ChallengeRequired(PaymentError):
    """The issuer wants a 3-D Secure OTP. A human must finish in the browser.

    Pre-submit from this system's point of view — the card has not been charged
    — but the page must NOT be closed, because the booking depends on it.
    """


class PaymentSubmitted(Exception):
    """The payment was submitted and the outcome is UNKNOWN. NOT RETRYABLE.

    Raised rather than returned so it cannot be quietly folded into a result
    the caller treats as failure. The card may have been charged. Check the
    gateway or the bank before doing anything else; do not resubmit.
    """

    def __init__(self, message: str, journal_path: str = ""):
        super().__init__(message)
        self.journal_path = journal_path


class PaymentDeclined(PaymentSubmitted):
    """The gateway reported the payment FAILED. STILL NOT RETRYABLE.

        *** A DECLINE IS NOT PROOF THAT NOTHING WAS CHARGED. ***

    Subclasses PaymentSubmitted deliberately, and NOT PaymentError, because
    the click happened. VFS's own failure page says: "If funds have been
    deducted from your account, log in after 30 minutes and check if your
    appointment is confirmed" — i.e. the portal itself treats declined-and-
    charged as a real state.

    Anything that catches PaymentError to retry would, if this were a
    PaymentError, retry a payment that may already have taken money. Sorting it
    under PaymentSubmitted means every existing "do not retry" handler covers
    it for free, which is the whole reason that hierarchy exists.
    """


def attach_popup(context, trigger, timeout_ms: int = POPUP_TIMEOUT_MS):
    """Click `trigger` and return the page the gateway is on.

        *** SAME-TAB IS THE NORMAL PATH, NOT THE FALLBACK. ***

    On VFS ARE/NOR the browser NAVIGATES IN PLACE: Pay Online leads to a
    Payment Disclaimer at the same /review-pay URL, and its Continue takes the
    SAME TAB to secureacceptance.cybersource.com/checkout. Confirmed twice by
    the operator — from the address bar, and from a live run on 2026-09-29.
    Returning the existing page is the expected outcome here and is logged at
    debug, not as a surprise.

    The popup listener is kept because it costs ~3s at most and another
    country's portal may genuinely open a window. Where it does, the click and
    the wait MUST be arranged together — hence the trigger being passed in
    rather than clicked by the caller first: a popup can finish opening before
    a separately-issued wait starts listening, and the event is then missed
    forever. Playwright's expect_page exists for that race.

    Either way the caller must still verify it actually arrived: this returns a
    PAGE, not a promise that the page is the gateway. runner._step_payment
    waits for the processor's own URL before a single card digit is typed.
    """
    page = getattr(context, "pages", [None])[-1]
    try:
        with context.expect_page(timeout=timeout_ms) as popup_info:
            trigger()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        log.info(f"Payment gateway opened in a new window: {popup.url}")
        return popup
    except Exception as e:                                  # noqa: BLE001
        # EXPECTED on ARE/NOR. Debug, not info: reporting the normal path as a
        # notable event trains the operator to skim past it, and this line sat
        # directly above the one place a run can spend money.
        log.debug(f"No popup window within {timeout_ms}ms ({e}) — "
                  "the gateway navigates in the same tab, as expected.")
        return page


def _fill(page, selector: str, value: str, what: str,
          timeout_ms: int = 15000) -> None:
    """Type one value. NEVER logs the value — only which field it went into."""
    if not selector or value in (None, ""):
        return
    try:
        field = page.locator(selector).first
        field.wait_for(state="visible", timeout=timeout_ms)
        field.fill(str(value), timeout=timeout_ms)
        log.info(f"  filled {what}")
    except Exception as e:                                  # noqa: BLE001
        raise PaymentError(f"Could not fill {what}: {e}") from e


def _select(page, selector: str, value: str, what: str,
            timeout_ms: int = 15000) -> None:
    if not selector or value in (None, ""):
        return
    try:
        field = page.locator(selector).first
        field.wait_for(state="visible", timeout=timeout_ms)
        try:
            field.select_option(value=str(value), timeout=timeout_ms)
        except Exception:
            field.select_option(label=str(value), timeout=timeout_ms)
        log.info(f"  selected {what}")
    except Exception as e:                                  # noqa: BLE001
        raise PaymentError(f"Could not select {what}: {e}") from e


def _check(page, selector: str, what: str, timeout_ms: int = 15000) -> None:
    """Tick one radio/checkbox, addressed by a full selector.

    CyberSource renders CARD TYPE as a radio group, not a select:

        <input type="radio" name="card_type" id="card_type_001" value="001">
        <label for="card_type_001">Visa</label>

    so this is not a stylistic alternative to _select — it is what the real
    page requires. The config hypothesis (select[name=card_type]) matched
    nothing at all, which is the failure mode a capture exists to catch.
    """
    if not selector:
        return
    try:
        control = page.locator(selector).first
        control.wait_for(state="visible", timeout=timeout_ms)
        control.check(timeout=timeout_ms)
        log.info(f"  chose {what}")
    except Exception as e:                                  # noqa: BLE001
        raise PaymentError(f"Could not choose {what}: {e}") from e


def fill_billing(page, spec: Dict[str, Any], values: Dict[str, Any]) -> int:
    """Fill the gateway's billing-address fields from the client record.

    Separate from the card fields because it is separate data with a different
    lifetime: billing details come from the client file the booking already
    uses, while the card comes from the environment and is never persisted.
    """
    filled = 0
    for field in spec.get("billing_fields") or []:
        name = field.get("name", "?")
        value = values.get(field.get("value_key") or name, "")
        if not value:
            if field.get("required"):
                raise PaymentError(
                    f"The gateway needs a billing {name} and the client "
                    f"record has none. Add it before booking.")
            continue
        if field.get("widget") == "select":
            _select(page, field.get("selector"), value, f"billing {name}")
        else:
            _fill(page, field.get("selector"), value, f"billing {name}")
        filled += 1
    return filled


def fill_card(page, spec: Dict[str, Any], card) -> None:
    """Fill the card fields. The page's own JS encrypts them on submit.

    Order matters: the card TYPE first, because gateways re-render the CVN
    field when it changes (amex wants four digits, everyone else three) and a
    CVN typed before the type is chosen gets cleared.
    """
    fields = spec.get("card_fields") or {}

    # THE CARD TYPE IS A PROCESSOR CODE, NOT A NAME. CyberSource wants "001"
    # for Visa and "002" for Mastercard; the word "visa" selects nothing. The
    # mapping lives in the config because it belongs to the processor, not to
    # this code or to the card.
    codes = spec.get("card_type_codes") or {}
    type_value = codes.get(card.card_type, card.card_type)

    accepted = spec.get("accepted_card_types")
    if accepted and card.card_type not in accepted:
        # BEFORE anything is typed, and certainly before anything is
        # submitted: a card the gateway does not take cannot be made to work
        # by trying, and finding out mid-payment leaves a booking stranded.
        raise PaymentError(
            f"This gateway accepts {', '.join(sorted(accepted))} and the "
            f"configured card is a {card.card_type}. Set a different card in "
            "the environment.")

    if fields.get("type_widget") == "radio":
        template = fields.get("type") or ""
        _check(page, template.replace("{value}", str(type_value)), "card type")
    else:
        _select(page, fields.get("type"), type_value, "card type")

    _fill(page, fields.get("number"), card.number, "card number")

    # Expiry is sometimes two selects, sometimes one text field.
    if fields.get("expiry_month") or fields.get("expiry_year"):
        _select(page, fields.get("expiry_month"), card.expiry_month,
                "expiry month")
        _select(page, fields.get("expiry_year"), card.expiry_year,
                "expiry year")
    else:
        _fill(page, fields.get("expiry"), card.expiry_mm_yy, "expiry")

    _fill(page, fields.get("name"), card.name, "cardholder name")
    _fill(page, fields.get("cvn"), card.cvn, "CVN")

    log.info(f"  card details entered ({card.masked})")


def _looks_like_challenge(page, spec: Dict[str, Any]) -> bool:
    """Has the gateway redirected to a bank's 3-D Secure page?"""
    markers = spec.get("challenge_markers") or [
        "3dsecure", "3ds", "acs", "secure-auth", "verifiedbyvisa",
        "securecode", "challenge",
    ]
    try:
        url = (page.url or "").lower()
    except Exception:                                       # noqa: BLE001
        return False
    return any(marker in url for marker in markers)


#: VFS states the payment outcome IN THE RETURN URL, as a query parameter.
#: Observed live 2026-09-29:
#:
#:   .../nor/confirmation?PaymentStatus=False&RequestRefNo=1049157557
#:                       &TransactionId=7906718876126295804106&token=
#:
#: with the page reading "Sorry, your appointment may not have been completed
#: successfully... If funds have been deducted from your account, log in after
#: 30 minutes and check if your appointment is confirmed."
#:
#: Note what that says: DECLINED AND CHARGED ARE NOT MUTUALLY EXCLUSIVE here.
#: So this is recorded as the outcome VFS reported, never as "no money moved".
PAYMENT_STATUS_PARAM = "paymentstatus"
PAYMENT_REF_PARAMS = ("requestrefno", "transactionid")

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"


def read_outcome(page) -> Dict[str, Any]:
    """What the gateway's return URL says about the payment.

    READ-ONLY, and best-effort: this runs after an irreversible click, so it
    must never raise and never touch the page beyond reading its URL. An
    unreadable outcome is reported as "unknown", which is the honest answer and
    the one that keeps the journal row unanswered rather than falsely resolved.
    """
    from urllib.parse import parse_qs, urlparse

    out: Dict[str, Any] = {"outcome": OUTCOME_UNKNOWN, "url": ""}
    try:
        url = getattr(page, "url", "") or ""
        out["url"] = url
        query = parse_qs(urlparse(url).query)
    except Exception:                                       # noqa: BLE001
        return out

    lowered = {str(k).lower(): v for k, v in (query or {}).items()}

    raw = (lowered.get(PAYMENT_STATUS_PARAM) or [""])[0]
    token = str(raw).strip().lower()
    if token in ("true", "success", "1", "y", "yes"):
        out["outcome"] = OUTCOME_SUCCESS
    elif token in ("false", "failed", "failure", "0", "n", "no"):
        out["outcome"] = OUTCOME_FAILED
    out["payment_status"] = str(raw)

    # The references are what a human quotes to VFS or the bank. They are the
    # single most useful thing in the whole row and cost nothing to keep.
    for name in PAYMENT_REF_PARAMS:
        value = (lowered.get(name) or [""])[0]
        if value:
            out[name] = str(value)

    return out


def submit_payment(page, spec: Dict[str, Any], journal=None,
                   timeout_ms: int = SETTLE_TIMEOUT_MS) -> str:
    """Submit the payment. THE IRREVERSIBLE CALL.

    Raises PaymentSubmitted in every case where the click has happened —
    including success, because "success" here means "the gateway said so", and
    a caller must treat the booking as committed either way.

    `journal` is a callable taking one dict and returning a path. It MUST have
    fsync'd before returning: it is the only record that survives the process
    being killed mid-click.
    """
    submit = spec.get("submit") or "input[type=submit]"

    if _looks_like_challenge(page, spec):
        raise ChallengeRequired(
            "The gateway redirected to a 3-D Secure challenge before the "
            "payment was submitted. A human must complete the OTP in the open "
            "browser; this bot cannot and must not.")

    # ─────────── WRITE-AHEAD. BEFORE THE CLICK, NOT AFTER. ───────────
    # After the click this row cannot be written reliably: that is precisely
    # the window in which the process can die with the payment in flight.
    journal_path = ""
    if journal is not None:
        try:
            journal_path = journal({
                "event": "payment_submitting",
                "url": getattr(page, "url", ""),
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }) or ""
        except Exception as e:                              # noqa: BLE001
            # A journal that cannot be written means a payment whose outcome
            # could become unknowable. Refuse BEFORE clicking — this is the
            # last moment refusing is still free.
            raise PaymentError(
                f"Refusing to submit a payment that cannot be journalled: {e}"
            ) from e

    log.warning("SUBMITTING PAYMENT — this cannot be undone.")
    try:
        page.locator(submit).first.click(timeout=30000)
    except Exception as e:                                  # noqa: BLE001
        # The click itself failed, so nothing was submitted. This is the one
        # post-journal path that is still safe to abandon — but the journal row
        # already says "submitting", so say clearly that it did not happen.
        raise PaymentError(
            f"The payment was NOT submitted — the submit control could not be "
            f"clicked: {e}") from e

    # ─────────── EVERYTHING BELOW HERE IS READ-ONLY. ───────────
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as e:                                  # noqa: BLE001
        raise PaymentSubmitted(
            "The payment was submitted but the gateway did not respond in "
            f"{timeout_ms // 1000}s. DO NOT RESUBMIT. Check the gateway or the "
            f"bank for the outcome. ({e})", journal_path) from e

    if _looks_like_challenge(page, spec):
        _record_outcome(journal, {"outcome": OUTCOME_UNKNOWN,
                                  "detail": "3-D Secure challenge raised",
                                  "url": getattr(page, "url", "")})
        raise PaymentSubmitted(
            "The payment was submitted and the issuer raised a 3-D Secure "
            "challenge. A human must complete the OTP in the open browser. "
            "DO NOT RESUBMIT.", journal_path)

    # ── CLOSE THE JOURNAL ROW. ──────────────────────────────────────────────
    # Without this every payment ever made stays in `unanswered()` forever:
    # the write-ahead row says "submitting" and nothing was ever written after
    # it. That turns the one report an operator checks after a crash — "which
    # payments have no recorded outcome" — into a list of every payment, which
    # is the same as having no report at all.
    result = read_outcome(page)
    _record_outcome(journal, result)

    if result.get("outcome") == OUTCOME_FAILED:
        raise PaymentDeclined(
            f"The gateway reported the payment FAILED "
            f"(PaymentStatus={result.get('payment_status')!r}). "
            f"Reference {result.get('requestrefno') or '?'}, transaction "
            f"{result.get('transactionid') or '?'}. "
            "*** A DECLINE IS NOT PROOF THAT NOTHING WAS CHARGED *** — VFS's "
            "own page says to log in after 30 minutes and check whether the "
            "appointment was confirmed before paying again. DO NOT RESUBMIT "
            "until that check is done.", journal_path)

    raise PaymentSubmitted(
        f"The payment was submitted. Outcome page: "
        f"{getattr(page, 'url', 'unknown')}. Confirm it before treating the "
        "booking as paid, and DO NOT RESUBMIT.", journal_path)


def _record_outcome(journal, result: Dict[str, Any]) -> None:
    """Append the outcome row. Best-effort AND LOUD IF IT FAILS.

    Unlike the write-ahead row, a failure here cannot be met by refusing to
    act — the click has already happened. But it must be shouted about,
    because the consequence is a payment that stays "unanswered" forever and
    an operator who cannot tell it from a genuine crash.
    """
    if journal is None:
        return
    try:
        row = {"event": "payment_result"}
        row.update(result or {})
        journal(row)
    except Exception as e:                                  # noqa: BLE001
        log.error(
            f"PAYMENT OUTCOME COULD NOT BE JOURNALLED: {e}. The payment WAS "
            f"submitted and its result was {result!r} — record this by hand "
            "in the payment journal under state/, or it will show as unanswered "
            "forever.")
