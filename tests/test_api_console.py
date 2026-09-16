"""The admin console at GET /console.

Two properties carry all the weight here:

  1. It is OFF unless asked for, and returns 404 (not 403) when off, so a probe
     through the tunnel cannot tell a switched-off console from a build that
     never had one.
  2. Serving the page hands out NO data. It is markup only — the token is typed
     in by the operator and travels as a header on the API calls the page then
     makes. So the page being unauthenticated must not make any *endpoint*
     unauthenticated.

The CSP test matters more than it looks: the console needs 'unsafe-inline' to
render (its CSS/JS are inline in one self-contained file), and it would be easy
to widen the policy for every response while adding it. The API's JSON
responses must keep the strict policy.
"""

from __future__ import annotations


import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TOKEN = "t" * 64


def _app(console_enabled: bool):
    """Build the app with the console flag set.

    The flag lives on ApiSettings and is read per request, so clearing the
    settings cache is enough — no module reload needed.

    The flag is set EXPLICITLY in both directions, never merely unset. Leaving
    it unset let the developer's own .env.api decide the answer: once that file
    said `VFSAPI_ENABLE_CONSOLE=true`, the "off by default" tests failed on that
    machine and passed everywhere else. A test whose result depends on an
    untracked local file is worse than no test.
    """
    os.environ["VFSAPI_SECRET_TOKEN"] = TOKEN
    os.environ["VFSAPI_ENABLE_CONSOLE"] = "1" if console_enabled else "0"

    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()

    import src.api.main as main_mod
    return main_mod.app


@pytest.fixture(autouse=True)
def _restore_env():
    """Undo `_app`'s environment writes after every test in this module.

    `_app` assigns VFSAPI_* directly rather than through monkeypatch, so without
    this the values LEAK into the rest of the session: os.environ is
    process-wide, and the settings cache is cleared right after, so every later
    module sees this module's token. The modules that pick their token with
    `os.environ.setdefault` then capture a HEADERS constant at import time that
    no longer matches what the server expects, and their requests 401.

    That is exactly what happened — test_api_status failed with 401 in a full
    run and passed when run alone. Restoring here keeps `_app` simple while
    making the leak impossible.
    """
    import src.api.config as config_mod

    saved = {k: os.environ.get(k)
             for k in ("VFSAPI_SECRET_TOKEN", "VFSAPI_ENABLE_CONSOLE")}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # The cache still holds settings built from THIS module's values, so
        # restoring the env alone is not enough.
        config_mod.get_settings.cache_clear()


@pytest.fixture
def on():
    return TestClient(_app(True), raise_server_exceptions=False)


@pytest.fixture
def off():
    return TestClient(_app(False), raise_server_exceptions=False)


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


def test_console_is_off_by_default(off):
    """Anything reachable through a tunnel ships off unless chosen."""
    assert off.get("/console").status_code == 404


def test_disabled_console_is_indistinguishable_from_absent(off):
    """404, not 403: a 403 would confirm the feature exists."""
    r = off.get("/console")
    assert r.status_code == 404
    assert "console" not in r.text.lower()


def test_console_serves_when_enabled(on):
    r = on.get("/console")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>VFS Waitlist Console</title>" in r.text


# --------------------------------------------------------------------------
# It hands out markup, never data
# --------------------------------------------------------------------------


def test_console_page_needs_no_token(on):
    """Requiring a token to fetch the page would leave nowhere to type it."""
    assert on.get("/console").status_code == 200


def test_serving_the_console_does_not_unauthenticate_the_api(on):
    """The page is public; the data behind it is not."""
    on.get("/console")
    for path in ("/clients", "/status", "/jobs"):
        assert on.get(path).status_code == 401, path


def test_console_html_contains_no_secret(on):
    """The token must be supplied by the operator, never baked into the page."""
    assert TOKEN not in on.get("/console").text


def test_console_declares_the_secret_header(on):
    """It must call the API the same way every other client does."""
    assert "X-Webhook-Secret-Token" in on.get("/console").text


# --------------------------------------------------------------------------
# CSP: widened for exactly one path, and no further
# --------------------------------------------------------------------------


def test_console_csp_allows_inline_but_nothing_remote(on):
    csp = on.get("/console").headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'unsafe-inline'" in csp     # needed to render at all
    assert "connect-src 'self'" in csp             # may only call THIS api
    assert "frame-ancestors 'none'" in csp
    # No external origin may supply code.
    assert "http://" not in csp
    assert "https://" not in csp


def test_api_responses_keep_the_strict_csp(on):
    """The console's looser policy must not leak onto JSON responses."""
    r = on.get("/status", headers={"X-Webhook-Secret-Token": TOKEN})
    csp = r.headers["content-security-policy"]
    assert csp == "default-src 'none'; frame-ancestors 'none'"
    assert "unsafe-inline" not in csp


def test_health_keeps_the_strict_csp(on):
    csp = on.get("/health").headers["content-security-policy"]
    assert "unsafe-inline" not in csp


def test_console_cannot_be_framed(on):
    """Clickjacking: the page drives real registrations."""
    r = on.get("/console")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_console_is_not_cached(on):
    """It reflects live operational state; a stale copy misleads."""
    assert "no-store" in on.get("/console").headers["cache-control"]


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_console_asset_ships_beside_the_module():
    """A missing file would 500 at request time rather than at import."""
    assert (REPO_ROOT / "src" / "api" / "console.html").is_file()


def test_console_is_hidden_from_the_schema(on):
    """It is a page, not part of the machine-readable API surface."""
    import src.api.main as main_mod
    route = [r for r in main_mod.app.routes
             if getattr(r, "path", None) == "/console"][0]
    assert route.include_in_schema is False


def test_flag_is_settable_from_the_env_file(tmp_path, monkeypatch):
    """Regression: the flag must work from .env.api, not just os.environ.

    It was originally read with a bare os.environ lookup at import time, which
    silently ignored .env.api — the documented way to configure this server.
    Setting it there did nothing, and /console 404'd with no clue why.
    """
    import src.api.config as config_mod

    env_file = tmp_path / ".env.api"
    env_file.write_text(
        f"VFSAPI_SECRET_TOKEN={TOKEN}\nVFSAPI_ENABLE_CONSOLE=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("VFSAPI_ENABLE_CONSOLE", raising=False)
    monkeypatch.delenv("VFSAPI_SECRET_TOKEN", raising=False)

    settings = config_mod.ApiSettings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert settings.enable_console is True


def test_flag_defaults_to_off_when_unset(monkeypatch, tmp_path):
    """The safe default survives however it is loaded."""
    import src.api.config as config_mod

    env_file = tmp_path / ".env.api"
    env_file.write_text(f"VFSAPI_SECRET_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("VFSAPI_ENABLE_CONSOLE", raising=False)

    settings = config_mod.ApiSettings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert settings.enable_console is False
