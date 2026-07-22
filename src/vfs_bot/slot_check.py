"""The slot-check flow: Appointment Details dropdowns -> the earliest-slot
banner -> the Telegram report.

This is the bot's actual purpose (everything before it — login, Turnstile,
OTP — just gets us to this page). Kept as its own module so the "what do we
check and how do we report it" business logic is separable from the Cloudflare
gauntlet in turnstile.py / otp_flow.py.

cascade_steps() is deliberately a PURE function (no `page`, no I/O) — the
dropdown cascade logic (which of centre/category/sub-category actually needs
re-selecting for this combination) is the one piece of real decision-making in
this module, so it's worth being able to unit test it without Playwright (see
tests/test_slot_check.py).
"""

import logging

from src.settings import settings
from src.utils.config_reader import get_config_value
from src.vfs_bot import diagnostics, turnstile
from src.vfs_bot.errors import SlotCheckError

# ===== Angular Material dropdown selection ==================================


def select_mat_dropdown(page, control_name: str, value: str,
                         attempts: int = 3, option_timeout_ms: int = 20000) -> bool:
    """
    Selects an option in an Angular Material dropdown (`mat-select`).

    Opens the dropdown identified by its `formcontrolname`, WAITS for the
    options to actually load (they arrive async, often behind a spinner), then
    clicks the option whose visible text contains `value` (case-insensitive
    substring).

    Robust to slow option loading: instead of a fixed 1s wait it polls up to
    `option_timeout_ms` for THIS option to become visible, and retries the
    whole open→select a few times. On any failure it presses Escape to close a
    half-open overlay first — otherwise a stuck overlay would intercept clicks
    and break the NEXT dropdown too.

    Returns:
        bool: True if the option was selected, False otherwise.
    """
    for attempt in range(1, attempts + 1):
        try:
            turnstile.wait_for_loader(page)  # lists load behind a full-screen spinner
            trigger = page.locator(
                f"mat-select[formcontrolname='{control_name}']"
            ).first
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)

            # The overlay opens, then its options load in async. Clear any
            # spinner, then wait for THIS option to actually render before
            # clicking it (the old fixed 1s wait was too short on slow loads).
            page.wait_for_timeout(200)
            turnstile.wait_for_loader(page)
            option = page.get_by_role("option", name=value, exact=False).first
            option.wait_for(state="visible", timeout=option_timeout_ms)
            option.scroll_into_view_if_needed(timeout=5000)
            option.click(timeout=10000)

            logging.debug(
                f"Selected '{value}' (dropdown: '{control_name}')"
                + (f" on attempt {attempt}" if attempt > 1 else "")
            )
            page.wait_for_timeout(1000)
            turnstile.wait_for_loader(page)  # let the dependent dropdown reload
            return True
        except Exception as e:
            logging.warning(
                f"Attempt {attempt}/{attempts}: could not select '{value}' for "
                f"'{control_name}': {e}"
            )
            # Close any half-open overlay so it can't block the next dropdown.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(700)
            except Exception:
                pass
            if attempt < attempts:
                turnstile.wait_for_loader(page)
                page.wait_for_timeout(1500)

    diagnostics.take_screenshot(page, "ERROR_dropdown")
    return False


def read_slot_message(page, timeout: int = 12000) -> str:
    """
    Returns the 'Earliest available slot ...' banner text on the Appointment
    Details step, or "" if none is shown (no availability for the chosen combo).

    VFS can show SEVERAL of these banners at once — one per applicant count,
    e.g. 'Earliest available slot for 1 Applicants is : 05-08-2026' AND
    '... for 2 Applicants is : 11-08-2026'. Each is its own role=alert div, so
    ALL of them are collected and newline-joined (the old code read only .first
    and silently dropped the rest).
    """
    try:
        turnstile.wait_for_loader(page)
        # Same single wait as before (so no-availability timing is unchanged):
        # wait for the FIRST banner; "" if none appears within the timeout.
        page.get_by_text("Earliest available slot", exact=False).first.wait_for(
            timeout=timeout)
        # A slot exists — now grab EVERY banner (instant; no extra wait).
        banners = page.get_by_role("alert").filter(has_text="Earliest available slot")
        texts = []
        for i in range(banners.count()):
            t = banners.nth(i).inner_text().strip()
            if t and t not in texts:
                texts.append(t)
        if texts:
            return "\n".join(texts)
        # Fallback for any markup without role=alert: the single first banner.
        return page.get_by_text(
            "Earliest available slot", exact=False).first.inner_text().strip()
    except Exception:
        return ""


