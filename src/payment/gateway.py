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


#: Ceiling on how many screens one challenge may capture. A bank that bounces
#: between screens, or a page that rewrites its own frame URLs in a loop, must
#: not be able to fill a disk while an operator is looking for their phone.
MAX_CHALLENGE_SHOTS = 25


def _await_challenge(page, spec: Dict[str, Any], journal, capture,
                     journal_path: str, timeout_ms: int,
                     seen: Optional[set] = None, otp=None) -> None:
    """See the 3-D Secure challenge through: auto-fill the code if we can read
    it, otherwise hold the open browser for a HUMAN.

    *** THE OUTCOME, NOT THE CHALLENGE, IS WHAT THIS GUARANTEES. *** It waits,
    keeps every screen the bank shows (they cannot be revisited once a code is
    spent), and notices the handback so the outcome can be read and journalled
    rather than left "unknown". That contract is unchanged whether a code gets
    typed automatically or not.

    `otp` is an optional src.otp_relay.OtpInbox. When one is supplied AND the
    challenge turns out to be a typed code AND a fresh SMS can be read, the
    code is filled and submitted here. EVERY ONE of those can fail, and each
    failure falls back to the same place: keep waiting, let a human finish in
    the browser. Which is what this function did before auto-fill existed, so
    the fallback path is the well-tested one rather than a new risk.

    It does NOT press Continue on an out-of-band ("approve in your app")
    challenge — that approval is the cardholder's, and clicking ahead of them
    fails the authentication. See src/payment/otp.py.

    `seen` carries signatures already captured by the caller, so the screen
    photographed immediately after the click is not photographed again one
    millisecond later under a second name.

    Returns when the browser is back on VFS. Raises PaymentSubmitted when
    nobody — bot or human — finished inside the window.
    """
    _journal_quietly(journal, {
        "event": "payment_challenge",
        "detail": "3-D Secure challenge raised; waiting for the cardholder",
        "url": getattr(page, "url", ""),
        "frames": _frame_urls(page),
    })
    # NOT NECESSARILY AN OTP, and saying "enter the OTP" to someone looking at
    # a bank-app prompt is a confusing instruction at the worst moment.
    # Observed live on AE/NOR 2026-10-14: Mashreq's ACS served an OUT-OF-BAND
    # challenge — approve in the bank's app, then press Continue. There was no
    # code to type and no field to type it into. Which method an issuer picks
    # is the issuer's choice and can differ per card and per transaction, so
    # the instruction names the screen rather than the method.
    log.warning("=" * 70)
    log.warning("  3-D SECURE: FINISH THE BANK'S CHECK IN THE OPEN BROWSER.")
    log.warning("  (an OTP to type, or an approval in your banking app — "
                "whichever your bank shows)")
    if otp is not None:
        # Said as an assist, not as a promise: the SIM may be elsewhere, the
        # relay may be down, and the issuer may not be asking for a code at
        # all. An operator who reads "the bot will handle it" and walks away
        # from a challenge that needs their phone loses the booking.
        log.warning("  An SMS relay is configured and will try to type a "
                    "texted code automatically —")
        log.warning("  DO NOT RELY ON IT. Stay with the browser: an app "
                    "approval still needs you.")
    log.warning(f"  Waiting up to {_human_wait(timeout_ms)}. Do not close it.")
    log.warning("=" * 70)

    # MUTATED IN PLACE when the caller supplies one, deliberately: the caller
    # captures the screen before this is entered and again after it returns,
    # and all three have to agree on what has already been photographed.
    if seen is None:
        seen = set()
    shots = 0
    otp_attempts = 0
    max_otp_attempts = _otp_max_attempts(spec)
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        # THE HANDBACK IS CHECKED FIRST, and that ordering is the difference
        # between the return page being filed as 'otp_03' and as
        # 'challenge_returned'. This loop photographs CHALLENGE screens; the
        # page VFS comes back on is the caller's to label, and a name that
        # says what a file is worth more than one more numbered screenshot.
        if _returned_to_merchant(page, spec):
            log.info(f"  gateway handed back to VFS: {page.url}")
            return
        signature = _screen_signature(page)
        if (signature not in seen and capture is not None
                and shots < MAX_CHALLENGE_SHOTS):
            seen.add(signature)
            shots += 1
            try:
                capture(page, f"otp_{shots:02d}")
            except Exception as e:                          # noqa: BLE001
                log.warning(f"  could not capture the challenge screen: {e}")

        # ── AUTO-FILL, IF ALL OF IT LINES UP ────────────────────────────────
        # Deliberately AFTER the handback check and the capture: a code typed
        # into a screen we never photographed loses the only evidence of what
        # the bank asked, and the handback must win over everything.
        #
        # The attempt cap is the point of this block. Banks lock a card after
        # a few wrong codes, so a loop that retried for the full ten minutes
        # would turn a parsing bug into a dead card. Two tries, then the
        # human — who is the fallback for every other failure here anyway.
        if otp is not None and otp_attempts < max_otp_attempts:
            code = otp.poll()
            if code:
                otp_attempts += 1
                from src.payment import otp as payment_otp

                filled = payment_otp.fill_and_submit(page, spec, code)
                _journal_quietly(journal, {
                    "event": "payment_otp_filled" if filled
                             else "payment_otp_fill_failed",
                    # The code itself is NEVER journalled. state/payments.jsonl
                    # is a durable file an operator greps; a live OTP in it is
                    # a secret written down for no reason, and by the time
                    # anyone reads the row the code is spent anyway.
                    "detail": (f"attempt {otp_attempts}/{max_otp_attempts}, "
                               f"{len(code)} digits from the SMS relay"),
                    "url": getattr(page, "url", ""),
                })
                if filled:
                    log.info("  code typed and submitted; waiting for the "
                             "bank to answer")
                else:
                    log.warning(
                        "  a code was read from SMS but could not be "
                        "submitted to the bank's screen. Finish in the open "
                        "browser.")
                # Give the ACS a beat to navigate before the next handback
                # check, so the loop does not photograph a half-submitted form.
                _pause(page, 2000)
                continue

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


