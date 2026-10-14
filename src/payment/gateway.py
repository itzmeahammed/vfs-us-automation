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

3-D SECURE. If the issuer challenges the payment, the bank asks for an OTP that
arrives on the cardholder's phone. THE BOT NEVER TYPES IT. What it does depends
entirely on which side of the click the challenge appears on:

    BEFORE the click  `ChallengeRequired` — nothing was submitted, the browser
                      is left open for a human, abandoning is still free.

    AFTER the click   it WAITS (`_await_challenge`). The payment is already in
                      flight, so giving up here would discard the answer: the
                      human types the OTP, the gateway returns to VFS with
                      PaymentStatus in the URL, and that is the outcome the
                      journal row exists to record. Every screen the bank shows
                      is captured on the way past, because none of them can be
                      revisited once the code is spent.

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
import re
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


#: Fallback markers for a processor config that declares none. The config is
#: the authority — this is what a NEW country gets before anyone has seen its
#: challenge, so it leans on the names that are not processor-specific: the
#: protocol ones, and Cardinal Commerce, whose step-up iframe is what most
#: CyberSource-family gateways actually serve.
#:
#: Every entry must be specific enough not to match a GATEWAY's own hostname —
#: a marker that does makes the checkout page look like a challenge and the
#: payment is refused before the card is ever submitted. "secure" alone would
#: match "secureacceptance"; that is why the list reads oddly precise.
_DEFAULT_CHALLENGE_MARKERS = (
    "3dsecure", "3ds", "acs", "secure-auth", "verifiedbyvisa", "securecode",
    "challenge", "cardinalcommerce", "centinel", "step-up", "stepup",
)


def _challenge_markers(spec: Dict[str, Any]) -> List[str]:
    return spec.get("challenge_markers") or list(_DEFAULT_CHALLENGE_MARKERS)


def _looks_like_challenge(page, spec: Dict[str, Any]) -> bool:
    """Has the gateway redirected to a bank's 3-D Secure page?"""
    markers = _challenge_markers(spec)
    try:
        url = (page.url or "").lower()
    except Exception:                                       # noqa: BLE001
        return False
    return any(marker in url for marker in markers)


def _frame_urls(page) -> List[str]:
    """The URLs of the page's child frames. [] when they cannot be read."""
    try:
        frames = list(getattr(page, "frames", None) or [])
    except Exception:                                       # noqa: BLE001
        return []
    main = getattr(page, "main_frame", None)
    urls = []
    for frame in frames:
        if main is not None and frame is main:
            continue
        try:
            url = frame.url or ""
        except Exception:                                   # noqa: BLE001
            continue
        if url and url != "about:blank":
            urls.append(url)
    return urls


def _challenge_showing(page, spec: Dict[str, Any]) -> bool:
    """A 3-D Secure screen, as a redirect OR an iframe on the gateway page.

    The redirect is what _looks_like_challenge checks. CyberSource's payer
    authentication more often does neither: it leaves the address bar on the
    gateway and renders the bank's OTP page in an iframe (Cardinal step-up), so
    the frame URLs have to be read too or the challenge is invisible.
    """
    if _looks_like_challenge(page, spec):
        return True
    markers = _challenge_markers(spec)
    return any(m in url.lower() for url in _frame_urls(page) for m in markers)


def _returned_to_merchant(page, spec: Dict[str, Any]) -> bool:
    """Has the gateway handed the browser back to VFS with an outcome?"""
    try:
        url = (page.url or "").lower()
    except Exception:                                       # noqa: BLE001
        return False
    if PAYMENT_STATUS_PARAM in url:
        return True
    gateway_host = (spec.get("url_contains") or "").lower()
    if not gateway_host or gateway_host in url:
        return False
    return not _looks_like_challenge(page, spec)


def _screen_signature(page) -> tuple:
    try:
        url = page.url or ""
    except Exception:                                       # noqa: BLE001
        url = ""
    return (url, tuple(_frame_urls(page)))


def _pause(page, ms: int) -> None:
    """Sleep without starving Playwright's event loop."""
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        try:
            waiter(ms)
            return
        except Exception:                                   # noqa: BLE001
            pass
    time.sleep(ms / 1000.0)


#: How long a human gets to type the OTP. The bank's own page usually expires
#: the code in 3-5 minutes; ten leaves room for a resend.
OTP_TIMEOUT_MS = 600000


def _human_wait(ms: int) -> str:
    """"10 min" / "45s". Integer-dividing by 60000 printed "0 min" for every
    timeout under a minute — which is every test, and is the line an operator
    reads to decide whether they have time to fetch their phone."""
    if ms >= 60000:
        return f"{ms // 60000} min"
    return f"{max(0, ms // 1000)}s"


