"""The ngrok edge lets through every route the API serves, and nothing stale.

WHY THIS FILE EXISTS
--------------------
ngrok/traffic-policy.yml 404s any path not on its allowlist, at the edge,
before the request reaches this machine. That is the point of it — and it means
a router added to src/api/main.py works perfectly on 127.0.0.1 and returns
{"error":"not_found"} through the tunnel. Nothing fails locally; the web app
just sees a 404 that reads like a server bug.

That happened: /accounts, /booking, /config, /inbox, /payments, /pipeline and
/webhooks all shipped without an edge entry, PATCH was 405'd, and every
document upload was 413'd by a flat 64 KB cap. So this reads the policy file
and asserts it against the app's own route table.

The CEL is not evaluated by a real engine here — only the two shapes rule 1
uses (`path == '...'` and `path.startsWith('...')`) are parsed. A new shape in
that rule makes the parse come up short and the test fail, which is the right
direction to fail in.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

POLICY = REPO_ROOT / "ngrok" / "traffic-policy.yml"
CONFIG = REPO_ROOT / "ngrok" / "ngrok.yml"

_PATH_STUBS = {
    "{job_id}": "nonexistent-job",
    "{client_id}": "nonexistent-client",
    "{route}": "AE-CHE",
    "{registrant_id}": "nonexistent-client",
    "{email}": "nobody@example.com",
}


def _rules() -> list[str]:
    """The policy split into rules, comments stripped (one string per rule)."""
    lines = [ln for ln in POLICY.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    rules, current = [], []
    for line in lines:
        if line.startswith("  - "):
            if current:
                rules.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        rules.append("\n".join(current))
    return rules


def _allowlist() -> tuple[set[str], tuple[str, ...]]:
    allow_rule = _rules()[0]
    exact = set(re.findall(r"req\.url\.path == '([^']+)'", allow_rule))
    prefixes = tuple(re.findall(r"startsWith\('([^']+)'\)", allow_rule))
    return exact, prefixes


def _routes() -> list[tuple[str, str]]:
    # app.openapi(), not /openapi.json: the endpoint only exists with docs on,
    # and app.routes hides included routers behind lazy wrappers.
    from src.api.main import app

    routes = []
    for path, operations in app.openapi().get("paths", {}).items():
        for method in operations:
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                routes.append((method.upper(), path))
    return sorted(routes)


def _fill(path: str) -> str:
    for placeholder, value in _PATH_STUBS.items():
        path = path.replace(placeholder, value)
    return path


def test_every_api_route_passes_the_edge_path_filter():
    exact, prefixes = _allowlist()
    assert exact and prefixes, "Could not parse the allowlist rule."

    blocked = sorted({path for _, path in _routes()
                      if not (_fill(path) in exact or _fill(path).startswith(prefixes))})
    assert not blocked, (
        f"These routes are served by the API but 404'd at the ngrok edge: "
        f"{blocked}. Add them to rule 1 of ngrok/traffic-policy.yml, then run "
        f"`python ngrok/build_config.py`.")


def test_every_api_method_passes_the_edge_method_filter():
    method_rule = next(r for r in _rules() if "req.method in" in r)
    allowed = set(re.findall(r"'([A-Z]+)'", method_rule.split("req.method in", 1)[1]))

    missing = sorted({m for m, _ in _routes()} - allowed)
    assert not missing, f"Methods the API uses but the edge 405s: {missing}"


def test_the_edge_admits_a_full_size_document_upload():
    """The edge cap for uploads must be at least what the app itself accepts."""
    from src.api.main import documents_max_upload_bytes

    caps = [int(n) for n in re.findall(r"req\.content_length > (\d+)",
                                       POLICY.read_text(encoding="utf-8"))]
    assert max(caps) >= documents_max_upload_bytes()


def test_ngrok_yml_is_regenerated_from_the_policy():
    """ngrok.yml is what the agent reads; traffic-policy.yml is what people
    edit. An edit without `python ngrok/build_config.py` changes nothing live."""
    policy_body = [ln for ln in POLICY.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    config = CONFIG.read_text(encoding="utf-8")
    expected = "\n".join("      " + ln for ln in policy_body) + "\n"
    assert config.endswith(expected), (
        "ngrok/ngrok.yml is stale. Run `python ngrok/build_config.py`.")
