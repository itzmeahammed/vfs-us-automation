"""Browser orchestration for an on-demand waitlist run.

The slot-check supervisor and this runner do the same first 90% — launch a
fresh Chrome, log in through the Cloudflare/OTP gauntlet, reach Appointment
Details, select a combination — so this module REUSES that machinery rather than
reimplementing it:

    ChromeProcess          launch + guaranteed kill (src/utils/chrome_launcher.py)
    get_vfs_bot(...)       the login flow (src/vfs_bot/vfs_bot_factory.py)
    slot_check helpers     the dropdown cascade and the slot banner read

What is different, and why this is its own module rather than a flag on the
supervisor:

  * It runs ON DEMAND, never on the hourly schedule.
  * It does NOT retry. The supervisor relaunches Chrome on any RetryableError,
    which is exactly wrong once a registration may have been submitted. Here a
    failure ends the run and reports.
  * It touches account health only to READ it (a benched account is skipped);
    a waitlist failure never counts a strike, because the account is not at
    fault for a page-shape surprise.

The roster comes from the client files themselves: every config/registrants/
<id>.json whose "route" matches. There is no separate targets file.
"""

import logging
import time
from typing import Dict, List, Optional

from src.settings import settings
from src.utils import proxy_pool
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import get_config_value, set_config_value
from src.vfs_bot import slot_check, turnstile
from src.vfs_bot.vfs_bot_factory import get_vfs_bot
from src.waitlist import accounts
from src.waitlist import config as waitlist_config
from src.waitlist import detect, guards, notify
from src.waitlist import register as register_mod
from src.waitlist import registrant as registrant_mod
from src.waitlist.errors import (
    WaitlistCommittedError,
    WaitlistConfigError,
    WaitlistNotOfferedError,
)
from src.waitlist.result import Status, WaitlistResult


class SlotsAvailable(Exception):
    """A combination the client is waiting on turned out to be BOOKABLE.

    Not an error — the opposite. It means waitlisting is the wrong action, so the
    run stops and reports the slot instead. Carries the combo and the banner text
    so the caller can say exactly what was found.
    """

    def __init__(self, combo: str, banner: str):
        self.combo = combo
        self.banner = banner
        super().__init__(f"Slots available for '{combo}': {banner}")


def _report_usage(bot, proxy_url, label: str) -> None:
    """Logs this run's data usage on the way out — success or failure.

    Two figures, deliberately, because they measure different things and only
    one of them is what the proxy seller bills:

      browser traffic  every byte the browser received, from the bot's own
                       meter. Works with OR without a proxy, so a local run and
                       a proxied run are comparable on the same number.
      proxy traffic    bytes that actually crossed the metered proxy — the
                       billed figure. Omitted on a local run, where it would
                       always read 0.0 MB and look like a broken meter.

    Mirrors supervisor.run_all_routes() so a waitlist run's cost can be compared
    against a slot-check run directly. Best-effort: a metering failure must never
    mask the outcome of the run itself.
    """
    if not settings().bandwidth.log_usage:
        return
    try:
        browser_bytes = getattr(bot, "_net_bytes", 0) or 0
        proxy_bytes = 0
        if proxy_url:
            from src.utils import proxy_forwarder
            proxy_bytes = proxy_forwarder.session_mb()

        # A run that failed before the browser started has no counters at all.
        # Printing "0.0 MB" there reads as a measurement ("we used nothing")
        # rather than the truth ("nothing ran"), so say nothing instead.
        if not browser_bytes and not proxy_bytes:
            logging.debug(f"No data usage to report for this {label} "
                          "(the browser never ran).")
            return

        if browser_bytes:
            logging.info(
                f"Browser traffic this {label}: "
                f"{browser_bytes / (1024 * 1024):.1f} MB "
                "(all requests, proxy or local IP).")

        blocked = getattr(bot, "_blocked_requests", 0) or 0
        if blocked:
            logging.info(
                f"Bandwidth: aborted {blocked} asset request(s) "
                "(image/media/font) — those bytes never hit the proxy.")

        if proxy_url:
            logging.info(
                f"Proxy traffic this {label}: {proxy_bytes:.1f} MB (billed).")
        else:
            logging.info("Egress was the local IP — no proxy bytes billed.")
    except Exception as e:
        logging.debug(f"Could not report data usage: {e}")


