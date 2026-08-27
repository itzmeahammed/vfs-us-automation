"""FastAPI application: local webhook that triggers jobs on this machine.

Run it:
    python -m src.api                       # uses VFSAPI_* env vars
    uvicorn src.api.main:app --host 127.0.0.1 --port 8000

Endpoints
    GET  /health            unauthenticated liveness probe
    POST /trigger/waitlist  AUTH — spawn the job, return 202 immediately
    GET  /jobs              AUTH — recent job history
    GET  /jobs/{job_id}     AUTH — one job's status
    POST /jobs/{job_id}/cancel  AUTH — stop a running job

Security posture, briefly:
    * Binds loopback only. The public edge is the tunnel, never this socket.
    * Every mutating endpoint requires the X-Webhook-Secret-Token header,
      compared in constant time.
    * The interactive docs (/docs, /redoc) and the OpenAPI schema are DISABLED
      by default — an exposed schema hands an attacker your entire API surface.
      Set VFSAPI_ENABLE_DOCS=1 for local development only.
    * Unhandled exceptions return a generic message; the traceback goes to the
      server log, not to the caller.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from src.api import __version__
from src.api.config import get_settings
from src.api.jobs import (
    JobAlreadyRunningError,
    JobManager,
    JobStartError,
    JobStatus,
    read_log_tail,
)
from src.api.schemas import (
    ErrorResponse,
    HealthResponse,
    JobListResponse,
    JobLogResponse,
    JobResponse,
    TriggerRequest,
    TriggerResponse,
)
from src.api.security import require_token

logging.basicConfig(
    level=os.environ.get("VFSAPI_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("vfs.api")

# One manager for the process lifetime.
job_manager = JobManager()

# Docs are off unless explicitly enabled. Anything exposed through a tunnel
# should not publish a machine-readable map of itself.
def _docs_enabled_at_import() -> bool:
    """Whether /docs, /redoc and /openapi.json exist at all.

    Unlike the console flag this MUST be resolved at import time: FastAPI takes
    docs_url/redoc_url/openapi_url as constructor arguments, so the routes are
    created (or not) before any request arrives.

    It still goes through ApiSettings rather than a bare os.environ lookup, so
    VFSAPI_ENABLE_DOCS works from .env.api like every other knob. Reading
    os.environ directly silently ignored that file — the same trap already
    fixed for the console.
    """
    try:
        return bool(get_settings().enable_docs)
    except Exception:                                  # noqa: BLE001
        # Settings are invalid; the server is about to fail loudly anyway.
        # Default to the closed answer rather than publishing a schema.
        return False


_DOCS_ENABLED = _docs_enabled_at_import()

# The admin console at GET /console. OFF by default, for the same reason as the
# docs: anything reachable through the tunnel should be there because you chose
# it, not because it shipped enabled.
#
# The page holds no secret of its own — it asks for the API token and sends it
# as X-Webhook-Secret-Token like any other caller, so serving the HTML gives an
# unauthenticated visitor nothing but markup. It must be served BY THIS APP
# rather than opened from disk: there is deliberately no CORS middleware, so a
# file:// page cannot call the API at all.
def documents_max_upload_bytes() -> int:
    """Body ceiling for a document upload, plus multipart framing overhead.

    Read from documents.MAX_BYTES rather than duplicated, so raising the portal
    limit in one place cannot leave the middleware rejecting files the endpoint
    would accept. The margin covers the multipart boundary, headers and the
    filename — a body is always a little larger than the file inside it.
    """
    try:
        from src.waitlist.documents import MAX_BYTES

        return MAX_BYTES + 64 * 1024
    except Exception:                                  # noqa: BLE001
        return 2 * 1024 * 1024 + 64 * 1024


def _console_enabled() -> bool:
    """Whether GET /console serves the UI.

    Read at REQUEST time, not import time, and from ApiSettings — so it can be
    set in .env.api like every other knob. A module-level os.environ lookup
    ignored that file entirely, which meant the documented way to configure this
    server silently did not work for this one flag.
    """
    try:
        return bool(get_settings().enable_console)
    except Exception:                                  # noqa: BLE001
        # Settings failed to validate; the server is already failing loudly
        # elsewhere. Default to the safe answer rather than raising here.
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate config on startup; kill orphaned jobs on shutdown."""
    settings = get_settings()
    # Touching secret_token here means a missing/weak token fails the SERVER,
    # loudly, at boot — not the first request, quietly, hours later.
    _ = settings.secret_token

    # The client endpoints validate against the bot's own config (routes,
    # waitlist page mappings, vfs_urls). That layer is lazily initialised, so
    # prime it once at startup rather than on the first request.
    try:
        from src.utils.config_reader import initialize_config
        initialize_config()
    except Exception:                              # noqa: BLE001
        # A bad bot config must not stop the API booting: /health and the job
        # endpoints still work, and /clients will report the problem per-route.
        log.exception("Could not initialise the bot config — /clients endpoints "
                      "may report routes as unavailable.")

    # Rebuild the job registry from disk BEFORE the first request can arrive.
    # Anything left 'running' by a previous process is reconciled to 'unknown'
    # and flagged for a human: the child may have completed a real registration
    # that we never recorded, and guessing either way is worse than saying so.
    try:
        orphaned = job_manager.load_history()
        if orphaned:
            log.error(
                "%d job(s) were interrupted by the last shutdown and need "
                "human verification — see GET /jobs?needs_attention=true.",
                orphaned,
            )
    except Exception:                              # noqa: BLE001
        log.exception("Could not load job history — starting with an empty "
                      "registry. Past jobs remain on disk.")

    # Housekeeping: one log file per job, forever, is a slow disk leak.
    try:
        job_manager.prune_logs()
    except Exception:                              # noqa: BLE001
        log.exception("Job log pruning failed — continuing.")

    log.info(
        "Webhook API v%s listening on http://%s:%s (docs=%s, single_flight=%s)",
        __version__,
        settings.host,
        settings.port,
        "on" if _DOCS_ENABLED else "off",
        settings.single_flight,
    )
    log.info("Job command: %s", " ".join(settings.job_command))
    try:
        yield
    finally:
        log.info("Shutting down — terminating any running jobs.")
        await job_manager.shutdown()