# ===== Dashboard -> booking =================================================


def start_new_booking(page) -> None:
    """Clicks the 'Start New Booking' button on the VFS dashboard."""
    try:
        page.wait_for_timeout(2000)
        # VFS renders two copies of this button (responsive: one for mobile,
        # one for desktop) — one is CSS-hidden at any given viewport. Target
        # the <button> (not its inner <span>) and keep only the visible copy,
        # otherwise the click lands on the hidden element and times out.
        booking_button = (
            page.locator("button:has-text('Start New Booking')")
            .filter(visible=True)
            .first
        )
        booking_button.scroll_into_view_if_needed(timeout=10000)
        booking_button.click(timeout=15000)
        logging.debug("Clicked Start New Booking")
        # Brief settle only — run_slot_check() right after does a proper
        # wait_for_url("**/application-detail"), which is the real gate.
        page.wait_for_timeout(800)
        diagnostics.take_screenshot(page, "06_start_new_booking")
        logging.debug(f"Start New Booking opened. URL: {page.url}")
    except Exception as e:
        logging.warning(f"Start New Booking failed: {e}")
        diagnostics.take_screenshot(page, "ERROR_start_new_booking")


# ===== Cascading centre / category / sub-category dropdowns ================

# (Playwright control name, combo dict key) for each cascade level, in the
# order they must be selected — a parent level resets its children in VFS's
# form, so a change at any level forces every level below it to be re-picked.
_CASCADE_LEVELS = (
    ("centerCode", "centre"),
    ("selectedSubvisaCategory", "category"),
    ("visaCategoryCode", "sub_category"),
)

# key -> human-readable name, used only in the 'could not select X' message.
_FRIENDLY_NAME = {"centre": "centre", "category": "category", "sub_category": "sub-category"}


def cascade_steps(combo: dict, prev: dict) -> list:
    """
    Pure logic: returns the (control_name, key, value) triples that must be
    (re)selected for `combo`, given what was selected for the previous combo
    (`prev`, same shape: {"centre": ..., "category": ..., "sub_category": ...}).

    A level is skipped only if BOTH its value is unchanged from `prev` AND no
    earlier (parent) level in this combo changed — once a parent changes, VFS
    resets every dependent dropdown below it, so those must be re-selected
    even if their target value happens to match what was there before.
    """
    steps = []
    changed = False
    for control, key in _CASCADE_LEVELS:
        value = combo.get(key)
        if not value:
            continue
        if not changed and prev.get(key) == value:
            continue
        changed = True
        steps.append((control, key, value))
    return steps


def combo_label(combo: dict) -> str:
    """The display label for a combination: its explicit "label", or the
    centre/category/sub-category joined."""
    return combo.get("label") or " / ".join(
        filter(None, [combo.get("centre"), combo.get("category"), combo.get("sub_category")])
    )


def _select_combo(page, combo: dict, prev: dict) -> tuple:
    """Selects one combination's dropdowns. Returns (ok, fail_detail)."""
    for control, key, value in cascade_steps(combo, prev):
        if select_mat_dropdown(page, control, value):
            continue
        friendly = _FRIENDLY_NAME.get(key, key)
        return False, f"could not select {friendly} '{value}'"
    return True, None


# ===== Orchestration: run every configured combination + report ============


