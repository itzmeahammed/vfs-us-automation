"""What can go wrong getting a code, as distinct exceptions.

The distinction that matters is NOT CONFIGURED vs UNAVAILABLE vs TIMED OUT.
Callers on the payment path treat all three the same way — fall back to the
human — but an operator reading the log needs to know which one happened,
because the fixes are unrelated: fill in a key, check the device is online, or
look at why no SMS arrived.
"""

from __future__ import annotations


class OtpRelayError(Exception):
    """Base class. Deliberately NOT derived from the payment or booking errors:
    a code that could not be fetched must never be mistaken for a payment
    failure, and must never be swallowed by a handler that retries VFS."""


class OtpNotConfigured(OtpRelayError):
    """No API key, so there is no inbox to read. An ordinary state on a
    developer machine, and the reason request_code is allowed to return None
    rather than raising on every call."""


class OtpRelayUnavailable(OtpRelayError):
    """The relay was asked and could not answer — network, auth, HTTP 5xx, a
    device offline. The code may well exist; we cannot see it."""


class OtpTimeout(OtpRelayError):
    """No matching message arrived inside the window. The commonest cause is
    the dullest: the SIM that receives the code is not in the relay."""