def _record_usage(proxy_url) -> None:
    """Bills this run's proxy bytes against the SHARED daily budget.

    The slot checker and the waitlist spend from the same metered proxy, so they
    must debit the same ledger — otherwise a waitlist run is invisible to the
    cap and the checker's own accounting is wrong by however much waitlisting
    used. supervisor.run_all_routes() does exactly this per route; a waitlist
    invocation is a single "route", so it records once on the way out.

    Only proxied runs are billed. On the local IP there is no metered upstream,
    so there is nothing to charge (and recording 0.0 would still rewrite the
    ledger file for no reason).

    record() also fires the one-shot 60%/100% Telegram alerts, which is why this
    is called even when the run failed: the bytes were spent either way, and a
    failing run that burns the cap is precisely the case worth alerting on.

    Best-effort — a metering failure must never mask the outcome of the run.
    """
    if not proxy_url:
        return
    try:
        from src.utils import bandwidth_budget, proxy_forwarder
        spent = proxy_forwarder.session_mb()
        if spent <= 0:
            return
        total = bandwidth_budget.record(spent)
        cap = bandwidth_budget.cap_mb()
        if cap > 0:
            logging.info(
                f"Daily proxy data: {total:.1f} / {cap:.0f} MB "
                f"({bandwidth_budget.percent_used():.0f}%) — "
                f"{bandwidth_budget.remaining_mb():.1f} MB left today.")
    except Exception as e:
        logging.debug(f"Could not record proxy usage against the daily cap: {e}")


def _check_budget() -> None:
    """Refuses to START a run when today's proxy allowance is already spent.

    Checked BEFORE the browser launches, mirroring supervisor.run_all_routes():
    once the cap is gone the next run would just add to the overspend. A run
    already in flight is never killed mid-way — the bytes are spent, and a
    half-driven registration is worse than a slightly-over-cap day.

    Raises WaitlistConfigError so it surfaces as a clean refusal rather than a
    traceback; it is a configuration//budget state, not a bug.
    """
    try:
        from src.utils import bandwidth_budget
        if not bandwidth_budget.is_exhausted():
            return
        used, cap = bandwidth_budget.used_mb(), bandwidth_budget.cap_mb()
    except WaitlistConfigError:
        raise
    except Exception as e:
        logging.debug(f"Could not read the daily proxy budget: {e}")
        return
    raise WaitlistConfigError(
        f"Daily proxy data cap spent ({used:.1f} of {cap:.0f} MB) — refusing to "
        "start. Resets at midnight, or "
        "'python -m src.utils.bandwidth_budget reset' to resume sooner.")


def _assert_account_healthy(email: str, route: str) -> None:
    """Refuses to log in with an account the circuit breaker has benched.

    The waitlist keeps its OWN account pool (nothing is read from
    credentials.local.ini), but account_health is keyed on (email, route) and is
    about the ACCOUNT, not about which subsystem is driving it. A VFS portal that
    has locked an account does not care that this login came from the waitlist —
    signing in again during a hard cooldown is exactly the hammering the breaker
    exists to stop, and it puts the client's existing waitlist entries at risk.

    Read-only here: the run is refused rather than the bench being extended, so
    a waitlist attempt can never deepen a cooldown the slot checker is serving.

    Raises WaitlistConfigError — a clean refusal, not a traceback.
    """
    try:
        from src.utils import account_health
        disabled = account_health.is_disabled(email)
        benched = account_health.is_benched(email, route)
        until = account_health.benched_until(email, route) if benched else 0
    except Exception as e:
        # Never let a health-file problem block a registration the user asked
        # for; the breaker is a safety net, not a gate of last resort.
        logging.debug(f"Could not read account health: {e}")
        return

    if disabled:
        raise WaitlistConfigError(
            f"Account {accounts.mask(email)} is DISABLED (wrong credentials or "
            "unauthorised) — refusing to sign in. Fix the account, then: "
            f"python -m src.utils.account_health clear {email}")
    if benched:
        mins = max(1, int((until - time.time()) / 60))
        raise WaitlistConfigError(
            f"Account {accounts.mask(email)} is benched on {route} for another "
            f"~{mins} min by the circuit breaker (a block or repeated failures). "
            "Signing in now risks the account. Wait for it to clear, or "
            f"python -m src.utils.account_health clear {email}")


