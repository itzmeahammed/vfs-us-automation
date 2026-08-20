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
from typing import Any, AsyncIterator, Dict

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.api import __version__
from src.api.config import get_settings
from src.api.jobs import JobManager, JobStartError, JobStatus
from src.api.schemas import (
    ErrorResponse,
    HealthResponse,
    JobListResponse,
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
_DOCS_ENABLED = os.environ.get("VFSAPI_ENABLE_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


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
    max_bytes = 64 * 1024  # 64 KB — this API only ever receives small JSON.
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
async def trigger_waitlist(payload: TriggerRequest) -> TriggerResponse:
    """Spawn the waitlist job in the background and return at once.

    202 Accepted means "spawned", never "finished". Poll GET /jobs/{job_id}
    for the outcome, or read the per-job log file named in the response.
    """
    try:
        record = await job_manager.trigger(
            extra_args=payload.to_cli_args(),
            payload=payload.model_dump(mode="json"),
        )
    except JobStartError as exc:
        # Two distinct causes, two distinct codes: 409 when something is
        # already running (retry later), 500 when the process would not start.
        already_running = "already running" in str(exc)
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if already_running
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=str(exc),
        ) from exc

    return TriggerResponse(
        accepted=True,
        message="Job started in the background.",
        job=JobResponse(**record.to_dict()),
    )


@app.get(
    "/jobs",
    response_model=JobListResponse,
    dependencies=[Depends(require_token)],
    tags=["jobs"],
    responses={401: {"model": ErrorResponse}},
)
async def list_jobs(limit: int = 20) -> JobListResponse:
    """Recent jobs, most recent first."""
    limit = max(1, min(limit, 100))
    records = job_manager.recent(limit)
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
