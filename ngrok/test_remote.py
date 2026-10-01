"""Verify the tunnel from the OUTSIDE, the way your web app will call it.

Run this from anywhere with the public URL — ideally from a different machine
or network, because that is the only way to prove the tunnel really is
reachable rather than that loopback happens to work.

    python ngrok/test_remote.py https://abc123.ngrok-free.app

The token is read from --token, $VFSAPI_SECRET_TOKEN, or ./.env.api.

The check that matters most is the LAST one: an unauthenticated request must be
refused. Everything else failing is an inconvenience; that one failing means
the API is exposed to the internet without auth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Free ngrok injects an HTML interstitial on browser-ish requests; this header
# suppresses it. Harmless on paid plans and on other tunnels.
BASE_HEADERS = {"ngrok-skip-browser-warning": "true",
                "User-Agent": "vfs-tunnel-test/1.0"}


def load_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit.strip()
    if os.environ.get("VFSAPI_SECRET_TOKEN"):
        return os.environ["VFSAPI_SECRET_TOKEN"].strip()
    env_file = REPO_ROOT / ".env.api"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("VFSAPI_SECRET_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def call(url: str, token: str | None = None, method: str = "GET",
         body: dict | None = None, timeout: float = 20.0):
    """Returns (status, parsed_body_or_text). Never raises on an HTTP error."""
    headers = dict(BASE_HEADERS)
    data = None
    if token:
        headers["X-Webhook-Secret-Token"] = token
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:                                  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", help="Public tunnel URL, e.g. https://x.ngrok-free.app")
    ap.add_argument("--token", default=None)
    ap.add_argument("--trigger", action="store_true",
                    help="Also fire a DRY-RUN waitlist trigger.")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    token = load_token(args.token)
    if not token:
        print("No token found (--token / $VFSAPI_SECRET_TOKEN / .env.api).")
        return 2

    passed = failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [ok]   {name}" + (f" — {detail}" if detail else ""))
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print(f"\nTesting {base}\n")

    # 1. Reachability, no auth needed.
    status, body = call(f"{base}/v1/health")
    check("/v1/health reachable", status == 200 and isinstance(body, dict)
          and body.get("status") == "ok", f"status={status}")
    if status is None:
        print(f"\n  Could not reach the tunnel at all: {body}")
        print("  Is the agent running? Is the URL current?")
        return 1

    # 2. HTML instead of JSON means the ngrok interstitial got through.
    check("/v1/health returns JSON, not the ngrok interstitial",
          isinstance(body, dict),
          "send 'ngrok-skip-browser-warning: true'" if not isinstance(body, dict) else "")

    # 3. One read per module. A 404 whose body is exactly {"error":"not_found"}
    #    came from the ngrok EDGE (the app's own errors are the /v1 envelope),
    #    so the path is missing from ngrok/traffic-policy.yml.
    for path in ("/v1/health/ready", "/v1/switches", "/v1/clients?limit=1",
                 "/v1/routes", "/v1/waitlist/status", "/v1/booking/status",
                 "/v1/booking/invitations", "/v1/booking/payments/unanswered",
                 "/v1/jobs?limit=1", "/v1/accounts", "/v1/overview",
                 "/v1/notifications/webhook/deadletters"):
        status, body = call(f"{base}{path}", token=token)
        edge_blocked = body == {"error": "not_found"}
        ok = status == 200 or (path == "/v1/health/ready" and status == 503)
        check(f"GET {path}", ok,
              f"status={status}"
              + (" — blocked at the ngrok edge; see traffic-policy.yml"
                 if edge_blocked else ""))

    # 4. PATCH reaches the app. A nonexistent client, so nothing changes: the
    #    app answers 404, whereas an edge that does not allow PATCH answers 405.
    status, body = call(f"{base}/v1/clients/tunnel-test-nonexistent", token=token,
                        method="PATCH", body={"city": "x"})
    check("PATCH passes the edge", status not in (405, None)
          and body != {"error": "method_not_allowed"}, f"status={status}")

    # 5. Old paths are gone, refused at the edge.
    status, _ = call(f"{base}/clients", token=token)
    check("pre-/v1 paths are refused", status == 404, f"status={status}")

    # 6. Optional dry-run registration.
    if args.trigger:
        status, body = call(f"{base}/v1/waitlist/runs", token=token, method="POST",
                            body={"route": "AE-CHE", "dry_run": True,
                                  "reason": "tunnel connectivity test"})
        job = body.get("job", {}) if isinstance(body, dict) else {}
        check("POST /v1/waitlist/runs (dry run)", status in (202, 409),
              f"status={status} job={job.get('job_id')}"
              + (" (409 = a job is already running, which is fine)"
                 if status == 409 else ""))

    # --- The one that must never fail --------------------------------------
    print()
    status, _ = call(f"{base}/v1/clients")                   # deliberately no token
    check("UNAUTHENTICATED /v1/clients is refused", status in (401, 403, 404),
          f"status={status}"
          + ("  <-- THE API IS EXPOSED WITHOUT AUTH. Stop the tunnel."
             if status == 200 else ""))

    status, _ = call(f"{base}/v1/clients", token="wrong-" + "x" * 60)
    check("a WRONG token is refused", status in (401, 403, 404), f"status={status}")

    print(f"\nRESULT: {passed} passed, {failed} failed\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