def _record_account_outcome(email: str, route: str, ok: bool,
                            reason: str = "") -> None:
    """Feeds this run's result back into the shared circuit breaker.

    Without this the waitlist is a blind spot: it could fail login ten times in a
    row and the breaker would never bench the account, because only the
    supervisor was reporting. Both subsystems drive the same VFS accounts, so
    both must report.

    A success clears the account's strike count for the route, exactly as a
    successful slot check does.

    Best-effort: the run's own outcome has already been decided and journalled by
    the time this is called, and must not be disturbed by a bookkeeping failure.
    """
    try:
        from src.utils import account_health
        if ok:
            account_health.record_success(email, route)
        else:
            account_health.record_failure(email, route, reason or "waitlist run failed")
    except Exception as e:
        logging.debug(f"Could not record account health: {e}")


def _reset_usage() -> None:
    """Zeroes the proxy meter at the start of a run.

    proxy_forwarder._session_bytes is a per-PROCESS running total (the
    supervisor sums every route into one figure). A waitlist invocation is its
    own process, so it starts at zero anyway — but resetting explicitly keeps
    the reported number correct if a run is ever repeated in-process.
    """
    try:
        from src.utils import proxy_forwarder
        proxy_forwarder.reset_session_bytes()
    except Exception as e:
        logging.debug(f"Could not reset the proxy meter: {e}")


def _combo_parts(combo_label: str, route: str) -> Dict[str, str]:
    """Finds the centre/category/sub_category behind a combination LABEL.

    The label is what client files and the journal use (it is stable and
    human-readable); the dropdown values are what the page needs. Both live in
    config/routes/<ROUTE>.json, so this maps one to the other.
    """
    from src.utils.route_schema import get_route_schema

    source, _, dest = route.partition("-")
    schema = get_route_schema(source, dest)
    combos = schema.get("slot_check", {}).get("combinations", [])

    wanted = combo_label.strip().lower()
    matches = [c for c in combos
               if slot_check.combo_label(c).strip().lower() == wanted]

    if len(matches) > 1:
        # Two combos sharing a label is a config bug, and a dangerous one: the
        # lookup would silently take the first and register for a category the
        # client did not ask for. Refuse rather than guess.
        details = "; ".join(
            f"centre={c.get('centre')!r} category={c.get('category')!r} "
            f"sub_category={c.get('sub_category')!r}" for c in matches)
        raise WaitlistConfigError(
            f"'{combo_label}' matches {len(matches)} combinations in "
            f"config/routes/{route}.json — {details}. Labels must be unique "
            "within a route; give each one a distinct \"label\" so a client file "
            "can name exactly the combination it wants.")

    if matches:
        combo = matches[0]
        return {
            "centre": combo.get("centre", ""),
            "category": combo.get("category", ""),
            "sub_category": combo.get("sub_category", ""),
        }
    available = "; ".join(f"'{slot_check.combo_label(c)}'" for c in combos)
    raise WaitlistConfigError(
        f"Combination '{combo_label}' is not defined in config/routes/{route}.json. "
        f"A client's \"combos\" must use a label that exists there. Available: "
        f"{available or 'none'}"
    )


def _select_combo(page, combo_parts: Dict[str, str], combo_label: str) -> None:
    """Selects the combination's dropdowns on the Appointment Details page."""
    ok, detail = slot_check._select_combo(page, combo_parts, prev={})
    if not ok:
        raise WaitlistConfigError(
            f"Could not select combination '{combo_label}': {detail}")
    turnstile.wait_for_loader(page)
    page.wait_for_timeout(500)