def _await_challenge(page, spec: Dict[str, Any], journal, capture,
                     journal_path: str, timeout_ms: int) -> None:
    """Hold the open browser while a HUMAN completes the 3-D Secure OTP.

    The bot never types the OTP — it arrives on the cardholder's phone. What it
    does is wait, keep every screen the bank shows (they cannot be revisited
    once the code is spent), and notice when the gateway hands back to VFS so
    the outcome can be read and journalled rather than left "unknown".

    Returns when the browser is back on VFS. Raises PaymentSubmitted when the
    human never finishes inside the window.
    """
    _journal_quietly(journal, {
        "event": "payment_challenge",
        "detail": "3-D Secure challenge raised; waiting for the OTP",
        "url": getattr(page, "url", ""),
        "frames": _frame_urls(page),
    })
    log.warning("=" * 70)
    log.warning("  3-D SECURE: ENTER THE OTP IN THE OPEN BROWSER.")
    log.warning(f"  Waiting up to {_human_wait(timeout_ms)}. Do not close it.")
    log.warning("=" * 70)

    seen = set()
    shots = 0
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        signature = _screen_signature(page)
        if signature not in seen and capture is not None and shots < 25:
            seen.add(signature)
            shots += 1
            try:
                capture(page, f"otp_{shots:02d}")
            except Exception as e:                          # noqa: BLE001
                log.warning(f"  could not capture the OTP screen: {e}")
        if _returned_to_merchant(page, spec):
            log.info(f"  gateway handed back to VFS: {page.url}")
            return
        if time.monotonic() >= deadline:
            raise PaymentSubmitted(
                "The payment was submitted and the 3-D Secure challenge was "
                f"not completed within {_human_wait(timeout_ms)}. The browser "
                "is still open on it. DO NOT RESUBMIT — check the bank and "
                "the VFS account before doing anything else.", journal_path)
        _pause(page, 1000)


def _journal_quietly(journal, row: Dict[str, Any]) -> None:
    if journal is None:
        return
    try:
        journal(row)
    except Exception as e:                                  # noqa: BLE001
        log.error(f"could not journal {row.get('event')!r}: {e}")


_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_INPUT_VALUE = re.compile(
    r"(\svalue=)(\"[^\"]*\"|'[^']*')", re.IGNORECASE)

#: Input types whose `value` is a LABEL, not data — the text printed on the
#: button. Blanking those costs real information and protects nothing: it is
#: how a control gets addressed when its selectors are ambiguous, which on
#: this portal is the normal case (VFS's own Cancel and Continue carry
#: byte-identical class attributes, so the booking config addresses them by
#: accessible name). A captured OTP page whose submit button reads value=""
#: cannot be turned into a selector by whoever reads it next.
_LABEL_INPUT_TYPES = ("submit", "button", "reset", "image")
_INPUT_TYPE = re.compile(
    r"""\stype=["']?([a-z]+)""", re.IGNORECASE)


def _redact_input(match: "re.Match") -> str:
    tag = match.group(0)
    kind = _INPUT_TYPE.search(tag)
    if kind and kind.group(1).lower() in _LABEL_INPUT_TYPES:
        return tag
    return _INPUT_VALUE.sub(r'\1""', tag)


def redact_html(html: str, scrub: List[str] = ()) -> str:
    """Blank every <input value> and remove any known secret from markup.

    The OTP pages sit on the same tab as the filled card form, and whatever
    the human types into the OTP box is in the DOM too. Neither belongs in a
    file under runs/.

    Button LABELS survive — see _LABEL_INPUT_TYPES. Everything else is blanked
    whether or not it looks sensitive, because the whole point is not having to
    decide: a hidden field holding an authentication token and a text field
    holding an OTP are indistinguishable from the markup, and this runs on
    pages nobody has seen before.
    """
    html = _INPUT_TAG.sub(_redact_input, html or "")
    for secret in scrub or ():
        if secret and len(secret) >= 3:
            html = html.replace(secret, "[redacted]")
    return html


def capture_screen(page, directory: str, label: str,
                   scrub: List[str] = ()) -> List[str]:
    """Screenshot + redacted DOM of the page AND every frame in it.

    Frames matter here more than anywhere: the bank's OTP form is usually an
    iframe, and page.content() of the parent shows only an empty <iframe>.
    Best-effort throughout; returns the paths written.
    """
    import io
    import os

    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    written: List[str] = []

    shot = os.path.join(directory, f"{stamp}_{label}.png")
    try:
        page.screenshot(path=shot, full_page=True, timeout=5000)
        written.append(shot)
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"  could not screenshot '{label}': {e}")

    frames = []
    try:
        frames = list(page.frames)
    except Exception:                                       # noqa: BLE001
        pass
    for index, frame in enumerate(frames or [page]):
        try:
            url = frame.url or ""
            if url == "about:blank":
                continue
            html = redact_html(frame.content(), scrub)
        except Exception as e:                              # noqa: BLE001
            log.debug(f"  frame {index} unreadable: {e}")
            continue
        name = f"{stamp}_{label}" + ("" if index == 0 else f"_frame{index}")
        path = os.path.join(directory, f"{name}.html")
        try:
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(f"<!-- captured from: {url} -->\n")
                fh.write(html)
            written.append(path)
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  could not write {path}: {e}")

    log.info(f"  captured '{label}' ({len(written)} files) -> {directory}")
    return written


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


