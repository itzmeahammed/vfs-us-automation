"""A block that lands DURING the slot check must fail fast and rotate the IP.

The field failure this pins down (AE-HUN, 2026-08-17): VFS served its
'Permission Issues (403)' page right after the first combination was read. With
no liveness check past the dashboard, the flow spent 3 combos x 3 attempts x
~12s timing out against dropdowns that were no longer in the DOM, then reported
the route OK with "no availability" — a false negative that also cleared the
account's failure strikes.

Two guarantees are tested here:
  * slot_check bails on the FIRST failed attempt once the page is dead, and
    surfaces it as a typed block error rather than a per-combo 'ERROR:' string
    that the supervisor treats as a still-OK route;
  * the supervisor answers that block by ROTATING to a different IP, and only
    gives up (GEO) once the IP budget is spent — never striking the account.

Run: python -m unittest tests.test_mid_run_block
"""

import unittest
from unittest import mock

from src import supervisor
from src.vfs_bot import page_guard, slot_check
from src.vfs_bot.errors import GeoBlockedError

PERMISSION_ISSUES = (
    "Permission Issues (403)\nIt seems like you're encountering a permission issue."
)


# ===== slot_check: stop grinding a dead form ================================


class _DeadLocator:
    """A locator for a control that is no longer in the DOM. Any wait on it
    would time out in a real browser — the point is that we never get there."""

    def __init__(self):
        self.waits = 0

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False        # no loader overlay — the page is simply gone

    def wait_for(self, **kwargs):
        pass                # the form HAD rendered; the block lands later

    def scroll_into_view_if_needed(self, **kwargs):
        self.waits += 1
        raise AssertionError("waited on a control that is not in the DOM")


APP_URL = "https://visa.vfsglobal.com/are/en/hun/application-detail"
ERROR_URL = "https://visa.vfsglobal.com/are/en/hun/error"


class _BlockedPage:
    """A page showing VFS's 403 block page; every locator resolves to nothing.

    `url` defaults to the app route the flow *thinks* it is on — the harder case,
    where only a DOM pass can tell the page is dead. Pass ERROR_URL for the case
    where the Angular app actually routed us to its error view, which the free
    sentinel check catches on its own.
    """

    def __init__(self, url=APP_URL):
        self.url = url
        self.locators = []       # every locator handed out
        self.dropdowns = []      # only the mat-select probes (not the loader check)
        self.main_frame = self

    def locator(self, selector):
        loc = _DeadLocator()
        self.locators.append(loc)
        if "mat-select" in selector:
            self.dropdowns.append(loc)
        return loc

    def evaluate(self, script):
        if "heading" in script:
            return {"heading": "Permission Issues (403)", "msg": "", "url": self.url}
        return PERMISSION_ISSUES

    def content(self):
        return PERMISSION_ISSUES

    def get_by_role(self, *a, **k):
        return _DeadLocator()

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_url(self, _pattern, **kwargs):
        pass

    def screenshot(self, **kwargs):
        pass

    @property
    def keyboard(self):
        raise AssertionError("pressed Escape on a page that is already gone")


class TestSlotCheckFailsFast(unittest.TestCase):
    def setUp(self):
        # wait_for_loader would otherwise assert liveness itself; neutralise it so
        # these tests exercise select_mat_dropdown's OWN fast-bail path.
        patcher = mock.patch.object(slot_check.turnstile, "wait_for_loader")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_blocked_page_raises_instead_of_returning_false(self):
        # A False return becomes a per-combo "ERROR: could not select ..." string,
        # which the supervisor reports as a still-OK route. On a dead page it must
        # raise so the route fails and the IP rotates.
        page = _BlockedPage()
        with self.assertRaises(GeoBlockedError):
            slot_check.select_mat_dropdown(page, "visaCategoryCode", "Tourist")

    def test_no_timeout_is_ever_spent_on_a_missing_control(self):
        page = _BlockedPage()
        with self.assertRaises(GeoBlockedError):
            slot_check.select_mat_dropdown(page, "centerCode", "Dubai")
        self.assertTrue(all(loc.waits == 0 for loc in page.dropdowns),
                        "must not wait on controls that are absent from the DOM")

    def test_missing_control_on_a_live_page_does_not_retry(self):
        # Same "control absent" state but the page is healthy — no block error,
        # yet still no point retrying: three more waits cannot conjure it back.
        page = _BlockedPage()
        with mock.patch.object(slot_check.page_guard, "assert_alive"), \
             mock.patch.object(slot_check.diagnostics, "take_screenshot"):
            ok = slot_check.select_mat_dropdown(page, "centerCode", "Dubai")
        self.assertFalse(ok)
        self.assertEqual(len(page.dropdowns), 1,
                         "the dropdown should be probed once, not once per attempt")