def _roster(route: str, only_registrant: Optional[str]) -> List:
    """The clients this run should process."""
    if only_registrant:
        person = registrant_mod.load(only_registrant)
        if person.route != route:
            raise WaitlistConfigError(
                f"Client '{person.id}' targets route {person.route}, not {route}. "
                "Each client file targets exactly one route.")
        if not person.enabled:
            raise WaitlistConfigError(
                f"Client '{person.id}' is disabled (\"enabled\": false).")
        return [person]

    people = registrant_mod.for_route(route)
    if not people:
        parked = registrant_mod.for_route(route, include_disabled=True)
        if parked:
            raise WaitlistConfigError(
                f"Every client for {route} is disabled "
                f"({', '.join(p.id for p in parked)}). Set \"enabled\": true "
                "in the one you want to run.")
        raise WaitlistConfigError(
            f"No client files target {route}. Create "
            f"config/registrants/<name>.json with \"route\": \"{route}\" and "
            "the combination(s) to register for.")
    return people


def run_registration(source: str, dest: str,
                     registrant_id: Optional[str] = None,
                     only_combo: Optional[str] = None,
                     force_dry_run: Optional[bool] = None,
                     email: Optional[str] = None,
                     password: Optional[str] = None,
                     proxy: Optional[str] = None,
                     keep_open: bool = False) -> List[WaitlistResult]:
    """
    One on-demand waitlist run: log in once, then process each client waiting on
    this route, and each combination they listed.

    Returns a WaitlistResult per attempted combination. Never raises for an
    ordinary failure — the results carry the outcome — but two exceptions DO
    propagate, because neither may be quietly absorbed:

        SlotsAvailable          a real slot exists; waitlisting is wrong
        WaitlistCommittedError  an ambiguous submit needs a human
    """
    route = f"{source.upper()}-{dest.upper()}"
    logging.info(f"=== Waitlist run: {route} ===")
    logging.info(guards.describe())

    # Validate EVERYTHING before launching a browser.
    waitlist_config.get(route)                    # raises on a bad route config
    people = _roster(route, registrant_id)

    plan = []
    for person in people:
        combos = person.combos
        if only_combo:
            combos = [c for c in combos
                      if c.strip().lower() == only_combo.strip().lower()]
            if not combos:
                continue
        for combo in combos:
            plan.append((person, combo))

    if not plan:
        raise WaitlistConfigError(
            f"Nothing to do for {route}"
            + (f" with --combo '{only_combo}'" if only_combo else "")
            + ". Check the \"combos\" lists in the client files.")

    logging.info(
        f"Plan: {len(plan)} registration(s) across {len(people)} client(s) — "
        + "; ".join(f"{p.id}:{c}" for p, c in plan))

    # --- account ------------------------------------------------------------
    # Resolved from the CLIENT (or the CLI / the [waitlist] default), never from
    # the hourly slot-check rotation — a waitlist entry belongs to the account
    # that created it, so it must be a stable, deliberate choice.
    #
    # Every client in this plan must resolve to the SAME account, because one run
    # is one login. Mixed pins are a config error, caught here before Chrome
    # starts rather than halfway through.
    resolved = accounts.resolve(people[0], cli_email=email, cli_password=password)
    for person in people[1:]:
        other = accounts.resolve(person, cli_email=email, cli_password=password)
        if other.email.lower() != resolved.email.lower():
            raise WaitlistConfigError(
                f"Clients in this run pin different accounts: "
                f"'{people[0].id}' -> {resolved.masked} ({resolved.source}) but "
                f"'{person.id}' -> {other.masked} ({other.source}). One run is "
                "one login — run them separately with --registrant, or pin them "
                "to the same account.")
    account = resolved.masked
    logging.info(accounts.describe(resolved, route))

    proxy_url, how = accounts.resolve_proxy(resolved, route, cli_proxy=proxy)
    logging.info(
        f"Egress: {proxy_pool.label(proxy_url) if proxy_url else 'local IP'} "
        f"({how}) — pass --proxy-url \"\" to force local.")

    _assert_account_healthy(resolved.email, route)
    if proxy_url:
        _check_budget()

    url = get_config_value("vfs-url", route)
    if not url:
        raise WaitlistConfigError(f"No login URL for {route} in config/vfs_urls.ini.")

    results: List[WaitlistResult] = []
    committed_this_run = 0
    bot = None      # bound inside the try; needed by _report_usage in finally

    # Global run lock — taken AFTER validation (so a bad config still fails fast
    # without queueing behind another run) but BEFORE Chrome starts.
    #
    # This is what makes auto-triggering safe. journal.py documents that the
    # append-only journal assumes exactly ONE writer; the scheduled slot check,
    # an auto-triggered registration and a manual run are three potential
    # writers. Two at once can double-register a client — a real appointment
    # slot.
    #
    # This is the WAITLIST lane only. A scheduled slot check takes a different
    # lane and is no longer blocked by a registration: it never writes the
    # journal, never registers anyone, and uses a separate account pool, so the
    # two have nothing to serialise on. (They used to collide on a fixed Chrome
    # debugging port and a shared profile dir; chrome_launcher.py now gives each
    # run its own.)
    #
    # on_busy="raise" (unlike the supervisor's "skip"): a registration is
    # deliberate, so the caller must be TOLD it did not happen, not silently
    # given an empty result list.
    from src.utils import runlock
    with runlock.acquire("waitlist-run", lane=runlock.LANE_WAITLIST,
                         timeout=runlock.DEFAULT_TIMEOUT_SECONDS):
        return _run_registration_locked(
            route=route, source=source, dest=dest, plan=plan, url=url,
            resolved=resolved, account=account, proxy_url=proxy_url,
            force_dry_run=force_dry_run, keep_open=keep_open,
            results=results, committed_this_run=committed_this_run, bot=bot,
        )


