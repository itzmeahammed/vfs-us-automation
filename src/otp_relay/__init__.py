"""Get a one-time code from somewhere that is not this process.

A standalone service whose entire contract is:

    ask for a code; return it, or time out.

It knows nothing about payments, bookings or waitlists, and imports nothing
from them. That is what lets the 3-D Secure step and the AE-ITA waitlist OTP
step share it — docs/archive/OTP_RELAY_TASKS.md specified this shape for a
Telegram transport; Relayion reads the SMS directly instead, so no human has
to be awake.

    errors.py     OtpNotConfigured / OtpRelayUnavailable / OtpTimeout
    extract.py    PURE. an SMS body -> the code, or None. Refuses to guess.
    relayion.py   the HTTP client. Reads inbound SMS and lists devices.
    inbox.py      ARMED waiting: only codes received after we asked count.

Typical use:

    python -m src.otp_relay devices           # is the SIM online?
    python -m src.otp_relay watch             # tail inbound SMS, codes masked
    python -m src.otp_relay wait --digits 6   # block for one code

    ═════════════════════════ THE OPERATIONAL CATCH ═════════════════════════

The bank texts THE NUMBER REGISTERED ON THE CARD. If that SIM is not in a
Relayion device, nothing here can work and the payment path falls back to a
human in the browser — which is the pre-existing behaviour, not a regression.
Run `devices` first.
"""

from src.otp_relay.errors import (  # noqa: F401
    OtpNotConfigured,
    OtpRelayError,
    OtpRelayUnavailable,
    OtpTimeout,
)
from src.otp_relay.extract import find_code  # noqa: F401
from src.otp_relay.inbox import OtpInbox, request_code  # noqa: F401

__all__ = [
    "OtpInbox",
    "request_code",
    "find_code",
    "OtpRelayError",
    "OtpNotConfigured",
    "OtpRelayUnavailable",
    "OtpTimeout",
]
