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
from src.settings import settings
from src.utils import (
    account_health, connectivity, credentials, proxy_pool, telegram, telegram_message,
)
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import (
    get_config_section,
    get_config_value,
    initialize_config,
    set_config_value,
)
from src.vfs_bot.vfs_bot import (
    AccessRestrictedError,
    AccountBlockedError,
    AccountLockedError,
    EmailNotRegisteredError,
    GeoBlockedError,
    InvalidCredentialsError,
    IpBlockedError,
    OtpVerificationError,
    RetryableError,
    SignInDisabledError,
)
from src.vfs_bot.vfs_bot_factory import UnsupportedCountryError, get_vfs_bot

def _max_attempts() -> int:
    """In-run browser relaunches on failure ([account_safety] max_attempts).

    Fewer relaunches is gentler on accounts. Typed/validated in src.settings.
    """
    return max(1, settings().account_safety.max_attempts)


# Failure modes that are the INFRASTRUCTURE's fault (proxy exit / network /
# Cloudflare / CDP / geo), NOT the account's. These must never count as an
# account strike — otherwise a network blip or a flaky IP benches a perfectly
# good account for hours. Only genuine auth/OTP/access errors (handled in their
# own except-branches: InvalidCredentials, AccountBlocked, AccessRestricted,
# AccountLocked, OtpVerification) strike an account.
_INFRA_EXC_NAMES = (
    "CdpConnectError", "LoginFormNotReadyError", "SignInDisabledError",
    "TurnstileRejectedError", "GeoBlockedError", "IpBlockedError",
    "ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError",
    "ConnectionError", "TimeoutError",
)
_INFRA_MARKERS = (
    "winerror 10054", "winerror 10053", "winerror 10060", "winerror 10061",
    "winerror 10065", "connection reset", "connection aborted",
    "connection refused", "actively refused", "remotedisconnected",
    "connectionreset", "connectionabort", "broken pipe", "chrome exited early",
    "cdp endpoint never came up", "err_proxy", "err_tunnel", "err_empty_response",
    "err_connection", "err_timed_out", "err_address_unreachable", "net::err",
    "proxy", "forwarder", "geo-block", "403203",
)


def _is_infra_error(exc) -> bool:
    """True if `exc` is a network/proxy/Cloudflare/CDP failure (infra), not the
    account's fault — so the caller can fail the run WITHOUT striking the account."""
    name = type(exc).__name__ if isinstance(exc, BaseException) else ""
    if name in _INFRA_EXC_NAMES:
        return True
    text = f"{name}: {exc}".lower()
    return any(m in text for m in _INFRA_MARKERS)


def _vfs_url(source: str, dest: str) -> str:
    return get_config_value("vfs-url", f"{source.upper()}-{dest.upper()}")