def _run_registration_locked(*, route, source, dest, plan, url, resolved,
                             account, proxy_url, force_dry_run, keep_open,
                             results, committed_this_run,
                             bot) -> List[WaitlistResult]:
    """The browser-driving half of run_registration, holding the run lock.

    Split out purely so the lock's scope is explicit and covers every line that
    touches Chrome or the journal. See run_registration for the contract.
    """
    _reset_usage()
    chrome = ChromeProcess(port=settings().retry.cdp_port, url=url,
                           proxy=proxy_url, profile_key=resolved.email)
    try:
        chrome.start()
        # Set ON THE BOT as well as in the config: a concurrent slot check
        # launches its own Chrome, and the shared config key would leave both
        # bots attached to whichever started last.
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        egress_changed = bool(getattr(chrome, "egress_changed", False))
        set_config_value("browser", "keep_cf_clearance",
                         "false" if egress_changed else "true")

        bot = get_vfs_bot(source, dest)
        bot.cdp_url = chrome.cdp_url
        bot.keep_cf_clearance = not egress_changed
        bot.set_credential(resolved.email, resolved.password)
        page = _login_and_reach_appointment_page(bot, url)

        checkbox = waitlist_config.checkbox_selector(route)

        for person, combo_label in plan:
            logging.info(f"--- {person.label()} · {combo_label} ---")
            try:
                parts = _combo_parts(combo_label, route)
                _select_combo(page, parts, combo_label)

                # A real slot beats a waitlist. Finding one STOPS the run: the
                # right action is to go book it, not to queue for a waitlist.
                banner = slot_check.read_slot_message(
                    page, timeout=settings().timeouts.slot_read_ms)
                if banner:
                    logging.warning(
                        f"SLOTS AVAILABLE for '{combo_label}' — "
                        f"{banner.splitlines()[0]}")
                    logging.warning(
                        "Stopping the run: a bookable slot exists, so "
                        "waitlisting is not the right action. Book it instead.")
                    raise SlotsAvailable(combo_label, banner)

                if not detect.is_offered(page, checkbox):
                    logging.info(
                        f"'{combo_label}': no slots and no waitlist offered.")
                    results.append(WaitlistResult(
                        route=route, combo=combo_label, registrant_id=person.id,
                        status=Status.SKIPPED, account=account,
                        reason="no waitlist offered for this combination",
                    ).finish(Status.SKIPPED))
                    continue

                # How many clients one account may carry — and whether it may
                # hold two entries for the same combination — is not documented
                # by VFS, so both are configurable rather than assumed.
                allowed, why = accounts.capacity_verdict(
                    resolved, person, route, combo_label)
                if not allowed:
                    logging.warning(f"'{combo_label}': {why}")
                    results.append(WaitlistResult(
                        route=route, combo=combo_label, registrant_id=person.id,
                        status=Status.SKIPPED, account=account, reason=why,
                    ).finish(Status.SKIPPED))
                    continue

                result = register_mod.register(
                    page, route=route, combo=combo_label, registrant=person,
                    account=account, combo_parts=parts,
                    attempted_this_run=committed_this_run,
                    force_dry_run=force_dry_run,
                )
                results.append(result)
                if result.committed:
                    committed_this_run += 1
                notify.notify_registered(result, login_url=url)

            except WaitlistNotOfferedError as e:
                results.append(WaitlistResult(
                    route=route, combo=combo_label, registrant_id=person.id,
                    status=Status.SKIPPED, account=account, reason=str(e),
                ).finish(Status.SKIPPED))
            except WaitlistCommittedError:
                # Already journalled as 'unknown' and alerted by register().
                # STOP the whole run: with an ambiguous submit outstanding, the
                # safe move is to touch nothing else on this account.
                logging.error(
                    "Stopping the run — a submit is unresolved. Verify on the "
                    "VFS account before running again.")
                raise
            except WaitlistConfigError as e:
                logging.error(f"'{combo_label}': {e}")
                results.append(WaitlistResult(
                    route=route, combo=combo_label, registrant_id=person.id,
                    status=Status.FAILED, account=account, reason=str(e),
                ).finish(Status.FAILED))

    finally:
        if keep_open:
            try:
                input("\n[keep-open] Browser left open. Press Enter to close it...")
            except EOFError:
                pass
        # Report usage BEFORE closing Chrome: the counters live on the bot and
        # the forwarder, but reading them after teardown risks a half-torn-down
        # state. Reported on failure too — a run that died still cost MB.
        # Feed the shared circuit breaker. "Reached the portal" is the signal,
        # NOT "every client registered": a combination with no waitlist on offer
        # is a perfectly healthy run, and striking the account for it would bench
        # it for a VFS state that has nothing to do with the account. `bot` is
        # only bound once _login_and_reach_appointment_page() has returned, so it
        # is exactly the "did we get in?" flag — and it stays None when login
        # threw, which is the case worth counting.
        _record_account_outcome(
            resolved.email, route, ok=bot is not None,
            reason="waitlist run failed before reaching the portal")
        _report_usage(bot, proxy_url, "run")
        _record_usage(proxy_url)
        chrome.close()

    return results


