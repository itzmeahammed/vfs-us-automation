"""Tests for the operational status endpoints (Phase 5)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "e" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient

    from src.api.security import _reset_rate_limiter
    _reset_rate_limiter()

    from src.api.main import app
    with TestClient(app) as client:
        yield client


def test_status_requires_auth(api):
    assert api.get("/status").status_code == 401
    assert api.get("/status/dangling").status_code == 401
    assert api.post("/status/resolve", json={}).status_code == 401


def test_status_reports_switches(api):
    r = api.get("/status", headers=HEADERS)
    assert r.status_code == 200
    switches = r.json()["switches"]
    for key in ("register_enabled", "dry_run", "auto_trigger_enabled",
                "auto_trigger_dry_run", "max_per_run", "max_per_day"):
        assert key in switches


def test_posture_is_plain_language(api):
    """An operator should not have to reason about four booleans."""
    posture = api.get("/status", headers=HEADERS).json()["posture"]
    assert any(word in posture for word in ("PARKED", "MANUAL", "AUTO"))


def test_posture_reflects_the_switches(monkeypatch):
    """Each combination gets its own honest description."""
    from src.api import status as status_mod
    from src.api.schemas import SwitchState

    parked = SwitchState(register_enabled=False, dry_run=True,
                         auto_trigger_enabled=True, auto_trigger_dry_run=False,
                         max_per_run=1, max_per_day=5)
    assert "PARKED" in status_mod._describe_posture(parked)

    manual = SwitchState(register_enabled=True, dry_run=False,
                         auto_trigger_enabled=False, auto_trigger_dry_run=False,
                         max_per_run=1, max_per_day=5)
    assert "MANUAL" in status_mod._describe_posture(manual)

    auto_dry = SwitchState(register_enabled=True, dry_run=False,
                           auto_trigger_enabled=True, auto_trigger_dry_run=True,
                           max_per_run=1, max_per_day=5)
    assert "DRY RUN" in status_mod._describe_posture(auto_dry)

    live = SwitchState(register_enabled=True, dry_run=False,
                       auto_trigger_enabled=True, auto_trigger_dry_run=False,
                       max_per_run=1, max_per_day=5)
    described = status_mod._describe_posture(live)
    assert "LIVE" in described
    # The riskiest posture must say so unambiguously.
    assert "without a human" in described


def test_status_lists_routes(api):
    routes = api.get("/status", headers=HEADERS).json()["routes"]
    assert routes, "no routes reported"
    by_id = {r["route"]: r for r in routes}
    assert "AE-CHE" in by_id
    assert by_id["AE-CHE"]["ready"] is True
    assert by_id["AE-CHE"]["combos"]


def test_not_ready_routes_explain_themselves(api):
    """A route that cannot register must say why, not just report false."""
    routes = api.get("/status", headers=HEADERS).json()["routes"]
    for route in routes:
        if not route["ready"]:
            assert route["problems"], f"{route['route']} is not ready but gives no reason"


def test_dangling_endpoint_returns_a_list(api):
    r = api.get("/status/dangling", headers=HEADERS)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_dangling_entries_surface_in_status(api, monkeypatch):
    """An unresolved submit must be visible — it silently parks a client."""
    monkeypatch.setattr("src.waitlist.journal.dangling", lambda: [{
        "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
        "registrant_id": "stuck-client", "status": "unknown",
        "reason": "confirmation could not be read",
        "started_at": "2026-08-19T10:00:00",
    }])
    body = api.get("/status", headers=HEADERS).json()
    assert body["needs_attention"] is True
    assert body["dangling"][0]["registrant_id"] == "stuck-client"
    assert body["dangling"][0]["status"] == "unknown"


def test_resolve_rejects_an_ambiguous_status(api):
    """Resolving replaces ambiguity with fact — 'pending' would defeat that."""
    r = api.post("/status/resolve", headers=HEADERS, json={
        "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
        "registrant_id": "x", "status": "pending",
    })
    assert r.status_code == 422


def test_resolve_rejects_unknown_fields(api):
    r = api.post("/status/resolve", headers=HEADERS, json={
        "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
        "registrant_id": "x", "status": "success", "surprise": 1,
    })
    assert r.status_code == 422


def test_resolve_accepts_a_definite_outcome(api, monkeypatch):
    from src.waitlist.result import Status, WaitlistResult

    captured = {}

    def fake_resolve(route, combo, registrant_id, status, reason=""):
        captured.update(route=route, combo=combo, registrant_id=registrant_id,
                        status=status, reason=reason)
        return WaitlistResult(route=route, combo=combo,
                              registrant_id=registrant_id, status=Status.SUCCESS)

    monkeypatch.setattr("src.waitlist.journal.resolve", fake_resolve)
    r = api.post("/status/resolve", headers=HEADERS, json={
        "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
        "registrant_id": "x", "status": "success",
        "reason": "verified on the portal",
    })
    assert r.status_code == 200
    assert r.json()["resolved"] is True
    assert captured["status"] == "success"
    assert captured["registrant_id"] == "x"


# --------------------------------------------------------------------------
# Security headers vs. the Swagger UI
# --------------------------------------------------------------------------


def test_api_responses_keep_the_strict_csp(api):
    """JSON endpoints must never relax the policy."""
    r = api.get("/health")
    assert r.headers["Content-Security-Policy"] == \
        "default-src 'none'; frame-ancestors 'none'"


def test_docs_csp_allows_the_swagger_cdn(monkeypatch):
    """Regression: `default-src 'none'` rendered /docs as a BLANK page.

    Swagger UI loads its CSS/JS from cdn.jsdelivr.net. The strict policy blocked
    them, so the HTML arrived with HTTP 200 but nothing painted — which reads as
    a broken server rather than a deliberate policy. The docs routes get a
    widened policy; every other route keeps the strict one.
    """
    import importlib

    monkeypatch.setenv("VFSAPI_ENABLE_DOCS", "1")
    # The flag now comes from ApiSettings, which is lru_cached — without this
    # the reload re-reads the settings object built BEFORE the env var was set.
    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()
    import src.api.main as main_mod
    reloaded = importlib.reload(main_mod)

    from fastapi.testclient import TestClient
    try:
        with TestClient(reloaded.app) as client:
            docs = client.get("/docs")
            assert docs.status_code == 200
            csp = docs.headers["Content-Security-Policy"]
            assert "cdn.jsdelivr.net" in csp, "the Swagger CDN is still blocked"
            assert "script-src" in csp and "style-src" in csp

            # ...but an API route must NOT inherit the relaxed policy.
            assert reloaded.app and client.get("/health").headers[
                "Content-Security-Policy"
            ] == "default-src 'none'; frame-ancestors 'none'"
    finally:
        # Restore the module to its docs-disabled state for other tests.
        # Clearing the settings cache is load-bearing: leaving a docs-enabled
        # ApiSettings cached makes the NEXT test see /docs as enabled and fail.
        monkeypatch.delenv("VFSAPI_ENABLE_DOCS", raising=False)
        import src.api.config as config_mod
        config_mod.get_settings.cache_clear()
        importlib.reload(main_mod)


def test_docs_are_disabled_by_default():
    """Off unless explicitly enabled — an exposed schema maps the API.

    Asserts the DEFAULT on the settings object rather than hitting the app,
    because the developer's own .env.api may legitimately enable docs locally.
    A test that fails because the operator turned a feature on is testing the
    machine, not the code.
    """
    import src.api.config as config_mod

    fields = config_mod.ApiSettings.model_fields
    assert fields["enable_docs"].default is False
    assert fields["enable_console"].default is False


def test_openapi_declares_the_security_scheme(monkeypatch):
    """Regression: /docs had no "Authorize" button.

    The token was declared as a plain Header() parameter, which documents the
    header but gives Swagger UI nothing to authorise WITH — so every "Try it
    out" from the docs page returned 401 and there was no way to fix it.

    APIKeyHeader declares it as a real security scheme, which both renders the
    button and tells generated clients how to authenticate.
    """
    import importlib

    monkeypatch.setenv("VFSAPI_ENABLE_DOCS", "1")
    # The flag now comes from ApiSettings, which is lru_cached — without this
    # the reload re-reads the settings object built BEFORE the env var was set.
    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()
    import src.api.main as main_mod
    reloaded = importlib.reload(main_mod)

    from fastapi.testclient import TestClient
    try:
        with TestClient(reloaded.app) as client:
            spec = client.get("/openapi.json").json()

            schemes = spec.get("components", {}).get("securitySchemes", {})
            assert "APIKeyHeader" in schemes, "no security scheme — no Authorize button"
            assert schemes["APIKeyHeader"]["in"] == "header"
            assert schemes["APIKeyHeader"]["name"] == "X-Webhook-Secret-Token"

            # Protected endpoints must reference it...
            assert spec["paths"]["/status"]["get"].get("security")
            assert spec["paths"]["/clients"]["post"].get("security")
            # ...and /health must stay public.
            assert not spec["paths"]["/health"]["get"].get("security")
    finally:
        monkeypatch.delenv("VFSAPI_ENABLE_DOCS", raising=False)
        importlib.reload(main_mod)


def test_auth_behaviour_unchanged_by_the_security_scheme(api):
    """Switching to APIKeyHeader must not alter the responses.

    auto_error=False keeps our handler: FastAPI's default would send its own 403
    and distinguish missing from wrong — the disclosure the opaque 401 avoids.
    """
    for headers in ({}, {"X-Webhook-Secret-Token": "wrong-token"}):
        r = api.get("/status", headers=headers)
        assert r.status_code == 401
        assert r.json()["detail"] == \
            "Unauthorized: missing or invalid authentication token."
