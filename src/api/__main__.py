"""Entry point: `python -m src.api`.

Reads host/port from the same validated settings the app uses, so there is one
source of truth and no chance of uvicorn binding something the app did not
expect. Fails fast and readably if the shared secret is unset.
"""

from __future__ import annotations

import sys

import uvicorn

from src.api.config import get_settings


def main() -> None:
    """Start the uvicorn server on the configured loopback address."""
    try:
        settings = get_settings()
    except Exception as exc:  # pydantic ValidationError, most likely
        print("FATAL: webhook API configuration is invalid.\n", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(
            "\nMost likely cause: VFSAPI_SECRET_TOKEN is not set.\n"
            "Generate and set one:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"\n'
            '  $env:VFSAPI_SECRET_TOKEN = "<paste the token>"',
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    uvicorn.run(
        "src.api.main:app",
        host=settings.host,
        port=settings.port,
        # No reload in production: it spawns a watcher process that would
        # duplicate the JobManager and break single-flight.
        reload=False,
        # One worker. Job state lives in memory, so a second worker would have
        # its own registry and its own idea of what is running.
        workers=1,
        access_log=True,
        # We terminate our own child jobs in the lifespan shutdown hook.
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
