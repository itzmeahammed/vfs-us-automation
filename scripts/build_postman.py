"""Build a Postman collection for the webhook API from the API itself.

    python -m scripts.build_postman          # -> postman/*.json

Generated, not hand-written: every route, query parameter and response shape
comes from app.openapi(), so a new endpoint lands in the collection the next
time this runs. What the schema cannot supply is a SENSIBLE body — the one to
click in Postman — so those are curated in EXAMPLE_BODIES below, and every one
of them is safe to send: triggers are dry runs, booking is a read-only probe.

Writes three files:
    postman/VFS-API.postman_collection.json   every endpoint, grouped by tag
    postman/VFS-API.postman_environment.json  baseUrl + an EMPTY token (tracked)
    postman/VFS-API.local.postman_environment.json
                                              the same, with the real token and
                                              the live tunnel URL (gitignored)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "postman"
NAME = "VFS Local Trigger API"

#: Folder order in Postman — roughly the order a web UI is built in.
TAG_ORDER = ["system", "clients", "catalog", "waitlist", "booking", "jobs",
             "accounts", "notifications"]
TAG_TITLES = {"system": "System & switches", "clients": "Clients (both flows)",
              "catalog": "Routes & combos", "waitlist": "Waitlist (Flow 1)",
              "booking": "Booking runs & invitations", "jobs": "Jobs",
              "accounts": "VFS accounts", "notifications": "Notifications"}

#: Path parameter -> collection variable. Saved by the test scripts below, so
#: "Create Client" then "Get Client" works without copying ids around.
PATH_VARS = {
    "client_id": ("clientId", "u10432-nor-1"),
    "job_id": ("jobId", ""),
    "route": ("route", "AE-NOR"),
    "email": ("accountEmail", "waitlist-acc1@example.com"),
    "key": ("inviteKey", ""),
}

_APPLICANT = {
    "first_name": "AHMED", "last_name": "KHAN",
    "passport_number": "A1234567", "date_of_birth": "1990-04-12",
    "nationality": "India", "gender": "Male",
    "phone_country_code": "971", "phone_number": "501234567",
    "email": "ahmed@example.com",
    "address_line_1": "FLAT 101, AL BARSHA TOWER",
    "address_line_2": "SHEIKH ZAYED ROAD",
    "city": "Dubai", "postcode": "00000", "country_code": "AE",
    "account": "booking-acc@example.com", "account_password": "the-password",
}

_LIVE_CLIENT = {
    "client_id": "{{clientId}}", "flow": "live", "route": "AE-NOR",
    "combo": "Norway Visa Application Center - Dubai - Tourist",
    "date_from": "2026-10-10", "date_to": "2026-10-20",
    "enabled": False, **_APPLICANT,
}

#: (METHOD, path) -> JSON body. Safe by construction: nothing here books, pays,
#: or submits a live registration.
EXAMPLE_BODIES: Dict[tuple, Any] = {
    ("POST", "/v1/clients"): _LIVE_CLIENT,
    ("PUT", "/v1/clients/{client_id}"): {k: v for k, v in _LIVE_CLIENT.items()
                                         if k != "client_id"},
    ("PATCH", "/v1/clients/{client_id}"): {"date_from": "2026-10-12",
                                           "date_to": "2026-10-25"},
    ("POST", "/v1/clients/{client_id}/resolve"): {
        "outcome": "booked", "reason": "confirmed on the VFS account",
        "appointment_date": "2026-10-12", "appointment_time": "09:30",
        "reference": "NOR/DXB/123456"},
    ("POST", "/v1/booking/runs"): {"client_id": "{{clientId}}", "mode": "probe",
                                   "reason": "Postman test"},
    ("POST", "/v1/booking/invitations/{key}/resolve"): {
        "outcome": "not_booked", "reason": "checked the account"},
    ("POST", "/v1/waitlist/runs"): {
        "route": "AE-CHE", "registrant": "u10432-che", "dry_run": True,
        "reason": "Postman test"},
    ("POST", "/v1/waitlist/dangling/resolve"): {
        "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
        "registrant_id": "u10432-che", "status": "success",
        "reason": "verified on the VFS account by hand"},
    ("POST", "/v1/waitlist/reconcile"): {"dry_run": True},
    ("PATCH", "/v1/switches"): {"test_booking": True},
    ("POST", "/v1/accounts/{email}/bench"): {
        "route": "{{route}}", "hours": 2, "reason": "benched via Postman"},
}

#: Extra warnings prepended to a request's description.
NOTES = {
    ("POST", "/v1/clients"):
        "Pick `flow`: `live` books a live slot inside date_from..date_to AND "
        "PAYS; `waitlist` joins the VFS waitlist and books on invitation. SAFE "
        "AS SHIPPED: `enabled: false` stores it parked. A 422 lists every "
        "missing field for the chosen route and flow.",
    ("POST", "/v1/clients/{client_id}/enable"):
        "ARMS the client. A live client is booked and paid automatically when "
        "a slot inside its window is seen (if [switches] live_booking is on).",
    ("DELETE", "/v1/clients/{client_id}"):
        "Deletes the client file, including their passport data.",
    ("POST", "/v1/booking/runs"):
        "SAFE AS SHIPPED: `mode: probe` logs in and reads. `walk` fills every "
        "page and stops before paying. `commit` BOOKS AND PAYS and needs "
        "`confirm` equal to the route. Send an Idempotency-Key with commit.",
    ("POST", "/v1/waitlist/runs"):
        "SAFE AS SHIPPED: `dry_run: true`. false submits a REAL registration.",
    ("PATCH", "/v1/switches"):
        "Master switches. Applies from the NEXT slot-check run (every 30 min).",
    ("GET", "/v1/jobs/{job_id}/stream"):
        "Server-Sent Events (`log`, `status`, `end`). In a browser use fetch + "
        "a stream reader; EventSource cannot send the auth header.",
    ("POST", "/v1/notifications/telegram/test"):
        "Sends a real message to the testing-bot chat.",
    ("POST", "/v1/notifications/webhook/test"):
        "Sends a real test event to the configured web-app webhook.",
}

#: Test script per (METHOD, path): capture ids for the requests that follow.
SAVE_JOB_ID = [
    "const b = pm.response.json();",
    "if (b && b.job && b.job.job_id) {",
    "  pm.collectionVariables.set('jobId', b.job.job_id);",
    "  console.log('jobId =', b.job.job_id);",
    "}",
]
TESTS = {
    ("POST", "/v1/booking/runs"): SAVE_JOB_ID,
    ("POST", "/v1/waitlist/runs"): SAVE_JOB_ID,
    ("POST", "/v1/clients"): [
        "const b = pm.response.json();",
        "if (b && b.client_id) pm.collectionVariables.set('clientId', b.client_id);",
    ],
    ("GET", "/v1/jobs"): [
        "const b = pm.response.json();",
        "if (b && b.items && b.items.length && !pm.collectionVariables.get('jobId'))",
        "  pm.collectionVariables.set('jobId', b.items[0].job_id);",
    ],
    ("GET", "/v1/booking/invitations"): [
        "const b = pm.response.json();",
        "if (b && b.items && b.items.length && !pm.collectionVariables.get('inviteKey'))",
        "  pm.collectionVariables.set('inviteKey', b.items[0].key);",
    ],
}

#: Runs before every request. The ngrok header is sent everywhere because the
#: free tier answers anything without it with an HTML page, not JSON.
PRE_REQUEST = [
    "pm.request.headers.upsert({key: 'ngrok-skip-browser-warning', value: 'true'});",
    # Who is acting — recorded in GET /v1/audit. Set `actor` in the environment.
    "pm.request.headers.upsert({key: 'X-Actor', value: pm.variables.get('actor') || 'postman'});",
    "if (!pm.environment.get('baseUrl') && !pm.collectionVariables.get('baseUrl')) {",
    "  throw new Error('Select the \"VFS API\" environment (top right) - baseUrl is unset.');",
    "}",
    # An empty jobId turns GET /jobs/{{jobId}} into GET /jobs/ — which answers
    # 200 with the job LIST, a plausible-looking wrong response.
    "if (pm.request.url.toString().includes('{{jobId}}') && !pm.variables.get('jobId')) {",
    "  throw new Error('jobId is empty - send a trigger or List Jobs first.');",
    "}",
]
#: Runs after every request.
COLLECTION_TESTS = [
    "const ct = pm.response.headers.get('Content-Type') || '';",
    "pm.test('Response is not the ngrok HTML interstitial', function () {",
    "  pm.expect(ct).to.not.include('text/html');",
    "});",
    "if (pm.response.code === 404) {",
    "  try {",
    "    const b = pm.response.json();",
    "    if (b && b.error === 'not_found' && !b.detail)",
    "      console.warn('404 came from the ngrok EDGE, not the API - the path is '",
    "        + 'missing from ngrok/traffic-policy.yml, or the tunnel URL is stale.');",
    "  } catch (e) {}",
    "}",
]


# --------------------------------------------------------------------------- #
# Schema -> example
# --------------------------------------------------------------------------- #


def _example(schema: Dict[str, Any], comps: Dict[str, Any], depth: int = 0) -> Any:
    """A representative value for a JSON schema. For response previews only —
    it shows the web UI developer the SHAPE, not real data."""
    if depth > 6:
        return None
    if "$ref" in schema:
        return _example(comps[schema["$ref"].split("/")[-1]], comps, depth + 1)
    for key in ("examples",):
        if schema.get(key):
            return schema[key][0]
    if "example" in schema:
        return schema["example"]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"]
            return _example(options[0], comps, depth + 1) if options else None
    if "allOf" in schema:
        return _example(schema["allOf"][0], comps, depth + 1)

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not props and isinstance(schema.get("additionalProperties"), dict):
            return {"key": _example(schema["additionalProperties"], comps, depth + 1)}
        return {name: _example(sub, comps, depth + 1) for name, sub in props.items()}
    if kind == "array":
        item = _example(schema.get("items", {}), comps, depth + 1)
        return [] if item is None else [item]
    if kind == "string":
        fmt = schema.get("format")
        return {"date-time": "2026-09-29T08:00:00+00:00",
                "date": "2026-09-29"}.get(fmt, "string")
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return None


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def _url(path: str, params: List[Dict[str, Any]]) -> Dict[str, Any]:
    postman_path = path
    for name, (var, _) in PATH_VARS.items():
        postman_path = postman_path.replace("{" + name + "}", "{{" + var + "}}")

    query = []
    for p in params:
        if p.get("in") != "query":
            continue
        schema = p.get("schema", {})
        value = _example(schema, {}) if "default" in schema else ""
        query.append({
            "key": p["name"],
            "value": "" if value is None else str(value).lower()
            if isinstance(value, bool) else str(value),
            "description": p.get("description", ""),
            # Optional filters start unticked, so a request sends exactly what
            # the endpoint does with no arguments until you opt in.
            "disabled": not p.get("required", False),
        })

    segments = [s for s in postman_path.strip("/").split("/") if s]
    raw = "{{baseUrl}}" + postman_path
    if any(not q["disabled"] for q in query):
        raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in query
                              if not q["disabled"])
    url: Dict[str, Any] = {"raw": raw, "host": ["{{baseUrl}}"], "path": segments}
    if query:
        url["query"] = query
    return url


def _item(method: str, path: str, op: Dict[str, Any],
          comps: Dict[str, Any]) -> Dict[str, Any]:
    key = (method, path)
    headers: List[Dict[str, Any]] = []
    for p in op.get("parameters", []):
        if p.get("in") == "header" and p["name"] == "Idempotency-Key":
            headers.append({
                "key": "Idempotency-Key", "value": "{{$guid}}",
                "description": "A fresh UUID per send. Re-use one value on a "
                               "retry to get the original job back instead "
                               "of a second run.",
            })

    body: Optional[Dict[str, Any]] = None
    content = op.get("requestBody", {}).get("content", {})
    if "multipart/form-data" in content:
        body = {"mode": "formdata", "formdata": [
            {"key": "file", "type": "file", "src": [],
             "description": "PNG, JPG or PDF, under 2 MB."},
            {"key": "kind", "value": "passport_bio", "type": "text"},
        ]}
    elif "application/json" in content:
        example = EXAMPLE_BODIES.get(key)
        if example is None:
            example = _example(content["application/json"]["schema"], comps)
        headers.append({"key": "Content-Type", "value": "application/json"})
        body = {"mode": "raw", "raw": json.dumps(example, indent=2),
                "options": {"raw": {"language": "json"}}}

    description = op.get("description") or ""
    if key in NOTES:
        description = f"**{NOTES[key]}**\n\n{description}"

    request: Dict[str, Any] = {
        "method": method,
        "header": headers,
        "url": _url(path, op.get("parameters", [])),
        "description": description.strip(),
    }
    if body:
        request["body"] = body
    if path == "/health":
        request["auth"] = {"type": "noauth"}

    item: Dict[str, Any] = {"name": op.get("summary") or f"{method} {path}",
                            "request": request, "response": []}

    # A saved example response per endpoint: the shape a web UI will parse.
    responses = op.get("responses", {})
    ok = next((code for code in responses if code.startswith("2")), None)
    if ok and path != "/jobs/{job_id}/stream":
        schema = responses[ok].get("content", {}).get("application/json", {}).get("schema")
        if schema:
            item["response"].append({
                "name": f"{ok} example (shape only)",
                "originalRequest": {k: v for k, v in request.items()
                                    if k != "description"},
                "status": "OK", "code": int(ok),
                "_postman_previewlanguage": "json",
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": json.dumps(_example(schema, comps), indent=2),
            })

    if key in TESTS:
        item["event"] = [{"listen": "test",
                          "script": {"type": "text/javascript", "exec": TESTS[key]}}]
    return item


def build_collection(spec: Dict[str, Any]) -> Dict[str, Any]:
    comps = spec.get("components", {}).get("schemas", {})
    folders: Dict[str, List[Dict[str, Any]]] = {}
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            # /v1 only: the pre-/v1 paths still work but are deprecated, and a
            # collection people build a web app from must not teach them.
            if op.get("deprecated") or not path.startswith("/v1"):
                continue
            tag = (op.get("tags") or ["other"])[0]
            folders.setdefault(tag, []).append(_item(method.upper(), path, op, comps))

    ordered = sorted(folders, key=lambda t: (TAG_ORDER.index(t)
                                             if t in TAG_ORDER else 99, t))
    return {
        "info": {
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "vfs-local-trigger-api")),
            "name": NAME,
            "description": (
                "Generated by `python -m scripts.build_postman` from the API's "
                "own route table — regenerate rather than hand-edit.\n\n"
                "1. Import this and an environment file from `postman/`.\n"
                "2. Select the **VFS API** environment; set `baseUrl` to the "
                "tunnel URL (changes on every free-tier restart) and `token` "
                "to VFSAPI_SECRET_TOKEN from `.env.api`.\n"
                "3. Start with **System → Readiness**, then **Clients → List**.\n\n"
                "This is the /v1 API only. The pre-/v1 paths still answer, "
                "with a `Deprecation: true` header, but are not listed here.\n\n"
                "Auth is set once on the collection (X-Webhook-Secret-Token). "
                "A pre-request script adds `ngrok-skip-browser-warning` and "
                "`X-Actor` (the environment's `actor`, recorded in "
                "GET /v1/audit). Your web app sends all three — from its "
                "SERVER, never the browser bundle — with X-Actor set to the "
                "signed-in agent.\n\n"
                "Every error is one envelope: "
                "{\"error\": {code, message, status, problems[], request_id}}. "
                "Lists are paginated: ?limit=&offset= → {items, total, "
                "next_offset}.\n\n"
                "Runs save `jobId`, Create Client saves `clientId`, List "
                "Invitations saves `inviteKey`.\n\n"
                "Rate limit: 120 authenticated requests/min per caller IP."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {"type": "apikey", "apikey": [
            {"key": "key", "value": "X-Webhook-Secret-Token", "type": "string"},
            {"key": "value", "value": "{{token}}", "type": "string"},
            {"key": "in", "value": "header", "type": "string"},
        ]},
        "event": [
            {"listen": "prerequest",
             "script": {"type": "text/javascript", "exec": PRE_REQUEST}},
            {"listen": "test",
             "script": {"type": "text/javascript", "exec": COLLECTION_TESTS}},
        ],
        "variable": [{"key": var, "value": default, "type": "string"}
                     for var, default in PATH_VARS.values()],
        "item": [{"name": TAG_TITLES.get(tag, tag.capitalize()),
                  "item": folders[tag]} for tag in ordered],
    }


def build_environment(base_url: str, token: str) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "vfs-local-trigger-api-env")),
        "name": "VFS API",
        "values": [
            {"key": "baseUrl", "value": base_url, "type": "default", "enabled": True},
            {"key": "token", "value": token, "type": "secret", "enabled": True},
            {"key": "actor", "value": "postman", "type": "default", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }


def _live_tunnel_url() -> Optional[str]:
    """The current public URL from the local ngrok agent, if one is running."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            tunnels = json.load(r).get("tunnels", [])
    except Exception:                                  # noqa: BLE001
        return None
    return next((t["public_url"] for t in tunnels if t.get("proto") == "https"), None)


