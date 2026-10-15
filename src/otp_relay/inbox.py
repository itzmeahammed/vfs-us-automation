"""Wait for a code that arrived AFTER we asked for it.

    ═══════════════════ THE ONLY HARD PROBLEM IN THIS PACKAGE ═══════════════

Reading an SMS is easy. Reading the RIGHT SMS is the whole job, and there is
exactly one way to get it wrong that matters: typing a code from a previous
attempt. A stale code is always rejected by the bank, and three rejections
locks the card — on a booking that is already paid for and cannot be re-paid.

Two mechanisms, both required, neither sufficient alone:

    ARMING     `arm()` stamps the moment just before the Pay click. Nothing
               received before that instant is ever a candidate. The payment
               path calls it inside submit_payment, as late as possible,
               because every second between arming and clicking is a second in
               which a stale message could sneak into the window.

    CONSUMING  every message id this object has returned a code from is
               remembered, so the same SMS cannot be typed twice even if the
               bank's own screen bounces and the loop comes round again.

    ═════════════════════════════ CLOCK SKEW ═════════════════════════════════

`received_at` comes from Relayion's clock; `arm()` reads ours. If this machine
runs a minute fast, a perfectly good code looks like it arrived before we
asked and is discarded forever — and the symptom is indistinguishable from "no
SMS came", which is an hour of debugging the wrong thing.

So the anchor is relaxed backwards by `grace_s` (default 90s). That is a
deliberate trade: it admits a message up to 90 seconds stale in exchange for
tolerating 90 seconds of clock drift. Codes expire in 3-5 minutes and the
consumed-id set stops a re-type, so the exposure is one stale code at most,
once — against a failure mode that would otherwise be permanent and silent.

    ═════════════════════════════════ LOGS ═════════════════════════════════════

No method here logs a code or a message body. The log says "a 6-digit code
from MashreqBK", which is everything an operator needs and nothing a log file
should hold.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Set

from src.otp_relay import extract, relayion
from src.otp_relay.errors import OtpRelayUnavailable

log = logging.getLogger(__name__)

#: Don't ask Relayion more often than this. The payment loop ticks once a
#: second to stay responsive to the gateway handing back; polling an HTTP API
#: at 1Hz for ten minutes would be 600 requests to read one text message.
DEFAULT_POLL_S = 5

#: How far back of the arming instant a message may have been received. See
#: CLOCK SKEW above — this is not a convenience, it is what stops a fast
#: clock from discarding every code.
DEFAULT_GRACE_S = 90


class OtpInbox:
    """A one-shot, armed view of the SMS inbox.

    Built before the irreversible click, armed at it, then polled. Not
    reusable across payments: the consumed set and the anchor both belong to
    one transaction, and sharing them is how attempt two types attempt one's
    code.
    """

    def __init__(self, digits: int = 6, senders: Sequence[str] = (),
                 poll_s: int = DEFAULT_POLL_S, grace_s: int = DEFAULT_GRACE_S,
                 code_words: Sequence[str] = (), key: str = "",
                 fetch: Optional[Callable] = None):
        self.digits = int(digits or 6)
        self.senders = tuple(senders or ())
        self.poll_s = max(1, int(poll_s or DEFAULT_POLL_S))
        self.grace_s = max(0, int(grace_s if grace_s is not None
                                  else DEFAULT_GRACE_S))
        self.code_words = tuple(code_words or ())
        self.key = key
        #: Injectable for tests: (since, sender) -> List[Message]. Defaults to
        #: the real API so production needs no wiring.
        self._fetch = fetch or (
            lambda since, sender: relayion.list_inbound(
                since=since, sender=sender, key=self.key))

        self.since: Optional[datetime] = None
        self.consumed: Set[str] = set()
        self.errors = 0
        self._last_poll = 0.0

    # --------------------------------------------------------------- #

    @property
    def armed(self) -> bool:
        return self.since is not None

    def arm(self, now: Optional[datetime] = None) -> datetime:
        """Mark the instant from which a code counts. Call JUST before asking
        the bank for one — not at construction, which may be seconds earlier
        and widens the stale window for no benefit."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self.since = moment - timedelta(seconds=self.grace_s)
        log.info(f"  OTP inbox armed; codes received after "
                 f"{self.since.isoformat()} count "
                 f"({self.grace_s}s clock grace)")
        return self.since

    def poll(self, monotonic: Optional[float] = None) -> Optional[str]:
        """The next unconsumed code, or None. NEVER raises.

        Rate-limited internally, so a caller may call it every tick of a 1Hz
        loop without hammering the API. Returns None for all of "too soon",
        "nothing new", "relay down" and "could not parse" — the caller's
        fallback is the same in every case (keep waiting, then the human), and
        an exception escaping here would abandon a submitted payment.
        """
        if not self.armed:
            log.warning("  OTP inbox polled before it was armed — ignoring. "
                        "This is a wiring bug: arm() must be called before "
                        "the code is requested.")
            return None

        clock = monotonic if monotonic is not None else time.monotonic()
        if self._last_poll and clock - self._last_poll < self.poll_s:
            return None
        self._last_poll = clock

        try:
            messages = self._fetch(self.since, "")
        except OtpRelayUnavailable as exc:
            self.errors += 1
            # Logged every time, not once: a relay that is down for the whole
            # challenge window is the thing an operator needs to see in the
            # run log afterwards, and it is at most one line per poll_s.
            log.warning(f"  could not read the SMS relay: {exc}")
            return None
        except Exception as exc:                                # noqa: BLE001
            self.errors += 1
            log.warning(f"  SMS relay failed unexpectedly: {exc}")
            return None

        return self._pick(messages)

    # --------------------------------------------------------------- #

    def _pick(self, messages: List) -> Optional[str]:
        """The first code worth typing, oldest-first among the fresh ones."""
        fresh = [m for m in messages if self._eligible(m)]
        # Oldest first: if two codes arrived (a resend), the bank's own screen
        # is usually waiting on the FIRST, and the second is the one to try
        # next time round rather than now.
        fresh.sort(key=lambda m: m.received_at or datetime.min.replace(
            tzinfo=timezone.utc))

        for message in fresh:
            code = extract.find_code(message.body, self.digits,
                                     self.code_words)
            if not code:
                # Worth a line: a message that arrived in the window and could
                # NOT be parsed is how an issuer's wording gets added to the
                # config, and it is invisible otherwise. The body is not shown.
                log.info(f"  SMS from {message.sender or '?'} in the window "
                         f"held no {self.digits}-digit code — ignoring "
                         f"({len(message.body)} chars)")
                self.consumed.add(message.id)
                continue
            self.consumed.add(message.id)
            log.info(f"  got a {self.digits}-digit code from "
                     f"{message.sender or 'an unknown sender'}")
            return code
        return None

    def _eligible(self, message) -> bool:
        if not message.id or message.id in self.consumed:
            return False
        if not extract.looks_like_sender(message.sender, self.senders):
            return False
        # THE ANCHOR. A message with no timestamp is discarded rather than
        # assumed fresh: "we do not know when this arrived" must not be
        # allowed to mean "therefore type it".
        if message.received_at is None:
            return False
        return message.received_at >= self.since


def request_code(timeout_s: int = 180, digits: int = 6,
                 senders: Sequence[str] = (), poll_s: int = DEFAULT_POLL_S,
                 since: Optional[datetime] = None, key: str = "",
                 fetch: Optional[Callable] = None) -> Optional[str]:
    """Block until a code arrives, or return None at `timeout_s`.

    The contract docs/archive/OTP_RELAY_TASKS.md specifies, for the callers
    that can afford to block — the AE-ITA waitlist OTP step, and the CLI.

    *** NOT used by the payment path. *** There, the same wait has to share a
    loop with watching for the gateway to hand back, so submit_payment drives
    an OtpInbox directly and blocks on nothing.
    """
    inbox = OtpInbox(digits=digits, senders=senders, poll_s=poll_s, key=key,
                     fetch=fetch)
    inbox.arm(since)
    deadline = time.monotonic() + max(0, timeout_s)
    while True:
        code = inbox.poll()
        if code:
            return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(inbox.poll_s, 1))
