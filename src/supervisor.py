"""Self-healing supervisor for the VFS slot checker (EC2 / hourly cron).

One invocation runs the slot check for EVERY route listed in [vfs-url] (one
cron => all URLs). For each route, in order, the supervisor:

  1. Launches a fresh, real Chrome (CDP) that IT owns — a NEW Chrome per route.
  2. Points the bot at that Chrome and runs the full slot-check flow.
  3. KILLS Chrome on the way out — success or failure — so no zombie processes
     accumulate (and the next route always starts clean).
  4. On a retryable failure (Cloudflare not passed / Sign In disabled / page
     closed / dashboard not reached / CDP connect failure / any unexpected
     error), it tears everything down and tries again with a brand-new browser,
     up to MAX_ATTEMPTS times with a short backoff.
  5. Sends that route's slot report to Telegram (done by the bot), and a Telegram
     alert if all its attempts fail. One route failing does not stop the others.

So a single cron tick produces: url1 -> msg, url2 -> msg, url3 -> msg ...

Run directly:   python -m src.supervisor          # all routes
                python -m src.supervisor -sc AE -dc MT   # one route only
On EC2 it's invoked under xvfb-run by run_ec2.sh (see that script).
"""

import argparse
import logging
import os
import sys
import time

from src.main import initialize_logger
from src.utils import telegram, telegram_message
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import (
    get_config_section,
    get_config_value,
    initialize_config,
    set_config_value,
)
from src.vfs_bot.vfs_bot import (
    AccessRestrictedError,
    AccountLockedError,
    EmailNotRegisteredError,
    GeoBlockedError,
    InvalidCredentialsError,
    RetryableError,
)
from src.vfs_bot.vfs_bot_factory import UnsupportedCountryError, get_vfs_bot

# Retry policy. The EC2 box is slow, so individual UI actions occasionally time
# out; relaunching a fresh browser usually succeeds. 3 attempts balances
# resilience against total run time (each attempt can take a few minutes).
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 15
CDP_PORT = 9222


def _vfs_url(source: str, dest: str) -> str:
    return get_config_value("vfs-url", f"{source.upper()}-{dest.upper()}")


def run_once_with_fresh_browser(source: str, dest: str) -> list:
    """
    One attempt: launch a fresh Chrome, run the flow, always kill Chrome after.

    Returns the bot's slot_results (list of (label, message) pairs) if the slot
    check completed. Raises RetryableError (or other exceptions) on failure — the
    caller decides whether to retry.
    """
    url = _vfs_url(source, dest)
    # Optional proxy (e.g. an SSH reverse tunnel to your home PC) so VFS sees a
    # residential IP instead of the EC2 datacenter IP. Off unless configured.
    proxy = get_config_value("browser", "proxy", "") or None
    chrome = ChromeProcess(port=CDP_PORT, url=url, proxy=proxy)
    try:
        chrome.start()
        # Point the bot at the Chrome we just launched.
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        bot = get_vfs_bot(source, dest)
        if not bot.run():
            # Shouldn't normally happen (run() raises on failure), but treat a
            # bare False as a retryable failed attempt.
            raise RetryableError("Flow returned without completing.")
        return getattr(bot, "slot_results", [])
    finally:
        # Guaranteed cleanup — this is the anti-zombie guarantee.
        chrome.close()


def _combo_errors(slot_results: list) -> list:
    """
    Extracts per-combination slot-search errors from a route's slot_results.

    The bot tags a real selection/search failure with an 'ERROR:' prefix (as
    opposed to genuine 'no availability'). Returns a list of (label, short_reason).
    """
    out = []
    for label, message in slot_results or []:
        if message and message.startswith("ERROR:"):
            out.append((label, message[len("ERROR:"):].strip()))
    return out