#: How many codes may be typed before the bot stops trying and leaves it to a
#: human. Two, because banks lock a card after a small number of wrong codes
#: and the cost of stopping early is a fallback that already works.
MAX_OTP_ATTEMPTS = 2


def _otp_max_attempts(spec: Dict[str, Any]) -> int:
    """From the processor's config, clamped. Zero disables auto-fill for one
    processor without touching the relay or the route."""
    value = spec.get("otp_max_attempts", MAX_OTP_ATTEMPTS)
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        return MAX_OTP_ATTEMPTS
    # Never unbounded, whatever the file says. A config typo must not be able
    # to spend a card's retry budget.
    return max(0, min(attempts, 3))


def submit_payment(page, spec: Dict[str, Any], journal=None,
                   timeout_ms: int = SETTLE_TIMEOUT_MS, capture=None,
                   otp_timeout_ms: Optional[int] = None, otp=None) -> str:
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

    `otp` is an optional src.otp_relay.OtpInbox. It is ARMED HERE, immediately
    before the click, rather than by the caller — see the comment at the arm()
    call. Without one, the 3-D Secure path behaves exactly as it always has.
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

    # ─────────── ARM THE SMS INBOX. AS LATE AS POSSIBLE. ───────────
    # Every second between arming and the click is a second in which an SMS
    # from an EARLIER attempt could land inside the window and be typed. The
    # bank cannot text a code for a payment that has not been submitted, so
    # arming here means the first eligible message is necessarily this
    # payment's. Doing it in the caller — before filling the card, say — would
    # widen that window to a minute for no benefit whatsoever.
    #
    # Best-effort: a relay that cannot be armed must not stop a payment that
    # is otherwise ready. It only costs the auto-fill.
    if otp is not None:
        try:
            otp.arm()
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  could not arm the SMS relay ({e}); the 3-D "
                        "Secure code will have to be typed by hand.")
            otp = None

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

    # ONE FILE SET PER DISTINCT SCREEN, no matter how many times the code
    # looks. The live run of 2026-10-14 wrote the same screen twice at BOTH
    # ends — 'submitted' and 'otp_01' in the same second, then 'otp_03' and
    # 'challenge_returned' three seconds apart — because three separate places
    # each capture "the current page" and none of them knew about the others.
    # Ten redundant files in the one directory somebody reads when a payment
    # has gone wrong. The set is shared with _await_challenge, which mutates
    # it, so all three agree.
    captured: set = set()

    def capture_once(label: str) -> None:
        """Capture `label` unless this exact screen is already on disk."""
        if capture is None:
            return
        signature = _screen_signature(page)
        if signature in captured:
            log.debug(f"  '{label}' is the screen already captured — skipped")
            return
        captured.add(signature)
        try:
            capture(page, label)
        except Exception as e:                              # noqa: BLE001
            # Evidence is worth a lot. It is not worth abandoning a payment
            # that has already been submitted.
            log.warning(f"  could not capture '{label}': {e}")

    # THE FIRST SCREEN AFTER THE IRREVERSIBLE CLICK. Taken before anything is
    # decided about it: whatever the gateway did with the payment, this is the
    # page that says so, and it is not reachable again.
    capture_once("submitted")

    if _challenge_showing(page, spec):
        # HAND OVER, DO NOT GIVE UP. The old code journalled "unknown" and
        # raised here, which threw away the actual outcome: the OTP is typed
        # seconds later, the gateway returns to VFS with PaymentStatus in the
        # URL, and that is the answer this journal row exists to carry. The
        # bot still never types the OTP — it waits for the human and then
        # READS.
        _await_challenge(page, spec, journal, capture, journal_path,
                         otp_timeout_ms, seen=captured, otp=otp)
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception as e:                              # noqa: BLE001
            # Back on VFS and readable already — the outcome is in the URL,
            # which does not need the page to have finished loading.
            log.debug(f"Return page load state not confirmed: {e}")
        capture_once("challenge_returned")

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