def main() -> int:
    has_env_file = (REPO_ROOT / ".env.api").is_file()
    if not has_env_file:
        # Only so the app imports. Never set when .env.api exists: an env var
        # outranks the file in pydantic-settings and would mask the real token.
        os.environ.setdefault("VFSAPI_SECRET_TOKEN", "x" * 64)
    logging.disable(logging.INFO)
    from src.api.core.config import get_settings
    from src.api.main import app

    spec = app.openapi()
    collection = build_collection(spec)
    OUT_DIR.mkdir(exist_ok=True)

    def write(name: str, data: Dict[str, Any]) -> Path:
        path = OUT_DIR / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    count = sum(len(f["item"]) for f in collection["item"])
    print(f"Wrote {write('VFS-API.postman_collection.json', collection)} "
          f"({count} requests)")
    print(f"Wrote {write('VFS-API.postman_environment.json', build_environment('http://127.0.0.1:8000', ''))}")

    live = _live_tunnel_url()
    token = get_settings().secret_token if has_env_file else ""
    local = write("VFS-API.local.postman_environment.json",
                  build_environment(live or "http://127.0.0.1:8000", token))
    print(f"Wrote {local} (gitignored; baseUrl={live or 'http://127.0.0.1:8000'}"
          f", token={'set' if token else 'EMPTY'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
