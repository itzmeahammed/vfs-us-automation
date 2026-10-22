"""FastAPI application: the VFS bot's HTTP API.

Run it:
    python -m src.api                       # uses VFSAPI_* env vars / .env.api
    uvicorn src.api.main:app --host 127.0.0.1 --port 8000

Every endpoint is under /v1 and lives in src/api/modules/<area>/router.py:
system, accounts, catalog, clients, waitlist, booking, jobs, notifications.
This file only builds the app: settings, lifespan, error handling, middleware,
the /console page, and mounting /v1.

Security posture, briefly:
    * Binds loopback only. The public edge is the tunnel, never this socket.
    * Every endpoint except GET /v1/health requires the X-Webhook-Secret-Token
      header, compared in constant time.
    * The interactive docs (/docs, /redoc) and the OpenAPI schema are DISABLED
      by default — set VFSAPI_ENABLE_DOCS=1 for local development only.
    * Errors use one envelope (src/api/core/errors.py); unhandled exceptions
      return a generic message and the traceback goes to the server log.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api import __version__
from src.api.core import context as req_context
from src.api.core.config import get_settings
from src.api.core.errors import ApiError, envelope, from_detail
from src.api.modules.jobs.runtime import job_manager

logging.basicConfig(
    level=os.environ.get("VFSAPI_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("vfs.api")

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

    # The inbox watcher lives in this process (src/inbox/background.py): it
    # reads the VFS mailboxes every [inbox] poll_seconds and, while
    # [switches] invite_booking is on, books from the invitations it finds.
    # GET /v1/inbox/status shows it working. A failure to start it must not
    # stop the API: the supervisor still runs a pass per slot check.
    try:
        from src.inbox import background as inbox_background
        inbox_background.start()
    except Exception:                              # noqa: BLE001
        log.exception("Could not start the inbox watcher thread — the "
                      "supervisor's per-run pass is the only watcher.")

    try:
        yield
    finally:
        log.info("Shutting down — terminating any running jobs.")
        try:
            from src.inbox import background as inbox_background
            inbox_background.stop()
        except Exception:                          # noqa: BLE001
            log.exception("Inbox watcher thread did not stop cleanly.")
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


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
    """Every HTTPException leaves in the one envelope (src/api/core/errors.py).

    Registered on Starlette's base class, not FastAPI's subclass: the 404 for
    an unknown path and the 405 for a wrong method are raised by the router
    as the BASE type, and would otherwise leave as {"detail": "Not Found"} —
    the one error a caller of a removed path is guaranteed to see.

    A structured `detail` (a dict carrying a problem list, as the validation
    paths raise) keeps its problems, so a web app can show per-field errors.
    """
    return JSONResponse(status_code=exc.status_code,
                        content=from_detail(request, exc.status_code, exc.detail),
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 with the offending fields named, but no internal detail leaked."""
    problems = [
        {"field": _problem_field(err.get("loc", ())),
         "message": err.get("msg", ""),
         # The untrimmed path is still useful when debugging a nested model, so
         # it is kept rather than dropped — just not in the field name.
         "hint": ".".join(str(p) for p in err.get("loc", ()))}
        for err in exc.errors()
    ]
    log.info("Validation error on %s: %s", request.url.path, problems)
    return JSONResponse(status_code=422, content=envelope(
        request, 422, f"{len(problems)} problem(s) with the request.",
        "validation_error", problems))


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Typed errors raised by the modules."""
    return JSONResponse(status_code=exc.status, content=envelope(
        request, exc.status, exc.message, exc.code, exc.problems))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, tell the caller nothing useful."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    # Audit HERE, not in the middleware. Starlette installs this handler inside
    # ServerErrorMiddleware, which sits OUTSIDE user middleware, so a crashed
    # request never returns through `security_headers` — and the audit write
    # and the header block there were both skipped. That lost exactly the row
    # you most need: a mutating call that died mid-flight, where "did this
    # register someone / charge a card?" is the open question.
    _audit_once(request, 500)
    response = JSONResponse(status_code=500, content=envelope(
        request, 500, "An internal error occurred. Quote the request_id.",
        "internal_error"))
    return _stamp_headers(request, response)


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


#: Where pydantic says a bad value came from. Useful for debugging, useless as
#: a field name — and the browser keys its per-field form errors by exact name.
_LOC_PREFIXES = ("body", "query", "path", "header", "cookie")


def _problem_field(loc: Any) -> str:
    """The field name a form can bind an error to.

    FastAPI's `loc` leads with the location, so a rejected body field arrives
    as `body.client_id` and a query param as `query.limit`. The console indexes
    problems by exact field name (`errorFor('client_id')`) and filters
    candidates with `/^[a-z][a-z0-9_]*$/i`, which a dotted name fails — so a
    422 neither highlighted the offending input nor produced an inline message,
    and the operator had to read the field name out of the banner. The
    hand-built 422s elsewhere in the API already use bare names, which is why
    most validation errors did work and these did not.
    """
    parts = [str(p) for p in (loc or ())]
    if parts and parts[0] in _LOC_PREFIXES:
        parts = parts[1:]
    return ".".join(parts)


def _audit_once(request: Request, status_code: int) -> None:
    """Append the audit row for this request, at most once.

    Both the middleware and the error handlers can reach this, and a request
    that fails after the middleware already recorded it must not appear twice.
    """
    if getattr(request.state, "_audited", False):
        return
    try:
        request.state._audited = True
        req_context.audit(request, status_code)
    except Exception:                              # noqa: BLE001
        # A missing audit row is worth a loud log, never a failed response:
        # this runs on the error path, where the caller already has a problem.
        log.exception("Could not write the audit row for %s %s",
                      request.method, request.url.path)


def _stamp_headers(request: Request, response: Any) -> Any:
    """The hardening headers, for responses that bypass the middleware.

    A 413 returned early, and any 500 raised past ServerErrorMiddleware, used
    to arrive with none of these — no X-Request-ID to quote, no CSP, no
    nosniff, no Cache-Control. Confirmed live: the 413 response carried none of
    the header block that a 401 or 404 carried.
    """
    response.headers["X-Request-ID"] = getattr(request.state, "request_id", "")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'")
    return response


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
    request.state.request_id = req_context.request_id_for(request)
    request.state.actor = req_context.actor_for(request)

    # config.local.ini is also edited by hand and by config_editor.py. This
    # process lives for days, so without this it would go on reporting the
    # values it booted with while every slot-check run used the new ones.
    try:
        from src.settings import refresh_if_config_changed
        if refresh_if_config_changed():
            log.info("Config files changed on disk; settings reloaded.")
    except Exception:                              # noqa: BLE001
        log.exception("Config files changed but could not be re-read; "
                      "still serving the previous values.")

    is_upload = request.method == "POST" and request.url.path.endswith("/documents")
    max_bytes = (documents_max_upload_bytes() if is_upload else 64 * 1024)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        _audit_once(request, 413)
        return _stamp_headers(request, JSONResponse(status_code=413, content=envelope(
            request, 413, f"Request body exceeds {max_bytes} bytes.",
            "payload_too_large")))

    # A body sent with Transfer-Encoding: chunked carries NO Content-Length, so
    # the check above sees nothing and the cap was simply skipped. Verified
    # against the running server: the same 70,008-byte payload was rejected 413
    # with a Content-Length header and accepted (routed, 404) without one. The
    # "the endpoint still enforces documents.MAX_BYTES on the bytes it actually
    # reads" note above is true only for /documents; every JSON route had no
    # post-read ceiling at all, so an arbitrarily large chunked body was
    # buffered by uvicorn and handed to pydantic — in the single worker that
    # drives real registrations.
    #
    # So measure instead of trusting the header. Reading here is bounded: the
    # loop stops at the first chunk that crosses the cap, and the bytes are
    # cached on the request so handlers (including the multipart parser, which
    # replays from `_body`) still see the body exactly once.
    if content_length is None and request.method in ("POST", "PUT", "PATCH"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                log.warning("Chunked body over the %d-byte cap on %s %s",
                            max_bytes, request.method, request.url.path)
                _audit_once(request, 413)
                return _stamp_headers(request, JSONResponse(
                    status_code=413, content=envelope(
                        request, 413,
                        f"Request body exceeds {max_bytes} bytes.",
                        "payload_too_large")))
            chunks.append(chunk)
        request._body = b"".join(chunks)        # noqa: SLF001 — Starlette's cache

    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    _audit_once(request, response.status_code)
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



# --------------------------------------------------------------------------
# /v1 — every endpoint (src/api/modules).
# --------------------------------------------------------------------------

from src.api.modules import v1_router  # noqa: E402

app.include_router(v1_router())