def _otp_timeout(spec: Dict[str, Any]) -> int:
    """How long a human gets to type the OTP, from the processor's config."""
    seconds = spec.get("otp_timeout_seconds")
    try:
        ms = int(float(seconds) * 1000)
    except (TypeError, ValueError):
        return OTP_TIMEOUT_MS
    return ms if ms > 0 else OTP_TIMEOUT_MS


def submit_payment(page, spec: Dict[str, Any], journal=None,
                   timeout_ms: int = SETTLE_TIMEOUT_MS, capture=None,
                   otp_timeout_ms: Optional[int] = None) -> str:
    """Submit the payment. THE IRREVERSIBLE CALL.

    Raises PaymentSubmitted in every case where the click has happened —
    including success, because "success" here means "the gateway said so", and
    a caller must treat the booking as committed either way.

    `journal` is a callable taking one dict and returning a path. It MUST have
    fsync'd before returning: it is the only record that survives the process
    being killed mid-click.

    `capture` is an optional callable (page, label) -> anything, used to keep
    the screens the gateway and the bank show after the click. It exists
    because those screens CANNOT BE REVISITED: the OTP page is dead the moment
    the code is spent, and the slot is gone, so a run that does not keep them
    has lost the only evidence of what happened. Best-effort — a capture that
    fails never changes the outcome of a payment.

    `otp_timeout_ms` is how long a HUMAN gets at the 3-D Secure screen,
    defaulting to the processor's `otp_timeout_seconds`. Pass 0 in tests, or
    they wait ten real minutes.
    """
    submit = spec.get("submit") or "input[type=submit]"
    if otp_timeout_ms is None:
        otp_timeout_ms = _otp_timeout(spec)

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
    #
    # NETWORKIDLE IS NOT A RELIABLE SETTLE SIGNAL ON A 3-D SECURE CHALLENGE.
    # Cardinal's step-up iframe polls the ACS while it waits for the OTP, so the
    # page may never go idle for as long as the human is typing — a timeout here
    # means "still busy", which on this path is the NORMAL state, not a lost
    # response. Raising on it would abandon a payment that is mid-challenge and
    # report an unknown outcome that the browser is about to show us.
    #
    # So the timeout is recorded, the screen is looked at, and only a timeout
    # with NO challenge on screen is treated as a gateway that never answered.
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as e:                                  # noqa: BLE001
        if not _challenge_showing(page, spec):
            raise PaymentSubmitted(
                "The payment was submitted but the gateway did not respond in "
                f"{timeout_ms // 1000}s. DO NOT RESUBMIT. Check the gateway or "
                f"the bank for the outcome. ({e})", journal_path) from e
        log.info(f"  gateway still busy after {timeout_ms // 1000}s, and a "
                 "3-D Secure challenge is on screen — that is the challenge "
                 "polling, not a lost response.")

    # THE FIRST SCREEN AFTER THE IRREVERSIBLE CLICK. Kept unconditionally and
    # before anything is decided about it: whatever the gateway did with the
    # payment, this is the page that says so, and it is not reachable again.
    if capture is not None:
        try:
            capture(page, "submitted")
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  could not capture the post-submit screen: {e}")

    if _challenge_showing(page, spec):
        # HAND OVER, DO NOT GIVE UP. The old code journalled "unknown" and
        # raised here, which threw away the actual outcome: the OTP is typed
        # seconds later, the gateway returns to VFS with PaymentStatus in the
        # URL, and that is the answer this journal row exists to carry. The
        # bot still never types the OTP — it waits for the human and then
        # READS.
        _await_challenge(page, spec, journal, capture, journal_path,
                         otp_timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception as e:                              # noqa: BLE001
            # Back on VFS and readable already — the outcome is in the URL,
            # which does not need the page to have finished loading.
            log.debug(f"Return page load state not confirmed: {e}")
        if capture is not None:
            try:
                capture(page, "challenge_returned")
            except Exception as e:                          # noqa: BLE001
                log.warning(f"  could not capture the return screen: {e}")

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
