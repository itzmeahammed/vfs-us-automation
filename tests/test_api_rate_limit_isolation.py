"""Unauthenticated noise must not be able to starve the console.

WHY THIS FILE EXISTS
--------------------
`require_token` used to check the rate limiter BEFORE comparing the token, so
a caller with no token at all spent the same budget as the operator console.
The per-IP bucket keys on `X-Forwarded-For`, which the caller supplies, so
rotating that header gave a fresh bucket per request and moved the whole flood
onto the GLOBAL ceiling — which counts the console's legitimate traffic too.

Measured against the real app, 700 unauthenticated requests with a rotating
`X-Forwarded-For`:

    before:  600 x 401, then 100 x 429, and an AUTHENTICATED call -> 429
    after:    20 x 401, then       429s, and an AUTHENTICATED call -> 200

Tripping the ceiling was itself the denial of service: every operator action
failed with "the bot is rate-limiting this server", while `GET /v1/health`
kept answering — so the console's connection banner still reported the bot as
up. Nobody could arm a client, settle a dangling registration, or cancel a run
for as long as the flood lasted.

Failed authentication now spends a separate, much smaller bucket, keyed on the
real socket peer rather than a header so rotation cannot escape it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "f" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.api.core import security as sec  # noqa: E402

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

#: Comfortably past the global ceiling, which is what the evasion targets.
FLOOD = sec._settings.rate_limit_global_requests + 100   # noqa: SLF001


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient

    sec._reset_rate_limiter()                            # noqa: SLF001

    from src.api.main import app

    with TestClient(app) as client:
        yield client
    sec._reset_rate_limiter()                            # noqa: SLF001


def _flood(api, n):
    """n unauthenticated requests, each claiming a different forwarded IP."""
    codes = {}
    for i in range(n):
        res = api.get("/v1/switches",
                      headers={"X-Forwarded-For": f"10.0.0.{i % 250}"})
        codes[res.status_code] = codes.get(res.status_code, 0) + 1
    return codes


def test_an_unauthenticated_flood_does_not_starve_an_authenticated_caller(api):
    """The regression this file exists for."""
    _flood(api, FLOOD)

    res = api.get("/v1/switches", headers=HEADERS)
    assert res.status_code == 200, (
        "an unauthenticated flood exhausted the budget the console needs — "
        "operators could not arm, resolve or cancel anything while it lasted")


def test_the_failed_auth_bucket_is_far_smaller_than_the_real_one(api):
    """A flood should be cut off early, not allowed 600 attempts."""
    codes = _flood(api, FLOOD)

    assert codes.get(401, 0) <= sec._AUTH_FAIL_MAX_GLOBAL, (  # noqa: SLF001
        f"{codes.get(401)} bad-token attempts were served before throttling; "
        f"the ceiling is {sec._AUTH_FAIL_MAX_GLOBAL}")
    assert codes.get(429, 0) > 0, "the flood was never throttled at all"


def test_rotating_the_forwarded_header_does_not_escape_the_limit(api):
    """The bucket is keyed on the socket peer, which a caller cannot forge."""
    codes = _flood(api, FLOOD)
    assert codes.get(429, 0) > 0


def test_a_valid_token_is_still_refused_once_over_the_real_budget(api):
    """The authenticated ceiling must still exist — this is not a bypass."""
    limit = sec._settings.rate_limit_requests                # noqa: SLF001
    seen = set()
    for _ in range(limit + 5):
        seen.add(api.get("/v1/switches", headers=HEADERS).status_code)
    assert 429 in seen, "the authenticated rate limit stopped applying"


def test_health_is_public_but_not_unbounded(api):
    """`/v1/health` is deliberately unauthenticated so a tunnel can be probed
    without handing out the token. Because the limiter lived inside
    `require_token`, it previously had NO ceiling — not even the global one."""
    first = api.get("/v1/health")
    assert first.status_code == 200
    assert "X-Webhook-Secret-Token" not in first.request.headers

    codes = {first.status_code: 1}
    for _ in range(sec._settings.rate_limit_requests + 20):   # noqa: SLF001
        code = api.get("/v1/health").status_code
        codes[code] = codes.get(code, 0) + 1

    assert 429 in codes, (
        "unlimited polling of /v1/health consumes the worker pool that every "
        "authenticated endpoint shares")