class TestSlotReadNeverFakesNoAvailability(unittest.TestCase):
    """read_slot_message returns "" for genuine no-availability. On a dead page
    it must never return "" — that is precisely how the AE-HUN run reported
    combinations it had never actually checked.

    Two layers do this, and the split is deliberate:
      * once the app has ROUTED to its error view, or a VFS document was
        refused, the free sentinel catches it inside wait_for_loader — no DOM
        read, no waiting;
      * when the block is only visible in the DOM (same URL, no refused
        document, nothing flagged), the free check cannot know, so the empty
        read is resolved by run_slot_check's deep check before it is allowed to
        mean "no availability".

    wait_for_loader is deliberately NOT mocked here — it is the checkpoint.
    """

    def test_guard_fires_from_inside_wait_for_loader_on_an_error_route(self):
        with self.assertRaises(GeoBlockedError):
            slot_check.turnstile.wait_for_loader(_BlockedPage(url=ERROR_URL))

    def test_error_route_raises_instead_of_reading_as_no_slots(self):
        with self.assertRaises(GeoBlockedError):
            slot_check.read_slot_message(_BlockedPage(url=ERROR_URL), timeout=100)

    def test_a_flagged_document_403_is_enough_to_raise(self):
        # No error route — the app is still on /application-detail. The sentinel
        # flag from the refused document is what makes this catchable for free.
        page = _BlockedPage()
        page_guard.install(page)
        page_guard.suspect(page, "VFS returned HTTP 403 for /application-detail")
        with self.assertRaises(GeoBlockedError):
            slot_check.read_slot_message(page, timeout=100)

    def test_silent_dom_only_block_still_cannot_report_no_availability(self):
        # The worst case: same URL, no refused document, nothing flagged. The
        # free check genuinely cannot know, so the empty read reaches
        # run_slot_check — whose deep check must catch it there.
        page = _BlockedPage()
        schema = {"slot_check": {"combinations": [{"centre": "Dubai"}]}}
        with mock.patch.object(slot_check.turnstile, "wait_for_loader"), \
             mock.patch.object(slot_check, "read_slot_message", return_value=""), \
             mock.patch.object(slot_check, "_select_combo", return_value=(True, None)), \
             mock.patch.object(slot_check, "send_slot_report") as report:
            with self.assertRaises(GeoBlockedError):
                slot_check.run_slot_check(page, schema, "AE", "HUN")
        report.assert_not_called()


# ===== supervisor: answer a block by rotating the IP ========================


def _run_with(side_effect, ip_count=4):
    """Drive supervisor.run() with a pool of distinct proxies, all side effects
    mocked. Returns (outcome, run_mock, pick_mock, record_failure_mock)."""
    from src.utils.config_reader import initialize_config
    initialize_config()

    proxies = [(f"http://ip{i}", f"9.9.9.{i}") for i in range(1, ip_count + 1)]
    with mock.patch.object(supervisor, "run_once_with_fresh_browser",
                           side_effect=side_effect) as roc, \
         mock.patch.object(supervisor.proxy_pool, "pick_for_run",
                           side_effect=proxies) as pick, \
         mock.patch.object(supervisor.proxy_pool, "label", side_effect=lambda p: p), \
         mock.patch.object(supervisor, "_alert_failure"), \
         mock.patch.object(supervisor, "time") as fake_time, \
         mock.patch.object(supervisor.account_health, "record_failure",
                           return_value=False) as fail, \
         mock.patch.object(supervisor.account_health, "record_success"):
        fake_time.sleep = lambda *_a, **_k: None
        outcome = supervisor.run("AE", "HUN", force_email="x@y.com",
                                 force_password="pw")
    return outcome, roc, pick, fail


class TestGeoBlockRotatesIp(unittest.TestCase):
    def test_geo_block_rotates_to_a_different_ip(self):
        outcome, roc, pick, _fail = _run_with(GeoBlockedError("Permission Issues (403)"))
        self.assertGreater(roc.call_count, 1,
                           "a geo-block must be retried on another IP, not given up on")
        used = [c.kwargs.get("proxy", c.args[4] if len(c.args) > 4 else None)
                for c in roc.call_args_list]
        self.assertEqual(len(set(used)), len(used),
                         f"each attempt must use a DIFFERENT IP, got {used}")

    def test_geo_block_excludes_already_tried_ips(self):
        _outcome, _roc, pick, _fail = _run_with(GeoBlockedError("Permission Issues"))
        rotations = [c for c in pick.call_args_list if "exclude" in c.kwargs]
        self.assertTrue(rotations, "rotation must ask the pool for an unused IP")
        self.assertTrue(all(c.kwargs["exclude"] for c in rotations))

    def test_geo_block_never_strikes_the_account(self):
        # It is the IP that VFS refused, not the user — a benched account here
        # would take a healthy login out of rotation for hours for nothing.
        outcome, _roc, _pick, fail = _run_with(GeoBlockedError("Permission Issues"))
        fail.assert_not_called()
        self.assertEqual(outcome["status"], "GEO")

    def test_rotation_recovers_the_route_when_the_next_ip_works(self):
        outcome, roc, _pick, _fail = _run_with(
            [GeoBlockedError("Permission Issues"), [("Dubai - Tourist", "05-09-2026")]])
        self.assertEqual(outcome["status"], "OK")
        self.assertEqual(roc.call_count, 2)


if __name__ == "__main__":
    unittest.main()