def run_once_with_fresh_browser(source: str, dest: str,
                                email: str = None, password: str = None,
                                proxy: str = None, keep_open: bool = False,
                                traffic_sink: list = None) -> list:
    """
    One attempt: launch a fresh Chrome, run the flow, always kill Chrome after.

    The supervisor selects the credential AND the proxy once per route (see
    run()) and injects them here, so selection lives in ONE place. `proxy` is
    the egress for this route's Chrome (None = direct). Returns the bot's
    slot_results on success; raises on failure — the caller decides whether to
    retry.

    `traffic_sink` (optional): a list the teardown appends this attempt's
    bandwidth lines to (browser + proxy). The CALLER logs them AFTER the attempt's
    pass/fail line, so accounting never appears above the outcome it belongs to.

    keep_open (manual debugging only) leaves the browser open at the end and
    blocks until you press Enter, so you can inspect the final page.
    """
    url = _vfs_url(source, dest)
    # profile_key ties the (optional) persistent cache to the ACCOUNT, so each
    # account reuses only its own warm cache + its own IP's cf_clearance.
    chrome = ChromeProcess(port=settings().retry.cdp_port, url=url, proxy=proxy,
                           profile_key=email)
    bot = None
    try:
        chrome.start()
        # Point the bot at the Chrome we just launched.
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        # If this account's cached profile was last used from a DIFFERENT egress IP
        # (e.g. warmed on local, now run via proxy), tell the bot to drop the stale
        # IP-bound cf_clearance while keeping the HTTP asset cache.
        set_config_value("browser", "keep_cf_clearance",
                         "false" if getattr(chrome, "egress_changed", False) else "true")
        bot = get_vfs_bot(source, dest)
        if email and password:
            bot.set_credential(email, password)
        if not bot.run():
            # Shouldn't normally happen (run() raises on failure), but treat a
            # bare False as a retryable failed attempt.
            raise RetryableError("Flow returned without completing.")
        return getattr(bot, "slot_results", [])
    finally:
        if keep_open:
            # Debug: hold the browser open for inspection until the user is done.
            try:
                input("\n[keep-open] Browser left open. Press Enter to close it...")
            except EOFError:
                pass
        # Guaranteed cleanup — this is the anti-zombie guarantee.
        chrome.close()
        # Hand the bandwidth lines to the caller to log after the outcome.
        if traffic_sink is not None:
            summary = getattr(bot, "traffic_summary", None)
            if summary:
                traffic_sink.append(summary)
            traffic_sink.extend(getattr(chrome, "traffic_lines", []))


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
             account: str = "", proxy: str = "") -> dict:
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
    from src.vfs_bot import waitlist
    slots = slot_results or []
    slot_count = sum(1 for _label, message in slots if telegram_message._has_slot(message))
    combo_errors = _combo_errors(slots)
    disabled = [label for label, message in slots if message == "DISABLED"]
    waitlist_count = waitlist.count_waitlist(slots)

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

    # A per-combination slot-search error does NOT fail the route. Reaching the
    # appointment page and checking what it could IS the run's job; one flaky
    # combo (e.g. VFS's spinner stalling on a centre switch) is reported on its
    # own line and in that combo's Telegram message, but the route stays OK. Only
    # a real flow failure (login/Cloudflare/dashboard/block) marks it FAILED.
    if status == "OK" and combo_errors and not error:
        error = f"{len(combo_errors)} combo(s) errored (route still OK)"

    return {
        "source": source, "dest": dest, "status": status, "attempts": attempts,
        "error": error, "slots": slot_count, "combos": len(slots) - len(disabled),
        "combo_errors": combo_errors, "disabled": disabled,
        "slot_types": slot_types, "account": account, "proxy": proxy,
        "waitlist": waitlist_count,
        # OK/SKIPPED/PAUSED are not failures for the exit code; PAUSED means we
        # deliberately held off (all accounts cooling) — not an error.
        "ok": status in ("OK", "SKIPPED", "PAUSED"),
    }