def _outcome(source: str, dest: str, status: str, attempts: int,
             slot_results: list = None, error: str = None,
             account: str = "") -> dict:
    """
    Builds a per-route outcome record for the run summary.

    status is one of: 'OK', 'SKIPPED' (email not registered), 'FAILED' (retries
    exhausted / unsupported / a slot-search error), 'GEO' (geo-blocked), 'STOPPED'
    (invalid creds), 'LOCKED' (429202 cooldown), 'RESTRICTED' (access restricted —
    route skipped this run, tried again next run). `ok` (used for the process
    exit code) is True only for OK and SKIPPED.

    A completed run ('OK') that hit any per-combination slot-search error is
    promoted to 'FAILED' — an error while searching slots counts as a failure and
    is reported (with the offending combos) in the summary. Genuine 'no
    availability' is NOT an error and does not fail the route.
    """
    slots = slot_results or []
    slot_count = sum(1 for _label, message in slots if telegram_message._has_slot(message))
    combo_errors = _combo_errors(slots)
    disabled = [label for label, message in slots if message == "DISABLED"]

    # Slots grouped by visa type (the last label segment, e.g. 'Tourism'), so
    # the summary can say 'Tourism: 2 slot(s)' instead of just a total.
    slot_types = []  # list of [type, count], insertion-ordered
    for label, message in slots:
        if telegram_message._has_slot(message):
            visa_type = label.split(" - ")[-1].strip() or label
            for entry in slot_types:
                if entry[0] == visa_type:
                    entry[1] += 1
                    break
            else:
                slot_types.append([visa_type, 1])

    if status == "OK" and combo_errors:
        status = "FAILED"
        if not error:
            error = f"{len(combo_errors)} combo(s) failed slot search"

    return {
        "source": source, "dest": dest, "status": status, "attempts": attempts,
        "error": error, "slots": slot_count, "combos": len(slots) - len(disabled),
        "combo_errors": combo_errors, "disabled": disabled,
        "slot_types": slot_types, "account": account,
        "ok": status in ("OK", "SKIPPED"),
    }