def run_doctor(source: str, dest: str, combo: Optional[str] = None,
               registrant_id: Optional[str] = None, walk: bool = False,
               email: Optional[str] = None, password: Optional[str] = None,
               proxy: Optional[str] = None,
               keep_open: bool = False,
               harvests: Optional[List] = None) -> List:
    """
    Checks this route's configured selectors against the LIVE portal.

    READ-ONLY at the default depth: it logs in, selects a combination and probes
    the Appointment Details controls without typing, ticking or submitting.

    `walk=True` additionally ticks the checkbox and submits to reach the later
    pages so their selectors can be probed too. That ADVANCES the booking form
    (no registration is created — it stops before the committing step), so it is
    opt-in and used when mapping a new country.

    Returns a list of doctor.Finding.
    """
    from src.waitlist import doctor

    route = f"{source.upper()}-{dest.upper()}"
    cfg = waitlist_config.get(route)
    logging.info(f"=== Config health check: {route} "
                 f"({'walk' if walk else 'page'} depth) ===")

    # Pick a client only to resolve the account and a combination to look at —
    # nothing of theirs is ever typed.
    people = _roster(route, registrant_id)
    person = people[0]
    combo_label = combo or (person.combos[0] if person.combos else None)
    if not combo_label:
        raise WaitlistConfigError(
            f"No combination to check. Pass --combo, or list one in "
            f"config/registrants/{person.id}.json.")

    resolved = accounts.resolve(person, cli_email=email, cli_password=password)
    logging.info(f"Using account {resolved.masked} ({resolved.source}), "
                 f"combination '{combo_label}'.")

    proxy_url, how = accounts.resolve_proxy(resolved, route, cli_proxy=proxy)
    logging.info(
        f"Egress: {proxy_pool.label(proxy_url) if proxy_url else 'local IP'} "
        f"({how}) — pass --proxy-url \"\" to force local.")

    if proxy_url:
        _check_budget()

    url = get_config_value("vfs-url", route)
    if not url:
        raise WaitlistConfigError(f"No login URL for {route} in config/vfs_urls.ini.")

    findings, checked = [], []
    bot = None      # bound inside the try; needed by _report_usage in finally
    _reset_usage()
    chrome = ChromeProcess(port=settings().retry.cdp_port, url=url,
                           proxy=proxy_url, profile_key=resolved.email)
    try:
        chrome.start()
        # Set ON THE BOT as well as in the config: a concurrent slot check
        # launches its own Chrome, and the shared config key would leave both
        # bots attached to whichever started last.
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        egress_changed = bool(getattr(chrome, "egress_changed", False))
        set_config_value("browser", "keep_cf_clearance",
                         "false" if egress_changed else "true")

        bot = get_vfs_bot(source, dest)
        bot.cdp_url = chrome.cdp_url
        bot.keep_cf_clearance = not egress_changed
        bot.set_credential(resolved.email, resolved.password)
        page = _login_and_reach_appointment_page(bot, url)

        parts = _combo_parts(combo_label, route)
        _select_combo(page, parts, combo_label)

        banner = slot_check.read_slot_message(
            page, timeout=settings().timeouts.slot_read_ms)
        if banner:
            logging.info(f"NOTE: '{combo_label}' currently HAS slots — the "
                         "waitlist checkbox will not be shown.")

        findings.append(doctor.check_checkbox(page, route))

        steps = [s for s in cfg["steps"] if not s.get("disabled")]
        # The first step lives on the page we are already on.
        if steps:
            findings.extend(doctor.check_step(page, steps[0]))
            checked.append(steps[0]["name"])
            if harvests is not None:
                harvests.extend(doctor.harvest_step(page, steps[0]))

        if walk and len(steps) > 1:
            logging.warning(
                "walk depth: ticking the waitlist checkbox and submitting to "
                "reach the later pages. No registration is created (it stops "
                "before the committing step), but the form DOES advance.")
            findings.extend(
                _walk_and_check(page, route, cfg, steps, checked,
                                harvests=harvests))

    finally:
        if keep_open:
            try:
                input("\n[keep-open] Browser left open. Press Enter to close it...")
            except EOFError:
                pass
        _report_usage(bot, proxy_url, "check")
        _record_usage(proxy_url)
        chrome.close()

    missed = [s for s in doctor.unreachable_steps(route, checked)]
    if missed:
        logging.info(
            f"Not checked at this depth: {', '.join(missed)} "
            "(re-run with --walk to reach them).")
    return findings


