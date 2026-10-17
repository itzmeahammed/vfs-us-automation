"""Scrubbing what leaves the machine.

Two jobs, applied to every tool result before the model sees it:

1. SECRETS are destroyed — by key name (a dict key called `password`, `token`,
   `api_key`, ...) and by value shape (credentials inside URLs, OpenAI keys,
   bearer tokens, JWTs, Telegram bot tokens, Luhn-valid card numbers). There is
   no way back from these; the model never needs them.

2. E-MAIL ADDRESSES are pseudonymised, not destroyed: `alice@x.com` becomes
   `acct-3f9a1c@x.com`, the same token every time, so the model can still count,
   group and compare accounts. The Redactor remembers the mapping for the
   length of one request and `restore()` swaps the real address back into the
   answer the browser receives, and into tool arguments the model sends back.
   The provider only ever sees the pseudonym.

Redaction is defence in depth, not the main control: tools should not return
secrets in the first place. This catches what slips through (a password echoed
into a log line, a proxy URL with credentials in an error message).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict

REDACTED = "[redacted]"

#: Dict keys whose VALUE is always secret, whatever it looks like. Matched on
#: the whole key, so `otp` stats or `token_count` style fields are not caught by
#: accident — only names that unambiguously hold a secret.
_SECRET_KEY = re.compile(
    r"^(?:.*[_-])?(?:password|passwd|pwd|pass|secret|client_secret|token|access_token|"
    r"refresh_token|id_token|api_?key|apikey|authorization|auth|cookie|cookies|"
    r"set-cookie|session_?id|private_?key|cvv|cvc|cvv2|card_?number|pan|iban|pin|otp_code)$",
    re.I,
)

_VALUE_PATTERNS = [
    # scheme://user:pass@host  ->  scheme://***:***@host
    (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@", re.I), r"\1***:***@"),
    # OpenAI / Anthropic style keys
    (re.compile(r"\b(?:sk|rk|pk)-(?:[A-Za-z0-9_-]+-)?[A-Za-z0-9_-]{16,}\b"), REDACTED),
    # Authorization: Bearer xxx
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 " + REDACTED),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), REDACTED),
    # Telegram bot token  123456789:AA...35
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), REDACTED),
    # ?token=...&api_key=...  in URLs
    (re.compile(r"([?&](?:token|access_token|key|api_key|apikey|password|pwd|sig|signature|secret)=)"
                r"[^&\s\"']+", re.I), r"\1" + REDACTED),
    # key = value / key: value pairs in free text (log lines, config dumps)
    (re.compile(r"\b(password|passwd|pwd|api[_-]?key|secret|token|bot_token)(\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|\S+)", re.I), r"\1\2" + REDACTED),
]

_CARD = re.compile(r"(?<![\d-])[3-6](?:[ -]?\d){12,18}(?![\d-])")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PSEUDO = re.compile(r"\bacct-[0-9a-f]{6}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _mask_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[card ****" + digits[-4:] + "]"
    return m.group(0)


class Redactor:
    """One per request: holds the pseudonym map so the answer can be restored."""

    def __init__(self, pseudonymise_emails: bool = True, salt: str = ""):
        self.pseudonymise_emails = pseudonymise_emails
        self._salt = salt
        self._fwd: Dict[str, str] = {}   # real (lower-cased) -> pseudonym
        self._back: Dict[str, str] = {}  # pseudonym -> real, as first seen

    # ---- text --------------------------------------------------------------
    def text(self, s: str) -> str:
        for pat, repl in _VALUE_PATTERNS:
            s = pat.sub(repl, s)
        s = _CARD.sub(_mask_card, s)
        if self.pseudonymise_emails:
            s = _EMAIL.sub(self._email, s)
        return s

    def _email(self, m: re.Match) -> str:
        real = m.group(0)
        if _PSEUDO.fullmatch(real):
            return real
        key = real.lower()
        if key not in self._fwd:
            h = hashlib.sha256((self._salt + key).encode()).hexdigest()[:6]
            token = f"acct-{h}@{m.group(2).lower()}"
            self._fwd[key] = token
            self._back.setdefault(token, real)
        return self._fwd[key]

    # ---- structures --------------------------------------------------------
    def obj(self, value: Any, _key: str = "") -> Any:
        if _key and _SECRET_KEY.match(_key) and value not in (None, "", 0, False, [], {}):
            return REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(k): self.obj(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.obj(v) for v in value]
        return value

    # ---- the way back ------------------------------------------------------
    def restore(self, s: str) -> str:
        """Pseudonyms -> real addresses. Used on the answer and on tool args."""
        if not self._back:
            return s
        return _PSEUDO.sub(lambda m: self._back.get(m.group(0).lower(), m.group(0)), s)

    def restore_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, dict):
            return {k: self.restore_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore_obj(v) for v in value]
        return value


class StreamRestorer:
    """Restores pseudonyms in a token stream without splitting one in half.

    A pseudonym can arrive across several deltas. Text is released only up to
    the last whitespace, so a token is always whole when it is restored; the
    tail is held until more arrives or the stream ends.
    """

    def __init__(self, redactor: Redactor):
        self._r = redactor
        self._buf = ""

    def feed(self, delta: str) -> str:
        self._buf += delta
        cut = max(self._buf.rfind(" "), self._buf.rfind("\n"), self._buf.rfind("\t"))
        if cut < 0:
            # No boundary yet. Don't hoard forever on a long unbroken run.
            if len(self._buf) < 200:
                return ""
            cut = len(self._buf) - 1
        out, self._buf = self._buf[:cut + 1], self._buf[cut + 1:]
        return self._r.restore(out)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return self._r.restore(out)
