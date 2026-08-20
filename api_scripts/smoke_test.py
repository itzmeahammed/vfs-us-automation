"""End-to-end smoke test for the webhook API.

Starts the real app in-process (via FastAPI's TestClient), then exercises the
paths that matter: no token, wrong token, correct token, a genuinely spawned
subprocess, and the single-flight guard.

Run it:
    python api_scripts/smoke_test.py

It sets its own throwaway VFSAPI_SECRET_TOKEN, so it never touches your real
one and needs no server running.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Configure BEFORE importing the app — settings are read at import time.
TEST_TOKEN = "t" * 64
os.environ["VFSAPI_SECRET_TOKEN"] = TEST_TOKEN
os.environ["VFSAPI_JOB_COMMAND"] = (
    f'["{sys.executable.replace(chr(92), "/")}",'
    f'"{(REPO_ROOT / "api_scripts" / "placeholder_job.py").as_posix()}",'
    f'"--duration","2"]'
)

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402

HEADER = "X-Webhook-Secret-Token"
passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


def main() -> int:
    """Run every check. Returns a process exit code."""
    print("=" * 66)
    print("WEBHOOK API SMOKE TEST")
    print("=" * 66)

    with TestClient(app) as client:
        # -- Health: open, no token needed -------------------------------
        print("\n[1] Health endpoint (unauthenticated)")
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200, f"got {r.status_code}")
        check("reports ok", r.json().get("status") == "ok", str(r.json()))

        # -- Auth failures ------------------------------------------------
        print("\n[2] Authentication is enforced")
        r = client.post("/trigger/waitlist", json={})
        check("no header -> 401", r.status_code == 401, f"got {r.status_code}")

        r = client.post("/trigger/waitlist", json={}, headers={HEADER: "wrong-token"})
        check("wrong token -> 401", r.status_code == 401, f"got {r.status_code}")

        r = client.post("/trigger/waitlist", json={}, headers={HEADER: TEST_TOKEN[:-1]})
        check("near-miss token -> 401", r.status_code == 401, f"got {r.status_code}")

        body = r.json()
        check(
            "401 body leaks nothing",
            "invalid" in body.get("detail", "").lower()
            and TEST_TOKEN not in r.text,
            str(body),
        )

        r = client.get("/jobs")
        check("GET /jobs is protected", r.status_code == 401, f"got {r.status_code}")

        # -- Input validation ---------------------------------------------
        print("\n[3] Input validation")
        auth = {HEADER: TEST_TOKEN}
        r = client.post("/trigger/waitlist", json={"route": "not a route"}, headers=auth)
        check("bad route -> 422", r.status_code == 422, f"got {r.status_code}")

        r = client.post(
            "/trigger/waitlist",
            json={"registrant": "x; rm -rf /"},
            headers=auth,
        )
        check("injection-shaped input -> 422", r.status_code == 422, f"got {r.status_code}")

        r = client.post("/trigger/waitlist", json={"unexpected": 1}, headers=auth)
        check("unknown field -> 422", r.status_code == 422, f"got {r.status_code}")

        # Regression: real combo labels contain spaces and dashes, and real
        # routes are not always AA-BBB (AE-MT exists). An earlier slug-only
        # validator rejected both.
        r = client.post(
            "/trigger/waitlist",
            json={"route": "AE-MT", "combo": "Dubai - SCHENGEN", "dry_run": True},
            headers=auth,
        )
        check(
            "real combo label + 2-letter route accepted",
            r.status_code in (202, 409),
            f"got {r.status_code}: {r.text}",
        )
        if r.status_code == 202:
            cmd = r.json()["job"]["command"]
            check(
                "combo label reached argv intact",
                "Dubai - SCHENGEN" in cmd,
                str(cmd),
            )
            # Free the single-flight slot for the next section.
            client.post(f"/jobs/{r.json()['job']['job_id']}/cancel", headers=auth)

        # -- The real trigger ---------------------------------------------
        print("\n[4] Triggering a real background job")
        started = time.monotonic()
        r = client.post(
            "/trigger/waitlist",
            json={"route": "AE-DEU", "dry_run": True, "reason": "smoke test"},
            headers=auth,
        )
        elapsed = time.monotonic() - started
        check("valid trigger -> 202", r.status_code == 202, f"got {r.status_code}: {r.text}")

        # The job sleeps 2s; a non-blocking response must beat that comfortably.
        check(
            f"responded without waiting for the job ({elapsed:.2f}s < 2s)",
            elapsed < 2.0,
            f"took {elapsed:.2f}s — the endpoint appears to be blocking",
        )

        job = r.json()["job"]
        job_id = job["job_id"]
        check("status is running", job["status"] == "running", job["status"])
        check("has a real pid", isinstance(job["pid"], int) and job["pid"] > 0, str(job["pid"]))
        check("--route reached argv", "AE-DEU" in job["command"], str(job["command"]))
        check("--dry-run reached argv", "--dry-run" in job["command"], str(job["command"]))

        # -- Single flight -------------------------------------------------
        print("\n[5] Single-flight guard")
        r = client.post("/trigger/waitlist", json={}, headers=auth)
        check("second concurrent trigger -> 409", r.status_code == 409, f"got {r.status_code}")

        # -- Completion ----------------------------------------------------
        print("\n[6] Job completion")
        final = {}
        for _ in range(40):  # up to ~20s
            time.sleep(0.5)
            final = client.get(f"/jobs/{job_id}", headers=auth).json()
            if final["status"] != "running":
                break
        check("job finished", final.get("status") != "running", str(final.get("status")))
        check("succeeded", final.get("status") == "succeeded", str(final))
        check("exit code 0", final.get("exit_code") == 0, str(final.get("exit_code")))

        log_file = final.get("log_file")
        check("log file exists", bool(log_file) and Path(log_file).exists(), str(log_file))
        if log_file and Path(log_file).exists():
            content = Path(log_file).read_text(encoding="utf-8", errors="replace")
            check(
                "secret absent from job environment",
                "OK: API secret is not present" in content,
                "the child process could see VFSAPI_SECRET_TOKEN",
            )
            check("job logged completion", "COMPLETED SUCCESSFULLY" in content, "")

        # -- History + 404 --------------------------------------------------
        print("\n[7] Job history")
        r = client.get("/jobs", headers=auth)
        check("GET /jobs -> 200", r.status_code == 200, f"got {r.status_code}")
        check("history has our job", r.json()["count"] >= 1, str(r.json()["count"]))

        r = client.get("/jobs/does-not-exist", headers=auth)
        check("unknown job -> 404", r.status_code == 404, f"got {r.status_code}")

        # -- Hardening headers ----------------------------------------------
        print("\n[8] Security headers and docs")
        r = client.get("/health")
        check("nosniff present", r.headers.get("X-Content-Type-Options") == "nosniff", "")
        check("frame-deny present", r.headers.get("X-Frame-Options") == "DENY", "")
        r = client.get("/openapi.json")
        check("OpenAPI schema disabled", r.status_code == 404, f"got {r.status_code}")

    print("\n" + "=" * 66)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