def _walk_and_check(page, route: str, cfg: dict, steps: List[dict],
                    checked: List[str], harvests: Optional[List] = None) -> List:
    """Advances through the pre-commit steps, probing each. Never commits."""
    from src.waitlist import doctor
    from src.waitlist.register import _await_page, _click, _tick_checkbox

    findings = []
    checkbox = cfg.get("checkbox") or detect.DEFAULT_CHECKBOX_SELECTOR
    try:
        _tick_checkbox(page, checkbox, dry_run=False)
    except Exception as e:
        logging.error(f"Could not tick the waitlist checkbox — stopping walk: {e}")
        return findings

    for index, step in enumerate(steps):
        if step.get("commits"):
            logging.info(
                f"Reached the committing step '{step['name']}' — probing its "
                "controls but NOT submitting.")
            try:
                _await_page(page, step, 45000)
                findings.extend(doctor.check_step(page, step))
                checked.append(step["name"])
                if harvests is not None:
                    harvests.extend(doctor.harvest_step(page, step))
            except Exception as e:
                findings.append(doctor.Finding(
                    step["name"], "page", doctor.Finding.MISSING, str(e)))
            break

        if index == 0:
            # Already probed; just submit to move on.
            try:
                _click(page, step.get("submit"), f"Step '{step['name']}'", 45000)
                page.wait_for_timeout(1500)
            except Exception as e:
                logging.error(f"Could not advance past '{step['name']}': {e}")
                break
            continue

        try:
            _await_page(page, step, 45000)
            findings.extend(doctor.check_step(page, step))
            checked.append(step["name"])
            if harvests is not None:
                harvests.extend(doctor.harvest_step(page, step))
            # Later steps usually need their fields filled before the submit
            # enables, so the walk stops here rather than faking data.
            logging.info(
                f"Probed '{step['name']}'. Stopping the walk here — advancing "
                "further would require filling the form.")
            break
        except Exception as e:
            findings.append(doctor.Finding(
                step["name"], "page", doctor.Finding.MISSING, str(e)))
            break

    return findings