def run(source: str = "AE", dest: str = "MT") -> dict:
    """
    Runs up to MAX_ATTEMPTS attempts with a fresh browser each time.

    Returns a per-route outcome dict (see _outcome) so the caller can both decide
    the exit code (via outcome['ok']) and build the run summary.
    """
    from datetime import datetime
    from src.utils import credentials

    # Per-route credential check BEFORE launching a browser: each route rotates
    # through the accounts registered on it ([credN] 'routes' lists). A route
    # no account covers is skipped cleanly with a clear reason.
    route = f"{source.upper()}-{dest.upper()}"
    hour = datetime.now().hour
    account = credentials.active_account(hour, route)
    if not account:
        reason = "no registered credential for this route"
        logging.warning(f"{route}: {reason} — skipping.")
        return _outcome(source, dest, "SKIPPED", 0, error=reason)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logging.info(f"=== Attempt {attempt}/{MAX_ATTEMPTS} ===")
        try:
            slots = run_once_with_fresh_browser(source, dest)
            logging.info(f"Success on attempt {attempt}.")
            return _outcome(source, dest, "OK", attempt, slot_results=slots,
                            account=account)
        except EmailNotRegisteredError as e:
            # Expected, not an error: this account isn't registered for this URL.
            # Skip it silently (no retries, no Telegram alert) and move on.
            logging.info(f"{source}-{dest}: account not registered here — skipping. ({e})")
            return _outcome(source, dest, "SKIPPED", attempt, error=str(e),
                            account=account)
        except GeoBlockedError as e:
            # Non-retryable: VFS geo-blocked the IP (403203). Retrying uses the
            # same IP and fails identically — stop now, no further attempts.
            logging.error(f"Geo-blocked for {source}-{dest}: {e}")
            _alert_failure(source, dest, f"Geo-blocked (403203): {e}", attempts=attempt)
            return _outcome(source, dest, "GEO", attempt, error=str(e),
                            account=account)
        except InvalidCredentialsError as e:
            # Non-retryable: wrong email/password fails identically on retry.
            # Stop this route immediately (no further attempts) and alert on the
            # error channel. Other routes may rotate to different accounts, so
            # they still run.
            logging.error(f"Invalid credentials for {source}-{dest}: {e}")
            _alert_failure(source, dest, f"Invalid credentials: {e}", attempts=attempt)
            return _outcome(source, dest, "STOPPED", attempt, error=str(e),
                            account=account)
        except AccessRestrictedError as e:
            # Non-retryable WITHIN this run: VFS restricted access on this route.
            # Skip the route immediately (no browser relaunches — hammering it
            # prolongs the restriction) and move on to the next route with the
            # same account. The route is tried again fresh on the NEXT cron tick
            # (e.g. hit at :29 -> retried at :59) — nothing is persisted.
            logging.error(f"Access restricted for {source}-{dest}: {e}")
            _alert_failure(
                source, dest,
                f"Access restricted: {e} — skipping this route for this run; "
                f"it will be tried again on the next scheduled run.",
                attempts=attempt,
            )
            return _outcome(source, dest, "RESTRICTED", attempt, error=str(e),
                            account=account)
        except AccountLockedError as e:
            # Non-retryable: VFS locked the account (429202) for too many requests;
            # it resets after a cooldown (~2h). Report the exact on-page message.
            logging.error(f"Account locked for {source}-{dest}: {e}")
            _alert_failure(source, dest, f"Account locked (429202): {e}", attempts=attempt)
            return _outcome(source, dest, "LOCKED", attempt, error=str(e),
                            account=account)
        except UnsupportedCountryError as e:
            # Not retryable — a config problem, not a transient failure.
            logging.error(f"Unsupported route {source}-{dest}: {e}")
            _alert_failure(source, dest, str(e), attempts=attempt)
            return _outcome(source, dest, "FAILED", attempt, error=str(e),
                            account=account)
        except RetryableError as e:
            last_error = f"{type(e).__name__}: {e}"
            logging.warning(f"Attempt {attempt} failed (retryable): {last_error}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logging.exception(f"Attempt {attempt} failed (unexpected): {last_error}")

        if attempt < MAX_ATTEMPTS:
            logging.info(f"Backing off {BACKOFF_SECONDS}s before next attempt...")
            time.sleep(BACKOFF_SECONDS)

    logging.error(f"All {MAX_ATTEMPTS} attempts failed. Last error: {last_error}")
    _alert_failure(source, dest, last_error, attempts=MAX_ATTEMPTS)
    return _outcome(source, dest, "FAILED", MAX_ATTEMPTS, error=last_error,
                    account=account)


def _all_routes() -> list:
    """
    Returns every route configured in [vfs-url] as (source, dest) tuples.

    Each key in the section is a '<SOURCE>-<DEST>' route (e.g. 'AE-MT'), so we
    just split the keys. Order follows the config file.
    """
    section = get_config_section("vfs-url")
    routes = []
    for key in section:
        parts = key.upper().split("-")
        if len(parts) == 2 and parts[0] and parts[1]:
            routes.append((parts[0], parts[1]))
        else:
            logging.warning(f"Skipping malformed [vfs-url] key '{key}' (want SRC-DEST).")
    return routes


def run_all_routes() -> bool:
    """
    Runs the slot check for EVERY route in [vfs-url], one after another.

    Each route gets its OWN fresh Chrome (launched and killed per attempt inside
    run() -> run_once_with_fresh_browser), and its own Telegram report is sent by
    the bot at the end of its run. One route failing does NOT stop the others —
    each is independent, with its own retries and its own failure alert.

    A compact run summary (every route's status) is sent to the summary chat at
    the end of every run, regardless of success/failure.

    Returns True only if ALL routes succeeded.
    """
    routes = _all_routes()
    if not routes:
        logging.error("No routes configured in [vfs-url] — nothing to run.")
        return False

    # Surface typos in [credN] 'routes' lists (a misspelt route would silently
    # shrink that route's credential pool).
    from src.utils import credentials
    credentials.warn_unknown_routes()

    logging.info(
        f"Running {len(routes)} route(s): "
        + ", ".join(f"{s}-{d}" for s, d in routes)
    )

    outcomes = []
    for idx, (source, dest) in enumerate(routes, start=1):
        logging.info(f"########## Route {idx}/{len(routes)}: {source}-{dest} ##########")
        try:
            # A fresh Chrome is opened and closed for this route inside run().
            outcome = run(source, dest)
        except Exception as e:
            # run() handles its own errors, but guard so one route can never
            # abort the whole loop.
            logging.exception(f"Route {source}-{dest} crashed unexpectedly: {e}")
            outcome = _outcome(source, dest, "FAILED", MAX_ATTEMPTS, error=str(e))
        outcomes.append(outcome)
        logging.info(f"Route {source}-{dest} {outcome['status']}.")

    all_ok = all(o["ok"] for o in outcomes)
    logging.info(f"All routes done. Overall {'OK' if all_ok else 'with failures'}.")
    _send_run_summary(outcomes)
    return all_ok


def _send_run_summary(outcomes: list) -> None:
    """
    Builds the compact run summary and sends it to the summary chat (best-effort).

    Sent after EVERY run so you can monitor each route's status at a glance. This
    is separate from (and additional to) the per-route slot reports, which still
    go to the success chat only when slots are found.
    """
    from datetime import datetime

    # Accounts are chosen per route now, so each outcome carries its own
    # 'account' and the summary shows it per line; the header carries none.
    now = datetime.now()
    msg = telegram_message.run_summary(
        outcomes, "", now.strftime("%Y-%m-%d %H:%M")
    )
    logging.info("Run summary:\n" + msg)
    if telegram.is_error_configured():
        telegram.send_error(msg)
    else:
        logging.warning("Telegram summary channel not configured — run summary logged only.")


def _alert_failure(source: str, dest: str, error: str, attempts: int) -> None:
    """Sends a Telegram alert that the run failed (best-effort).

    Includes the account (email) that was used for this hour, so you know which
    credential failed. Message layout lives in src/utils/telegram_message.py.
    """
    login_url = _vfs_url(source, dest) or ""
    # The account in use this hour ON THIS ROUTE — the same one the bot tried
    # (per-route rotation is by clock hour, so recomputing gives the same email).
    from datetime import datetime
    from src.utils import credentials
    email, _ = credentials.get_credential(
        datetime.now().hour, f"{source.upper()}-{dest.upper()}"
    )
    msg = telegram_message.failure_alert(
        source, dest, error, attempts, login_url, email or ""
    )
    logging.error(msg)
    if telegram.is_error_configured():
        telegram.send_error(msg)
    else:
        logging.warning("Telegram error channel not configured — failure alert logged only.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-healing supervisor for the VFS slot checker."
    )
    # By default, run EVERY route in [vfs-url], each in its own fresh Chrome,
    # pushing a Telegram report per route. Pass -sc/-dc to run just one route.
    parser.add_argument(
        "-sc", "--source-country-code", default=None,
        help="Run only this source country (with -dc). Omit to run all routes.",
    )
    parser.add_argument(
        "-dc", "--destination-country-code", default=None,
        help="Run only this destination country (with -sc). Omit to run all routes.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Detailed step-by-step (DEBUG) logs. Omit for prod/schedule, which "
             "logs only major events.",
    )
    args = parser.parse_args()

    initialize_config()
    # -v gives fine-grained step logs; without it (prod/schedule) only major
    # events are logged. LOG_LEVEL is read by initialize_logger(), so set it first.
    if args.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    initialize_logger()

    if args.source_country_code and args.destination_country_code:
        outcome = run(args.source_country_code, args.destination_country_code)
        _send_run_summary([outcome])
        ok = outcome["ok"]
    else:
        ok = run_all_routes()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
