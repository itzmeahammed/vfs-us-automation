import argparse
import logging
import os
import sys

from src.utils.config_reader import get_config_value, initialize_config
from src.vfs_bot.vfs_bot import LoginError
from src.vfs_bot.vfs_bot_factory import UnsupportedCountryError, get_vfs_bot


def main() -> None:
    """
    Entry point for the VFS Malta Slot Checker.

    Logs in, reaches Step 1, reads the earliest-slot banner for each configured
    combination, and reports the results via Telegram. Defaults to the UAE ->
    Malta route (AE / MT); override with -sc / -dc if needed.
    """
    # Config must be read first so the logger can pick up [logging] level.
    initialize_config()
    initialize_logger()

    parser = argparse.ArgumentParser(
        description="VFS Malta Slot Checker: reports earliest appointment slots."
    )
    parser.add_argument(
        "-sc",
        "--source-country-code",
        type=str,
        default="AE",
        help="ISO 3166-1 alpha-2 source country code (default: AE).",
        metavar="<country_code>",
    )
    parser.add_argument(
        "-dc",
        "--destination-country-code",
        type=str,
        default="MT",
        help="ISO 3166-1 alpha-2 destination country code (default: MT).",
        metavar="<country_code>",
    )

    args = parser.parse_args()
    try:
        bot = get_vfs_bot(args.source_country_code, args.destination_country_code)
        bot.run()
    except (UnsupportedCountryError, LoginError) as e:
        logging.error(e)
    except Exception as e:
        logging.exception(e)


def resolve_log_level() -> int:
    """
    Resolves the logging level from (in priority order) the LOG_LEVEL env var,
    then [logging] level in config, defaulting to INFO. Accepts level names
    (DEBUG, INFO, WARNING, ERROR) case-insensitively.
    """
    raw = os.environ.get("LOG_LEVEL") or get_config_value("logging", "level", "INFO")
    level = logging.getLevelName(str(raw).strip().upper())
    # getLevelName returns an int for known names, else the string back.
    return level if isinstance(level, int) else logging.INFO


def resolve_browser_activity_logging() -> bool:
    """
    Whether to attach Playwright page hooks that log navigations, network
    requests/responses, console messages and page errors. Controlled by the
    BROWSER_ACTIVITY_LOG env var or [logging] browser_activity in config.
    """
    raw = (
        os.environ.get("BROWSER_ACTIVITY_LOG")
        or get_config_value("logging", "browser_activity", "False")
    )
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def initialize_logger():
    level = resolve_log_level()

    # A detailed format with timestamps, level and source line for both sinks so
    # the file log and console show the same information.
    detailed_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    )

    file_handler = logging.FileHandler("app.log", mode="a", encoding="utf-8")
    file_handler.setFormatter(detailed_fmt)

    # Per-day archive log for later analysis: logs/app-YYYY-MM-DD.log. Every run
    # of the day appends to that day's file (the date is fixed at startup, which
    # is robust across the separate short-lived scheduled processes — no midnight
    # rollover logic needed). app.log above stays the familiar live/current log.
    from datetime import datetime
    os.makedirs("logs", exist_ok=True)
    daily_path = os.path.join("logs", f"app-{datetime.now():%Y-%m-%d}.log")
    daily_handler = logging.FileHandler(daily_path, mode="a", encoding="utf-8")
    daily_handler.setFormatter(detailed_fmt)

    # Reconfigure stdout to UTF-8 so non-ASCII (page console output, URLs with
    # unicode, etc.) can't crash the console handler on Windows (cp1252).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # older Python / non-reconfigurable stream — best effort
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(detailed_fmt)

    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        handlers=[file_handler, daily_handler, stream_handler],
    )

    # Silence chatty third-party loggers (asyncio event-loop internals,
    # Playwright protocol) so our own progress logs stay readable even at DEBUG.
    for noisy in ("asyncio", "playwright", "urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.info(f"Logging initialized at level {logging.getLevelName(level)}.")


if __name__ == "__main__":
    main()