def _login_and_reach_appointment_page(bot, url: str):
    """Runs the existing login flow and returns the page on Appointment Details.

    Rather than duplicating the Cloudflare/OTP gauntlet, this drives VfsBot's own
    methods in the same order run() does, stopping at the appointment page
    instead of running the slot check.

    The credential was already resolved and injected by the caller via
    set_credential(), so _resolve_credential() returns THAT account rather than
    self-selecting from the rotation.
    """
    from playwright.sync_api import sync_playwright

    from src.vfs_bot import browser_setup, diagnostics

    bot._load_schema_and_selectors()
    email, password = bot._resolve_credential(
        f"{bot.source_country_code}-{bot.destination_country_code}")
    bot.active_email = email
    diagnostics.set_route(bot.destination_country_code)

    # NOTE: the Playwright context must outlive this function, so it is stashed
    # on the bot rather than closed here; ChromeProcess.close() tears the whole
    # browser down at the end of the run.
    playwright = sync_playwright().start()
    bot._playwright = playwright

    browser, context, page = browser_setup.launch_or_attach(
        playwright,
        get_config_value("browser", "type", "chromium"),
        get_config_value("browser", "headless", "True"),
        get_config_value("browser", "cdp_url"),
    )
    bot._instrument_page(page, context)
    try:
        page.set_viewport_size({"width": 1280, "height": 1024})
    except Exception:
        pass

    page.goto(url, timeout=settings().timeouts.page_load_ms,
              wait_until="domcontentloaded")
    bot._check_blocked(page)
    bot.pre_login_steps(page)

    # authenticate() + start_booking(), NOT login() — login() also runs the full
    # slot check over every combination in config/routes/<ROUTE>.json, which is
    # wrong here on three counts: it burns ~20s per combination the client never
    # asked for, it fires the "waitlist available" Telegram notice, and it leaves
    # the dropdowns on the LAST combo so we re-select ours immediately after.
    # This runner needs exactly one thing: an authenticated session sitting on
    # Appointment Details, ready for the client's own combination.
    bot.authenticate(page, email, password)
    # await_page=True: run_slot_check() normally performs this wait, and we are
    # deliberately not calling it — without this the dropdowns could be driven
    # before Appointment Details has rendered.
    bot.start_booking(page, await_page=True)

    return page