def run_slot_check(page, schema: dict, source_country_code: str,
                    destination_country_code: str) -> list:
    """
    Slot-check flow (no booking): on the Appointment Details step, run through
    each configured combination (centre / category / sub-category), read the
    earliest-slot banner for each, then send all results in one Telegram
    message. Combinations come from the route schema's `slot_check.combinations`.

    Returns the (label, message) results list (the caller stores it as
    `bot.slot_results` for the supervisor's run summary).
    """
    all_combos = schema.get("slot_check", {}).get("combinations", [])
    # JSON has no comments, so combinations are switched off with
    # `"disabled": true` instead of being deleted from the route file.
    combos = [c for c in all_combos if not c.get("disabled")]
    if len(combos) < len(all_combos):
        logging.info(
            f"Skipping {len(all_combos) - len(combos)} disabled combination(s)."
        )
    if not combos:
        logging.warning("Slot-check mode but no combinations configured.")
        return []

    try:
        page.wait_for_url("**/application-detail", timeout=30000)
    except Exception as e:
        raise SlotCheckError(
            f"Did not reach the Appointment Details page; cannot check slots: {e}"
        ) from e

    turnstile.wait_for_loader(page)
    page.wait_for_timeout(500)

    from src.vfs_bot import waitlist  # local: keeps waitlist evolvable independently

    results = []
    prev = {}  # the centre/category/sub-category selected for the previous combo
    for combo in combos:
        label = combo_label(combo)
        logging.info(f"Checking slot for: {label}")

        ok, fail_detail = _select_combo(page, combo, prev)
        prev = {
            "centre": combo.get("centre"),
            "category": combo.get("category"),
            "sub_category": combo.get("sub_category"),
        }

        if not ok:
            # 'ERROR:' prefix marks a real slot-search failure (as opposed to
            # genuine 'no availability'), so the supervisor can flag the route
            # as failed and name the combo + reason in the run summary.
            message = f"ERROR: {fail_detail or 'could not select this combination'}"
        else:
            message = read_slot_message(page, timeout=settings().timeouts.slot_read_ms)
            if not message:
                # No slot banner. VFS may instead offer a waitlist checkbox —
                # detect it (read-only) and, if present, mark this combo as
                # 'waitlist' instead of plain 'no availability'.
                message = (waitlist.as_result() if waitlist.is_offered(page)
                           else "No slot message shown (no availability?).")

        logging.info(f"  -> {message}")
        results.append((label, message))
        diagnostics.take_screenshot(page, f"slot_{len(results)}")
        page.wait_for_timeout(400)

    # Disabled combos are carried in the results with a 'DISABLED' marker so
    # the run summary can show them; no date in the text keeps them out of
    # the slot report and the slot count.
    for combo in all_combos:
        if combo.get("disabled"):
            results.append((combo_label(combo), "DISABLED"))

    # Send the (unchanged) per-route slot report to the success chat.
    send_slot_report(source_country_code, destination_country_code, results)

    # Separately notify (success chat) about any waitlist-only combinations —
    # the slot report above intentionally ignores them (they carry no date).
    url_key = f"{source_country_code}-{destination_country_code}"
    waitlist.notify(
        source_country_code, destination_country_code,
        results, get_config_value("vfs-url", url_key, ""),
    )

    return results


def send_slot_report(source_country_code: str, destination_country_code: str,
                      results: list) -> None:
    """Formats a route's slot results and sends them to Telegram.

    The message layout lives in src/utils/telegram_message.py — edit it there.
    It is route-aware, so each portal's message shows the correct destination.
    """
    from src.utils import telegram, telegram_message

    url_key = f"{source_country_code}-{destination_country_code}"
    login_url = get_config_value("vfs-url", url_key, "")
    report = telegram_message.slot_report(
        source_country_code, destination_country_code, results, login_url,
    )

    # Only notify when there's an actual slot. slot_report() returns "" when
    # no combination has availability, so we skip sending entirely.
    if not report:
        logging.info("No available slots for this route — no Telegram message sent.")
        return

    logging.info("Slot report:\n" + report)
    if telegram.is_configured():
        telegram.send_message(report)
    else:
        logging.warning(
            "Telegram not configured — slot report logged only. "
            "Set [telegram] bot_token and chat_id in config.ini to receive it."
        )
