"""Authentication + rate limiting for the webhook API.

Two deliberate choices worth reading before you change anything here:

1. `hmac.compare_digest`, not `==`. A plain string comparison short-circuits on
   the first differing byte, so the time it takes leaks how many leading
   characters were right. Over a tunnel that is noisy but not unexploitable;
   constant-time comparison costs nothing and removes the question entirely.

2. The 401 body is identical whether the header is missing, malformed, or
   simply wrong. Telling a caller "token present but incorrect" confirms they
   found the right header name — free reconnaissance for no benefit.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Final

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from src.api.config import ApiSettings, get_settings

log = logging.getLogger("vfs.api.security")

# The custom header carrying the shared secret.
SECRET_HEADER: Final[str] = "X-Webhook-Secret-Token"

# One opaque message for every auth failure mode.
_AUTH_FAILED_DETAIL: Final[str] = "Unauthorized: missing or invalid authentication token."

# Declares the header as a SECURITY SCHEME rather than a plain parameter. Two
# reasons that matters:
#   * Swagger UI renders an "Authorize" button, so /docs is usable — a bare
#     Header() parameter gets documented but gives you nothing to authorise with.
#   * The OpenAPI spec then describes how to authenticate, which is what client
#     generators read.
#
# auto_error=False keeps OUR error handling: FastAPI's default would return its
# own 403 with a different body, and would distinguish "missing" from "wrong" —
# exactly the disclosure the single opaque message below avoids.
_api_key_header = APIKeyHeader(
    name=SECRET_HEADER,
    auto_error=False,
    description="Shared secret. Required on every endpoint except /health.",
)


class _RateLimiter:
    """Fixed-window request counter, keyed by client IP.

    In-memory and per-process — it resets when the server restarts and does not
    survive multiple workers. That is fine for its actual job: blunting a flood
    from someone who has guessed the URL. It is NOT a substitute for the token.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, client_ip: str) -> bool:
        """Record a hit. Return False if `client_ip` is over its limit."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._hits[client_ip]
            # Drop timestamps that have aged out of the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            # Keep the dict from growing without bound across many IPs.
            if len(self._hits) > 1024:
                for ip in [k for k, v in self._hits.items() if not v]:
                    del self._hits[ip]
            return True


_settings = get_settings()
_limiter = _RateLimiter(
    max_requests=_settings.rate_limit_requests,
    window_seconds=_settings.rate_limit_window_seconds,
)


def _client_ip(request: Request) -> str:
    """Best-effort client identity for rate limiting.

    Behind a tunnel every request arrives from loopback, so we prefer the
    tunnel-supplied X-Forwarded-For when present. This value is used ONLY for
    rate limiting — never for authentication — because a client controls it.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def require_token(
    request: Request,
    x_webhook_secret_token: str | None = Security(_api_key_header),
) -> None:
    """FastAPI dependency: reject anything without the correct shared secret.

    Raises 401 on a missing/incorrect token, 429 when the caller is over its
    rate limit. Returns None on success — nothing downstream needs the token.
    """
    settings: ApiSettings = get_settings()
    ip = _client_ip(request)

    # Rate limit BEFORE the comparison so a flood of bad tokens is cheap to
    # absorb and cannot be used to probe timing.
    if not _limiter.check(ip):
        log.warning("Rate limit exceeded for %s on %s", ip, request.url.path)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down.",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )

    if not x_webhook_secret_token:
        log.warning("Auth failure (header absent) from %s on %s", ip, request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_AUTH_FAILED_DETAIL,
        )

    # Constant-time comparison. Both sides encoded to bytes so a non-ASCII
    # header cannot raise inside compare_digest.
    supplied = x_webhook_secret_token.strip().encode("utf-8")
    expected = settings.secret_token.encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        # Never log the supplied token, not even a prefix.
        log.warning("Auth failure (bad token) from %s on %s", ip, request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_AUTH_FAILED_DETAIL,
        )


def _reset_rate_limiter() -> None:
    """Clear all rate-limit state. FOR TESTS ONLY.

    The limiter is a module-level singleton (one per server process), which is
    correct in production but makes a test suite's many requests look like one
    client flooding the endpoint. Tests call this between cases instead of
    raising the production limit to accommodate them.
    """
    with _limiter._lock:                       # noqa: SLF001 — test seam
        _limiter._hits.clear()                 # noqa: SLF001