def run(source: str = "AE", dest: str = "MT", route_index: int = 0,
        force_email: str = None, force_password: str = None,
        force_proxy: str = None, keep_open: bool = False) -> dict:
    """
    Runs a route with ONE account (selected up front, skipping benched/disabled)
    and its pinned proxy IP, updating account health per the outcome.

    On a 403201 IP block it rotates to a DIFFERENT IP and retries once (up to
    MAX_IP_TRIES IPs) WITHOUT penalising the account — it's the IP, not the user.

    Test overrides (single-route only): `force_email`(+`force_password`) forces a
    specific account; `force_proxy` forces a proxy URL ('' = direct/local).

    Returns a per-route outcome dict (see _outcome).
    """
    route = f"{source.upper()}-{dest.upper()}"
    forced_proxy = force_proxy is not None

    # --- credential ---
    if force_email:
        email = force_email
        password = force_password or credentials.password_for(force_email)
        if not password:
            reason = (f"forced account {force_email} has no known password "
                      "(pass --password)")
            logging.error(f"{route}: {reason}")
            return _outcome(source, dest, "FAILED", 0, error=reason)
        account = credentials.mask(email) + " (forced)"
    else:
        # get_credential skips benched/disabled accounts, spread across the day.
        email, password = credentials.get_credential(route)
        if not email or not password:
            if credentials.eligible_emails(route):
                reason = "all eligible accounts are in cooldown (protecting them)"
                logging.warning(f"{route}: {reason} — pausing this run.")
                return _outcome(source, dest, "PAUSED", 0, error=reason)
            reason = "no registered credential for this route"
            logging.warning(f"{route}: {reason} — skipping.")
            return _outcome(source, dest, "SKIPPED", 0, error=reason)
        account = credentials.active_account(route)

    # --- proxy (the account's pinned IP, or a forced one) ---
    tried_proxies = set()
    if forced_proxy:
        proxy = proxy_pool._as_url(force_proxy) if force_proxy else None
    else:
        proxy, _ip = proxy_pool.pick_for_run(route, email=email)
    if proxy:
        tried_proxies.add(proxy)
    proxy_label = proxy_pool.label(proxy) if proxy else "local"

    # keep_open is a single-shot debug mode — don't relaunch (that would close the
    # window you asked to keep) and don't rotate IPs.
    max_attempts = 1 if keep_open else _max_attempts()
    MAX_IP_TRIES = 1 if keep_open else settings().retry.max_ip_tries
    last_error = None
    last_error_infra = False  # was the last failure infra/network (no account strike)?
    ip_blocked = False
    for attempt in range(1, max_attempts + 1):
        logging.info(f"=== Attempt {attempt}/{max_attempts} (ip {proxy_label}) ===")
        traffic_sink = []  # bandwidth lines, logged AFTER this attempt's outcome
        try:
            slots = run_once_with_fresh_browser(source, dest, email, password, proxy,
                                                keep_open=keep_open,
                                                traffic_sink=traffic_sink)
            # The flow completed — reaching the appointment page and checking the
            # combos IS success. A combo that errored (e.g. VFS's spinner stalled)
            # is reported per-combo but does NOT fail the route.
            outcome = _outcome(source, dest, "OK", attempt, slot_results=slots,
                               account=account, proxy=proxy_label)
            # Reaching the dashboard and running the check means the account/IP
            # are healthy, so clear strikes.
            account_health.record_success(email, route)
            combo_errs = outcome.get("combo_errors") or []
            if combo_errs:
                logging.warning(
                    f"Success on attempt {attempt} — but {len(combo_errs)} combo(s) "
                    "errored (reported per-combo; route still OK)."
                )
            else:
                logging.info(f"Success on attempt {attempt}.")
            return outcome
        except (IpBlockedError, SignInDisabledError) as e:
            # Both mean "this IP isn't working here": a hard 403201 block, or
            # Cloudflare/Turnstile that wouldn't pass. Rotate to a DIFFERENT IP
            # instead of hammering (and re-downloading) the same one, and do NOT
            # penalise the account — it's the IP, not the user.
            is_block = isinstance(e, IpBlockedError)
            # Both are IP/infra, never the account's fault — so if this ends up
            # being the run's final failure, it must not strike the account.
            last_error_infra = True
            last_error = (f"IP blocked (403201) on {proxy_label}" if is_block
                          else f"Cloudflare/Turnstile not passed on {proxy_label}")
            newp = None
            if not forced_proxy and len(tried_proxies) < MAX_IP_TRIES:
                newp, _ip = proxy_pool.pick_for_run(
                    route, email=email, exclude=tried_proxies)
            if newp:
                proxy, proxy_label = newp, proxy_pool.label(newp)
                tried_proxies.add(newp)
                logging.warning(f"{route}: {last_error} [{account}] — rotating to a "
                                f"different IP {proxy_label}, retrying.")
                continue  # retry on the new IP (no backoff, not an account strike)
            if is_block:
                # 403201 and no alternate IP left: fail this run (don't hammer).
                logging.warning(f"{route}: {last_error} [{account}] — no alternate IP.")
                ip_blocked = True
                break
            # Turnstile failure with no alternate IP (local run / pool exhausted):
            # fall through to a same-IP retry — a fresh browser may pass.
            logging.warning(f"{route}: {last_error} [{account}] — no alternate IP; "
                            "retrying on the same IP.")
        except EmailNotRegisteredError as e:
            # Neutral: the account simply isn't registered on THIS portal — not a
            # success and not a failure, so leave its health untouched. Skip.
            logging.info(f"{source}-{dest}: account not registered here — skipping. ({e})")
            return _outcome(source, dest, "SKIPPED", attempt, error=str(e),
                            account=account, proxy=proxy_label)
        except GeoBlockedError as e:
            # IP/environment, not the account — do NOT touch account health.
            logging.error(f"Geo-blocked for {source}-{dest}: {e}")
            _alert_failure(source, dest, f"Geo-blocked (403203): {e}",
                           attempts=attempt, account=account)
            return _outcome(source, dest, "GEO", attempt, error=str(e),
                            account=account, proxy=proxy_label)
        except (InvalidCredentialsError, AccountBlockedError) as e:
            # Needs a HUMAN fix, not a timed cooldown: wrong password, or VFS's
            # 'Access Denied ... Unauthorised Activity (429002)'. DISABLE the
            # account indefinitely so we stop hitting it (repeated wrong logins
            # are exactly what triggers the 429002 block), until it's fixed and
            # flagged healthy: python -m src.utils.account_health clear <email>
            kind = "Invalid credentials" if isinstance(e, InvalidCredentialsError) \
                else "Access denied — unauthorised activity (429002)"
            logging.error(f"{kind} for {source}-{dest} [{account}]: {e}")
            account_health.disable(email, kind)
            _alert_failure(
                source, dest,
                f"{kind}: {e}\nAccount {account} DISABLED until fixed & flagged "
                f"healthy (python -m src.utils.account_health clear <email>).",
                attempts=attempt, account=account,
            )
            return _outcome(source, dest, "BLOCKED", attempt, error=f"{kind}: {e}",
                            account=account, proxy=proxy_label)
        except AccessRestrictedError as e:
            # 429001: bench this account for the hard cooldown so we stop hitting
            # it (and the block clears on its own). Other routes rotate onward.
            hrs = account_health.hard_cooldown_hours()
            logging.error(f"Access restricted for {source}-{dest} [{account}]: {e}")
            account_health.bench(email, route, hrs, "restricted-429001")
            _alert_failure(
                source, dest,
                f"Access restricted (429001): {e}\nAccount {account} benched {hrs}h.",
                attempts=attempt, account=account,
            )
            return _outcome(source, dest, "RESTRICTED", attempt, error=str(e),
                            account=account, proxy=proxy_label)
        except AccountLockedError as e:
            # 429202: bench this account for the hard cooldown (~its reset window).
            hrs = account_health.hard_cooldown_hours()
            logging.error(f"Account locked for {source}-{dest} [{account}]: {e}")
            account_health.bench(email, route, hrs, "locked-429202")
            _alert_failure(
                source, dest,
                f"Account locked (429202): {e}\nAccount {account} benched {hrs}h.",
                attempts=attempt, account=account,
            )
            return _outcome(source, dest, "LOCKED", attempt, error=str(e),
                            account=account, proxy=proxy_label)
        except UnsupportedCountryError as e:
            # Config problem, not the account's fault.
            logging.error(f"Unsupported route {source}-{dest}: {e}")
            _alert_failure(source, dest, str(e), attempts=attempt, account=account)
            return _outcome(source, dest, "FAILED", attempt, error=str(e),
                            account=account, proxy=proxy_label)
        except OtpVerificationError as e:
            # A real OTP failure (field never appeared / email never arrived /
            # code rejected on every submit) — NOT a Turnstile problem on the OTP
            # form (that's TurnstileRejectedError, caught above and handled with a
            # same-IP refresh). A second attempt just burns another OTP email +
            # browser and almost always fails the same way, so fail THIS route now
            # WITHOUT an attempt 2.
            logging.error(f"{route}: OTP verification failed [{account}] — not "
                          f"retrying (no attempt 2). {e}")
            benched = account_health.record_failure(email, route, f"OtpVerificationError: {e}")
            note = (f"\nAccount {account} benched {account_health.soft_cooldown_hours()}h "
                    "(too many consecutive failures).") if benched else ""
            _alert_failure(source, dest, f"OTP verification failed: {e}{note}",
                           attempts=attempt, account=account)
            return _outcome(source, dest, "FAILED", attempt, error=f"OTP failed: {e}",
                            account=account, proxy=proxy_label)
        except RetryableError as e:
            last_error = f"{type(e).__name__}: {e}"
            last_error_infra = _is_infra_error(e)
            logging.warning(f"Attempt {attempt} failed (retryable): {last_error}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            last_error_infra = _is_infra_error(e)
            logging.exception(f"Attempt {attempt} failed (unexpected): {last_error}")
        finally:
            # Emit this attempt's bandwidth accounting LAST — after the success
            # or failure line above — so it never sits atop the outcome it belongs
            # to (runs on return/continue/break/fall-through alike).
            for _line in traffic_sink:
                logging.info(_line)

        if attempt < max_attempts:
            backoff = settings().retry.backoff_seconds
            logging.info(f"Backing off {backoff}s before next attempt...")
            time.sleep(backoff)

    # IP block that couldn't be worked around (all tried IPs 403201, or forced/
    # no alternate). Do NOT penalise the account — it's the IP. Just alert & fail.
    if ip_blocked:
        n = len(tried_proxies) or 1
        msg = (f"IP blocked (403201) — tried {n} IP(s), all blocked. "
               f"Route failed this run; a different/residential IP is needed.")
        logging.error(f"{route}: {msg}")
        _alert_failure(source, dest, msg, attempts=n, account=account)
        return _outcome(source, dest, "FAILED", n, error=msg,
                        account=account, proxy=proxy_label)

    # Infra/network failure (proxy exit died, Cloudflare/Turnstile, CDP, geo):
    # NOT the account's fault, so fail the run WITHOUT striking the account — a
    # transient blip must not bench a good account for hours.
    if last_error_infra:
        logging.error(
            f"All {max_attempts} attempts failed (infrastructure/network — account "
            f"NOT struck). Last error: {last_error}"
        )
        _alert_failure(
            source, dest,
            (last_error or "unknown error")
            + "\n(Infrastructure/network issue — account NOT penalised.)",
            attempts=max_attempts, account=account,
        )
        return _outcome(source, dest, "FAILED", max_attempts,
                        error=(last_error or "unknown error"), account=account,
                        proxy=proxy_label)

    # Genuinely account-attributable stuck run: count a strike; the breaker
    # benches the account after enough consecutive strikes so we stop hammering it.
    logging.error(f"All {max_attempts} attempts failed. Last error: {last_error}")
    benched = account_health.record_failure(email, route, last_error or "stuck")
    note = (f"\nAccount {account} benched {account_health.soft_cooldown_hours()}h "
            "(too many consecutive failures).") if benched else ""
    _alert_failure(source, dest, (last_error or "unknown error") + note,
                   attempts=max_attempts, account=account)
    return _outcome(source, dest, "FAILED", max_attempts,
                    error=(last_error or "unknown error"), account=account,
                    proxy=proxy_label)


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
        # Re-check connectivity before each route: if the link drops mid-run,
        # stop here instead of letting every remaining route fail with
        # connection-refused (striking accounts, flooding the log). Cheap when
        # online (returns on the first probe). The startup gate in main() covers
        # "offline from the start"; this covers "went offline during the run".
        if not connectivity.internet_available():
            logging.error(
                f"Internet connectivity lost — stopping after "
                f"{idx - 1}/{len(routes)} route(s); remaining skipped "
                "(no accounts struck). Next scheduled run retries."
            )
            break
        logging.info(f"########## Route {idx}/{len(routes)}: {source}-{dest} ##########")
        try:
            # A fresh Chrome is opened and closed for this route inside run();
            # idx-1 drives the per-route proxy rotation (route N -> proxy N).
            outcome = run(source, dest, route_index=idx - 1)
        except Exception as e:
            # run() handles its own errors, but guard so one route can never
            # abort the whole loop.
            logging.exception(f"Route {source}-{dest} crashed unexpectedly: {e}")
            outcome = _outcome(source, dest, "FAILED", _max_attempts(), error=str(e))
        outcomes.append(outcome)
        # Log the route's final status at a level that matches it: a genuine
        # failure (FAILED/GEO/BLOCKED/LOCKED/RESTRICTED) is an ERROR, not INFO,
        # so it stands out in the log. OK/SKIPPED/PAUSED stay INFO.
        (logging.info if outcome.get("ok") else logging.error)(
            f"Route {source}-{dest} {outcome['status']}."
        )

    all_ok = all(o["ok"] for o in outcomes)
    logging.info(f"All routes done. Overall {'OK' if all_ok else 'with failures'}.")
    if settings().bandwidth.log_usage:
        from src.utils import proxy_forwarder
        logging.info(
            f"Total proxy traffic this run: {proxy_forwarder.session_mb():.1f} MB "
            f"across {len(routes)} route(s)."
        )
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
    # Log-file-only marker for "this run's output ends here" — deliberately
    # NOT part of the Telegram message itself (that shouldn't carry a divider
    # with nothing after it); printed last so it sits right before the next
    # run's startup log lines.
    logging.info("━━━━━━━━━━━━━━")


def _alert_failure(source: str, dest: str, error: str, attempts: int,
                   account: str = None) -> None:
    """Sends a Telegram alert that the run failed (best-effort).

    `account` is the masked label of the account that was used (passed in by the
    caller — it must NOT be recomputed here, because by the time we alert the
    account may already have been benched/disabled, which would change what a
    fresh lookup returns). Message layout lives in src/utils/telegram_message.py.
    """
    login_url = _vfs_url(source, dest) or ""
    if account is None:
        account = credentials.active_account(f"{source.upper()}-{dest.upper()}")
    msg = telegram_message.failure_alert(
        source, dest, error, attempts, login_url, account or ""
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
    proxy_grp = parser.add_mutually_exclusive_group()
    proxy_grp.add_argument(
        "--proxy", action="store_true",
        help="Force proxy-seller residential IPs for this run (overrides config).",
    )
    proxy_grp.add_argument(
        "--local", action="store_true",
        help="Force this PC's own IP for this run — no proxy (overrides config).",
    )
    # Test overrides (single-route only): force a specific account and/or proxy IP.
    parser.add_argument("--email", default=None,
                        help="TEST: force this account (single route). Password is "
                             "looked up from credentials, or pass --password.")
    parser.add_argument("--password", default=None,
                        help="TEST: password for --email (if not in credentials).")
    parser.add_argument("--proxy-url", dest="proxy_url", default=None,
                        help="TEST: force this proxy URL for the run, e.g. "
                             "http://user:pass@host:port (single route).")
    parser.add_argument("--keep-open", dest="keep_open", action="store_true",
                        help="DEBUG (single route): leave the browser open at the "
                             "end and wait for Enter, so you can inspect the page. "
                             "Disables retries/IP-rotation.")
    args = parser.parse_args()

    initialize_config()
    # -v gives fine-grained step logs; without it (prod/schedule) only major
    # events are logged. LOG_LEVEL is read by initialize_logger(), so set it first.
    if args.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    # One-run override of the [proxy] enabled config switch.
    if args.local:
        os.environ["VFS_PROXY"] = "off"
    elif args.proxy or args.proxy_url:
        os.environ["VFS_PROXY"] = "on"
    initialize_logger()

    # Connectivity gate: if this machine is offline, stop NOW — before touching
    # any route. Otherwise every route fails with connection-refused, strikes its
    # account, and floods the log with proxy/Telegram errors, all for a problem
    # that isn't ours. Exit 3 = "skipped: no connectivity" (distinct from 1 =
    # ran with failures); the next scheduled run retries when the link is back.
    from src.utils import connectivity
    if not connectivity.require_internet_or_log():
        sys.exit(3)

    if args.source_country_code and args.destination_country_code:
        outcome = run(
            args.source_country_code, args.destination_country_code,
            force_email=args.email, force_password=args.password,
            force_proxy=args.proxy_url, keep_open=args.keep_open,
        )
        _send_run_summary([outcome])
        ok = outcome["ok"]
    else:
        if args.email or args.proxy_url or args.keep_open:
            logging.warning("--email / --proxy-url / --keep-open only apply with "
                            "-sc/-dc (one route); ignored.")
        ok = run_all_routes()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
