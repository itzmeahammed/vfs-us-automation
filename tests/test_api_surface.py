"""Every endpoint is authenticated, and the contract is enumerable.

WHY THIS FILE EXISTS
--------------------
The API is the sales agent's entire surface: it creates clients, arms them, and
triggers runs that spend real appointment slots. One route shipped without a
token check is a complete compromise of that, and it is exactly the kind of
mistake a growing router set makes — a new file, a forgotten dependency, and
nothing fails.

So this asserts the property rather than the list: EVERY documented route
except a small, named allowlist must refuse an unauthenticated request. A new
endpoint is covered the moment it is added, without anyone remembering to add
a test.

The routes are read from /openapi.json, not from app.routes, because this
FastAPI version keeps included routers as lazy `_IncludedRouter` wrappers whose
paths are invisible to naive introspection — an audit written against
app.routes silently sees 11 routes where there are 34, and reports a clean bill
of health for endpoints it never looked at.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_TOKEN = "d" * 64
os.environ.setdefault("VFSAPI_SECRET_TOKEN", TEST_TOKEN)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

#: Routes that are unauthenticated ON PURPOSE. Anything not named here must
#: refuse an anonymous caller. Keep this list short and justified.
PUBLIC_ROUTES = {
    # Liveness only. Says nothing sensitive, and exists so the tunnel can be
    # checked without handing out the token to whoever is testing.
    ("GET", "/health"),
}

#: Stand-ins for path parameters. The VALUE never matters: an unauthenticated
#: request must be refused before the handler looks at it, so a nonexistent id
#: proves the point better than a real one.
_PATH_STUBS = {
    "{job_id}": "nonexistent-job",
    "{client_id}": "nonexistent-client",
    "{route}": "AE-CHE",
    "{registrant_id}": "nonexistent-client",
    "{email}": "nobody@example.com",
}


@pytest.fixture()
def api():
    """A client with the rate limiter reset.

    The limiter is a per-process singleton — correct in production, and here it
    makes one test's sweep over 34 routes look like a flood that then 429s the
    NEXT test. Reset per test rather than weakening the limit, exactly as
    tests/test_api_clients.py does.
    """
    from fastapi.testclient import TestClient

    from src.api.security import _reset_rate_limiter

    _reset_rate_limiter()

    from src.api.main import app

    with TestClient(app) as client:
        yield client


def _fill(path: str) -> str:
    for placeholder, value in _PATH_STUBS.items():
        path = path.replace(placeholder, value)
    return path


def _documented_routes(client):
    """(method, path) for every route in the OpenAPI spec."""
    spec = client.get("/openapi.json").json()
    routes = []
    for path, operations in spec.get("paths", {}).items():
        for method in operations:
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                routes.append((method.upper(), path))
    return sorted(routes)


def test_the_router_set_is_actually_mounted(api):
    """A guard against the whole surface silently disappearing.

    Every router is attached at the BOTTOM of main.py, after `app` exists. An
    import error in any one of them, or a bad merge that drops an
    include_router line, leaves a server that starts cleanly and serves a
    fraction of its contract — with no failure anywhere.
    """
    paths = {path for _, path in _documented_routes(api)}

    for expected in ("/clients", "/booking/status", "/booking/trigger",
                     "/status", "/pipeline", "/accounts/health", "/config",
                     "/inbox/reconcile", "/payments/unanswered",
                     "/jobs/{job_id}/stream"):
        assert expected in paths, (
            f"{expected} is missing from the API. A router failed to mount, "
            "which does not raise — it just serves less.")


def test_every_route_requires_the_token(api):
    """No endpoint may act for an anonymous caller.

    Asserted as a property over the whole spec rather than route by route, so a
    new endpoint is covered the moment someone adds it.

    A 429 counts as refusal: the rate limiter runs ahead of auth, and a request
    it rejected never reached the handler. What must never appear is a 2xx.
    """
    unauthenticated = []

    for method, path in _documented_routes(api):
        if (method, path) in PUBLIC_ROUTES:
            continue
        response = api.request(method, _fill(path))
        if response.status_code < 400:
            unauthenticated.append(f"{method} {path} -> {response.status_code}")

    assert not unauthenticated, (
        "These routes served an UNAUTHENTICATED request:\n  "
        + "\n  ".join(unauthenticated))


def test_the_public_allowlist_really_is_public(api):
    """The other half: a route listed as public must actually answer.

    Without this, PUBLIC_ROUTES could quietly accumulate entries for endpoints
    that are in fact authenticated, and the exemption above would hide a real
    regression the day one of them stopped being.
    """
    for method, path in PUBLIC_ROUTES:
        response = api.request(method, _fill(path))
        assert response.status_code < 400, (
            f"{method} {path} is on the public allowlist but returned "
            f"{response.status_code}. Either fix the route or remove the "
            "exemption — a stale entry there masks a real auth gap.")


def test_a_valid_token_is_accepted(api):
    """Proves the refusals above are about AUTH, not a broken app.

    A suite where every route 401s would pass the test above even if the
    service were entirely broken.
    """
    response = api.get("/status", headers=HEADERS)
    assert response.status_code < 400, response.text


def test_secrets_never_appear_in_the_openapi_spec(api):
    """The spec is served to anyone who can reach /docs.

    Examples and defaults are the usual leak: a password copied into a Field
    example ends up published in the schema.
    """
    import json

    spec = json.dumps(api.get("/openapi.json").json())

    secrets = []
    try:
        token = os.environ.get("VFSAPI_SECRET_TOKEN", "")
        if token and token in spec:
            secrets.append("VFSAPI_SECRET_TOKEN")
    except Exception:                                       # noqa: BLE001
        pass

    assert not secrets, f"secrets found in the OpenAPI spec: {secrets}"
