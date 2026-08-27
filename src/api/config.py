"""Typed settings for the webhook API.

Mirrors the bot's own settings.py philosophy — validate once at startup, fail
fast with a clear error — but uses its own `VFSAPI_` env prefix so it can never
collide with the bot's `VFSCFG_` knobs.

The shared secret is READ FROM THE ENVIRONMENT ONLY. It is never given a
default, never read from a tracked .ini, and never logged. A missing or short
secret is a startup failure, not a warning: an unauthenticated webhook that is
about to be exposed through a public tunnel is worse than no webhook at all.

Set it (PowerShell, current session):
    $env:VFSAPI_SECRET_TOKEN = "<64-hex-chars>"
Set it (PowerShell, persisted for your user):
    [Environment]::SetEnvironmentVariable("VFSAPI_SECRET_TOKEN", "<token>", "User")
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = .../vfs-malta-slot-checker  (this file is src/api/config.py)
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Minimum acceptable shared-secret length. 32 chars is ~128 bits of entropy when
# generated with secrets.token_hex(16); we ask for a bit more headroom because
# this token is the ONLY thing standing between the public internet and a
# subprocess on your desktop.
MIN_TOKEN_LENGTH: int = 32


class ApiSettings(BaseSettings):
    """Everything the webhook server needs, validated at import time."""

    model_config = SettingsConfigDict(
        env_prefix="VFSAPI_",
        env_file=str(REPO_ROOT / ".env.api"),   # optional; gitignored
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Security -----------------------------------------------------------
    # No default on purpose: pydantic raises if it is unset.
    secret_token: str = Field(
        ...,
        description="Shared secret expected in the X-Webhook-Secret-Token header.",
    )

    # --- Admin console ------------------------------------------------------
    # Serve the browser UI at GET /console. OFF by default: anything reachable
    # through the tunnel should be there because you chose it.
    #
    # Declared HERE rather than read from os.environ so it can be set in
    # .env.api like every other setting. A bare os.environ lookup would silently
    # ignore that file — pydantic-settings loads .env.api into this object, it
    # does not export those values into the process environment.
    enable_console: bool = False

    # Swagger UI (/docs), ReDoc (/redoc) and the raw schema (/openapi.json).
    # OFF by default: anything exposed through a tunnel should not publish a
    # machine-readable map of its own attack surface. Local development only.
    enable_docs: bool = False

    # --- Network ------------------------------------------------------------
    # 127.0.0.1 ONLY. The tunnel connects to us over loopback, so there is never
    # a reason to bind 0.0.0.0 and expose this to your LAN as well.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Job execution ------------------------------------------------------
    # The command run when a job is triggered, as an argv LIST (never a string):
    # a list goes straight to CreateProcess/execve with no shell in between, so
    # there is no shell metacharacter for an attacker to smuggle in.
    #
    # This is the REAL waitlist CLI. The API appends the targeting flags itself
    # (--route / --registrant / --combo / --dry-run|--live --yes), so what is
    # configured here is only the command PREFIX.
    #
    # `--json` makes the run print a machine-readable result block, which the
    # API parses to report per-client outcomes rather than just an exit code.
    #
    # SAFETY: nothing here forces live mode. TriggerRequest.dry_run defaults to
    # True, so a trigger that does not explicitly ask for --live cannot submit
    # a registration. See also [waitlist] register_enabled, the master switch.
    #
    # To point this at the placeholder instead (smoke tests, or to exercise the
    # plumbing without touching VFS):
    #   $env:VFSAPI_JOB_COMMAND = '["python","api_scripts/placeholder_job.py"]'
    job_command: List[str] = Field(
        default_factory=lambda: [
            sys.executable, "-m", "src.waitlist", "run", "--json",
        ],
        description="argv list executed on trigger. No shell is involved.",
    )

    # Working directory for the child process. Repo root, so `-m src.waitlist`
    # resolves and the bot's relative config/ and logs/ paths keep working.
    job_cwd: Path = REPO_ROOT

    # Refuse to start a second job while one is still running. The waitlist bot
    # drives a real browser and touches shared state files — two at once would
    # corrupt the journal and double-book accounts.
    single_flight: bool = True

    # Hard ceiling on a job's runtime. A hung Playwright run is killed rather
    # than pinning single_flight forever and blocking every future trigger.
    job_timeout_seconds: int = Field(default=3600, ge=30, le=86_400)

    # Where each job's combined stdout/stderr is written (one file per job id).
    job_log_dir: Path = REPO_ROOT / "logs" / "api_jobs"

    # How many finished jobs to keep in the in-memory status registry. Eviction
    # past this point only drops the MEMORY copy — the record stays in the
    # JSONL history below and is still served by GET /jobs/{id}.
    job_history_limit: int = Field(default=100, ge=1, le=10_000)

    # Durable job history. Every state transition is appended here, so a
    # restart mid-run cannot make "did this job register anyone?" unanswerable.
    job_history_file: Path = REPO_ROOT / "logs" / "api_jobs" / "jobs.jsonl"

    # --- Log retention ------------------------------------------------------
    # One log file per job, forever, is a slow disk leak. Two independent
    # ceilings: anything older than max_age_days goes, and if the directory is
    # still over max_total_mb the oldest survivors go until it fits. Set either
    # to 0 to disable that half.
    job_log_max_age_days: int = Field(default=30, ge=0, le=3650)
    job_log_max_total_mb: int = Field(default=2048, ge=0)

    # --- Rate limiting ------------------------------------------------------
    # Coarse per-process limit on trigger attempts, counted per client IP. Cheap
    # insurance against someone who has the URL hammering the endpoint.
    rate_limit_requests: int = Field(default=20, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    # Per-IP limiting keys on X-Forwarded-For, which the CALLER controls: a
    # flood that rotates that header gets a fresh bucket every request and is
    # never limited. This second ceiling counts every authenticated request in
    # the window regardless of source, so a header-rotating flood still hits a
    # wall. Sized well above the per-IP limit so normal multi-client use is
    # unaffected and only an actual flood trips it.
    rate_limit_global_requests: int = Field(default=200, ge=1)

    # --- Idempotency --------------------------------------------------------
    # How long an Idempotency-Key is remembered. A web app retrying a request
    # whose response it never saw gets the ORIGINAL job back rather than
    # spawning a second live registration run.
    idempotency_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)
    idempotency_max_keys: int = Field(default=1_000, ge=16, le=100_000)

    @field_validator("secret_token")
    @classmethod
    def _token_must_be_strong(cls, v: str) -> str:
        """Reject empty, short, or obviously-placeholder secrets at startup."""
        v = v.strip()
        if len(v) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"VFSAPI_SECRET_TOKEN must be at least {MIN_TOKEN_LENGTH} "
                f"characters (got {len(v)}). Generate one with:\n"
                f'  python -c "import secrets; print(secrets.token_hex(32))"'
            )
        if v.lower() in {"changeme", "secret", "token", "test", "password"}:
            raise ValueError("VFSAPI_SECRET_TOKEN is a placeholder value.")
        return v

    @field_validator("host")
    @classmethod
    def _host_should_be_loopback(cls, v: str) -> str:
        """Loud failure if someone tries to bind this to the world."""
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"host={v!r} is not loopback. This API is designed to be reached "
                "ONLY through the tunnel. Binding a non-loopback address exposes "
                "it to your whole network. Override deliberately in code if you "
                "truly need this."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    """Return the singleton settings object (parsed once, then cached)."""
    return ApiSettings()  # type: ignore[call-arg]  # values come from env