app = FastAPI(
    title="VFS Local Trigger API",
    description="Local webhook wrapper that triggers bot jobs on this machine.",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


# --------------------------------------------------------------------------
# Exception handlers — every error leaves as the same JSON envelope.
# --------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render HTTPException in the standard error envelope.

    A structured `detail` (a dict, as the client endpoints raise to carry their
    full problem list) is passed through as JSON. Stringifying it would hand the
    web app a Python repr it cannot parse into per-field form errors.
    """
    if isinstance(exc.detail, dict):
        content = dict(exc.detail)
        content.setdefault("error", _error_class(exc.status_code))
        content.setdefault("status_code", exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=getattr(exc, "headers", None),
        )

    body = ErrorResponse(
        error=_error_class(exc.status_code),
        detail=str(exc.detail),
        status_code=exc.status_code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 with the offending fields named, but no internal detail leaked."""
    problems = [
        {"field": ".".join(str(p) for p in err.get("loc", [])), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    log.info("Validation error on %s: %s", request.url.path, problems)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "detail": "Request body failed validation.",
            "status_code": 422,
            "problems": problems,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, tell the caller nothing useful."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    body = ErrorResponse(
        error="internal_error",
        detail="An internal error occurred. Check the server logs.",
        status_code=500,
    )
    return JSONResponse(status_code=500, content=body.model_dump())


def _error_class(status_code: int) -> str:
    """Map a status code to a short machine-readable error class."""
    return {
        400: "bad_request",
        401: "unauthorized",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        503: "service_unavailable",
    }.get(status_code, "error")


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    """Attach hardening headers and refuse oversized bodies.

    Content-Length is checked before the body is read, so a large payload is
    rejected rather than buffered.
    """
    # 64 KB — this API only ever receives small JSON. The ONE exception is a
    # document upload: some portals (Italy) want the passport bio page, and VFS
    # itself allows 2 MB, so a blanket 64 KB cap rejected every real scan with a
    # misleading "body too large" that named the wrong limit.
    #
    # The wider cap is scoped to that exact path suffix, and the endpoint still
    # enforces documents.MAX_BYTES on the bytes it actually reads — Content-Length
    # is a claim, not a measurement.
    is_upload = request.method == "POST" and request.url.path.endswith("/documents")
    max_bytes = (documents_max_upload_bytes() if is_upload else 64 * 1024)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "error": "payload_too_large",
                "detail": f"Request body exceeds {max_bytes} bytes.",
                "status_code": 413,
            },
        )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    # This API has no browser UI; a strict CSP costs nothing and blocks
    # anything that tries to render its responses as a page.
    # The API itself serves only JSON, so it gets the strictest possible policy.
    # The Swagger/ReDoc pages are the one exception: they load their CSS and JS
    # from cdn.jsdelivr.net, and `default-src 'none'` blocks those — the HTML
    # arrives (HTTP 200) but the page renders BLANK, which looks like a broken
    # server rather than a policy decision.
    #
    # Widening the policy here is safe precisely because those routes only exist
    # when VFSAPI_ENABLE_DOCS is set, which is documented as local-only. When
    # docs are off (the default, and always through the tunnel) every response
    # keeps the strict policy.
    if _DOCS_ENABLED and request.url.path in ("/docs", "/redoc"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' https://fastapi.tiangolo.com data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    elif _console_enabled() and request.url.path == "/console":
        # The console is one self-contained file: its CSS and JS are inline, so
        # 'unsafe-inline' is what makes it render at all. Everything else stays
        # shut — no external origin may supply script, style, or images, and
        # connect-src 'self' means the page can only ever call THIS API.
        #
        # Narrower than it looks: this branch matches the single /console path,
        # so every JSON response keeps the strict policy below.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "form-action 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'")
    return response


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


from src.api.clients import router as clients_router          # noqa: E402
from src.api.clients import routes_router                     # noqa: E402
from src.api.status import router as status_router            # noqa: E402

# Client management, route readiness, and operational status. Imported after
# `app` exists so the routers can be attached; each carries its own
# require_token dependency.
app.include_router(clients_router)
app.include_router(routes_router)
app.include_router(status_router)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Liveness probe. Unauthenticated on purpose, and says nothing sensitive.

    Useful for confirming the tunnel reaches your machine at all, without
    handing out the token to whoever is testing.
    """
    return HealthResponse(
        status="ok",
        version=__version__,
        server_time=datetime.now(timezone.utc).isoformat(),
    )


_CONSOLE_FILE = Path(__file__).resolve().parent / "console.html"


@app.get("/console", response_class=HTMLResponse, include_in_schema=False)
async def console() -> HTMLResponse:
    """The admin console. Serves markup only — every API call it makes is
    authenticated exactly like any other client.

    Unauthenticated ON PURPOSE. The page contains no data and no secret: it
    renders a sign-in box and asks for the token, which the browser then sends
    as X-Webhook-Secret-Token on each request. Requiring a token to fetch the
    HTML would mean having nowhere to type the token in.

    404s (not 403) when disabled, so a probe through the tunnel cannot tell a
    switched-off console from a build that never had one.
    """
    if not _console_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found.")
    try:
        html = _CONSOLE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.exception("Console file missing or unreadable: %s", _CONSOLE_FILE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Console asset is unavailable.",
        ) from exc
    return HTMLResponse(content=html)


@app.post(
    "/trigger/waitlist",
    response_model=TriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_token)],
    tags=["trigger"],
    responses={
        401: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def trigger_waitlist(
    payload: TriggerRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=200,
        description=(
            "Optional. Send a stable unique value (a UUID) to make retries "
            "safe: a repeat with the same key returns the ORIGINAL job instead "
            "of starting a second run. Strongly recommended for live triggers."
        ),
    ),
) -> TriggerResponse:
    """Spawn the waitlist job in the background and return at once.

    202 Accepted means "spawned", never "finished". Poll GET /jobs/{job_id}
    for the outcome, or read the log with GET /jobs/{job_id}/logs.

    IDEMPOTENCY. Without a key, a client that retries after a network timeout
    can start a SECOND live registration run — single-flight blocks the
    concurrent case but not a sequential retry after the first finished. Send
    an Idempotency-Key and the retry returns the original job untouched.
    """
    try:
        record, replayed = await job_manager.trigger(
            extra_args=payload.to_cli_args(),
            payload=payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
    except JobAlreadyRunningError as exc:
        # A distinct exception type, not a substring of the message: rewording
        # the message must never silently turn a 409 into a 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except JobStartError as exc:
        # The process would not start at all — a broken install, not a busy one.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return TriggerResponse(
        accepted=True,
        message=(
            "Replayed: this Idempotency-Key already started a job, so nothing "
            "new was spawned."
            if replayed else "Job started in the background."
        ),
        job=JobResponse(**record.to_dict()),
        replayed=replayed,
    )


@app.get(
    "/jobs",
    response_model=JobListResponse,
    dependencies=[Depends(require_token)],
    tags=["jobs"],
    responses={401: {"model": ErrorResponse}},
)
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    needs_attention: bool = Query(
        default=False,
        description="Return only jobs with an unresolved submit or an outcome "
                    "lost to a restart. These BLOCK their client from running "
                    "again until a human verifies the VFS account.",
    ),
) -> JobListResponse:
    """Recent jobs, most recent first."""
    records = job_manager.recent(limit if not needs_attention else 100)
    if needs_attention:
        records = [r for r in records if r.needs_attention][:limit]
    active = job_manager.active_job
    return JobListResponse(
        count=len(records),
        active_job_id=active.job_id if active else None,
        jobs=[JobResponse(**r.to_dict()) for r in records],
    )


@app.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    dependencies=[Depends(require_token)],
    tags=["jobs"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_job(job_id: str) -> JobResponse:
    """Status of one job."""
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )
    return JobResponse(**record.to_dict())


@app.get(
    "/jobs/{job_id}/logs",
    response_model=JobLogResponse,
    dependencies=[Depends(require_token)],
    tags=["jobs"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_job_logs(
    job_id: str,
    lines: int = Query(default=200, ge=1, le=2000,
                       description="How many trailing lines to return."),
) -> JobLogResponse:
    """Tail of a job's log.

    The `log_file` field elsewhere is an absolute path on the machine running
    this API — useless to a remote web app, and a small disclosure besides.
    This serves the content instead, so an operator can diagnose a failed run
    without shell access.

    Declared `def`, not `async def`: it reads a file, and FastAPI runs a sync
    endpoint in a threadpool rather than blocking the event loop.
    """
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )

    path = record.log_file
    if not path or not os.path.exists(path):
        # A pruned or never-created log is not a 404 on the JOB — the job is
        # real and its status still means something.
        return JobLogResponse(
            job_id=job_id, lines=[], line_count=0,
            truncated=False, log_available=False,
        )

    try:
        tail, truncated = read_log_tail(path, lines)
    except OSError as exc:
        log.warning("Could not read log for job %s: %s", job_id, exc)
        return JobLogResponse(
            job_id=job_id, lines=[], line_count=0,
            truncated=False, log_available=False,
        )

    return JobLogResponse(
        job_id=job_id,
        lines=tail,
        line_count=len(tail),
        truncated=truncated,
        log_available=True,
    )


@app.post(
    "/jobs/{job_id}/cancel",
    response_model=JobResponse,
    dependencies=[Depends(require_token)],
    tags=["jobs"],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def cancel_job(job_id: str) -> JobResponse:
    """Terminate a running job (and its child processes)."""
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )
    if record.status is not JobStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is not running (status={record.status.value}).",
        )
    await job_manager.cancel(job_id)
    return JobResponse(**record.to_dict())
