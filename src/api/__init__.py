"""Local webhook API that lets a remote web app trigger jobs on this machine.

The package is deliberately self-contained: it imports nothing from the bot
itself, so a crash in (or a dependency of) the API can never take the slot
checker down, and `pip install fastapi uvicorn` is only needed on the machine
that actually runs the webhook listener.

Entry point:  python -m src.api          (see __main__.py)
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
