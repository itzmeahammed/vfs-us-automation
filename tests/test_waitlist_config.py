"""Unit tests for waitlist route-config loading, inheritance and validation,
plus the journal's dedup / dangling-entry behaviour.

The validation tests matter most: they are what stop a half-understood page
description from ever reaching a live registration.

Run: python -m unittest tests.test_waitlist_config
"""

import json
import os
import shutil
import tempfile
import unittest

from src.waitlist import config as wcfg
from src.waitlist import journal
from src.waitlist.errors import WaitlistConfigError
from src.waitlist.result import Status, WaitlistResult


class _TempConfigDir(unittest.TestCase):
    """Points the loader at a temp directory so tests never read real config."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._original = wcfg.WAITLIST_DIR
        wcfg.WAITLIST_DIR = self.tmp
        wcfg.clear_cache()

    def tearDown(self):
        wcfg.WAITLIST_DIR = self._original
        wcfg.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, key, data):
        with open(os.path.join(self.tmp, f"{key}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)


_MINIMAL = {
    "steps": [
        {"name": "details", "fields": []},
        {"name": "confirm", "commits": True,
         "submit": {"role": "button", "name": "Confirm"}},
    ]
}


class ValidationTests(_TempConfigDir):
    def test_valid_config_loads(self):
        self.write("AE-XX", _MINIMAL)
        cfg = wcfg.get("AE-XX")
        self.assertEqual([s["name"] for s in cfg["steps"]], ["details", "confirm"])

    def test_commit_step_is_identified(self):
        self.write("AE-XX", _MINIMAL)
        self.assertEqual(wcfg.commit_step_name("AE-XX"), "confirm")

    def test_missing_config_raises(self):
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-NOPE")

    def test_no_steps_raises(self):
        self.write("AE-XX", {"steps": []})
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-XX")

    def test_no_commit_step_raises(self):
        # The whole safety design rests on knowing where the point of no return
        # is. A config that never says must be rejected, not guessed at.
        self.write("AE-XX", {"steps": [{"name": "details"}]})
        with self.assertRaises(WaitlistConfigError) as cm:
            wcfg.get("AE-XX")
        self.assertIn("commits", str(cm.exception))

    def test_duplicate_step_names_raise(self):
        self.write("AE-XX", {"steps": [
            {"name": "a"}, {"name": "a", "commits": True}]})
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-XX")

    def test_step_without_name_raises(self):
        self.write("AE-XX", {"steps": [{"commits": True}]})
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-XX")

    def test_malformed_json_raises_rather_than_silently_skipping(self):
        path = os.path.join(self.tmp, "AE-XX.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-XX")


class InheritanceTests(_TempConfigDir):
    def test_extends_merges_parent_steps(self):
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "extra"}]})
        cfg = wcfg.get("AE-XX")
        self.assertEqual([s["name"] for s in cfg["steps"]],
                         ["details", "confirm", "extra"])

    def test_child_overrides_one_key_without_restating_the_step(self):
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "details", "dwell_seconds": 18}]})
        cfg = wcfg.get("AE-XX")
        details = next(s for s in cfg["steps"] if s["name"] == "details")
        self.assertEqual(details["dwell_seconds"], 18)
        self.assertIn("fields", details)   # parent's key survived the merge

    def test_new_step_is_appended_by_default(self):
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "extra"}]})
        self.assertEqual([s["name"] for s in wcfg.get("AE-XX")["steps"]][-1],
                         "extra")

    def test_after_inserts_a_step_mid_flow(self):
        # Italy's OTP page sits between the summary and review-pay. Appended it
        # would land AFTER the committing step and never be reached.
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "otp", "after": "details"}]})
        self.assertEqual([s["name"] for s in wcfg.get("AE-XX")["steps"]],
                         ["details", "otp", "confirm"])

    def test_before_inserts_a_step_mid_flow(self):
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "otp", "before": "confirm"}]})
        self.assertEqual([s["name"] for s in wcfg.get("AE-XX")["steps"]],
                         ["details", "otp", "confirm"])

    def test_positioning_against_an_unknown_step_raises(self):
        # A typo here would silently append and put the step past the commit.
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "otp", "after": "typo"}]})
        with self.assertRaises(WaitlistConfigError) as cm:
            wcfg.get("AE-XX")
        self.assertIn("typo", str(cm.exception))

    def test_remove_drops_an_inherited_step(self):
        self.write("_default", _MINIMAL)
        self.write("AE-XX", {"extends": "_default",
                             "steps": [{"name": "details", "remove": True}]})
        self.assertEqual([s["name"] for s in wcfg.get("AE-XX")["steps"]], ["confirm"])

    def test_missing_parent_raises(self):
        self.write("AE-XX", {"extends": "_nope", "steps": _MINIMAL["steps"]})
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("AE-XX")

    def test_cyclic_extends_raises(self):
        self.write("A", {"extends": "B", "steps": _MINIMAL["steps"]})
        self.write("B", {"extends": "A", "steps": _MINIMAL["steps"]})
        with self.assertRaises(WaitlistConfigError):
            wcfg.get("A")

    def test_is_enabled_respects_the_flag(self):
        self.write("AE-XX", dict(_MINIMAL, enabled=False))
        self.assertFalse(wcfg.is_enabled("AE-XX"))

    def test_is_enabled_false_for_missing_config(self):
        # Must not raise — callers ask this cheaply on every route.
        self.assertFalse(wcfg.is_enabled("AE-NOPE"))


class DwellOrderingTests(unittest.TestCase):
    """The two waits must fire in the right ORDER around the fill.

    settle_seconds BEFORE filling, dwell_seconds AFTER — getting this backwards
    would defeat the point: a portal gating on "how long was this page open
    before it was touched" is not satisfied by filling instantly then idling.
    """

    def setUp(self):
        from src.waitlist import register
        self.register = register
        self.events = []

        self._dwell = register._dwell
        self._fill = register.fields.fill_all
        self._await_page = register._await_page
        self._screenshot = register._screenshot
        self._click = register._click
        self._await_enabled = register._await_enabled

        def fake_dwell(page, step, key, why):
            if step.get(key):
                self.events.append(f"wait:{key}={step[key]}")

        def fake_fill(page, specs, context, where="", dry_run=False):
            self.events.append("fill")
            return len(specs or [])

        register._dwell = fake_dwell
        register.fields.fill_all = fake_fill
        register._await_page = lambda *a, **k: self.events.append("await_page")
        register._screenshot = lambda *a, **k: None
        register._click = lambda *a, **k: self.events.append("submit")
        register._await_enabled = lambda *a, **k: None

    def tearDown(self):
        self.register._dwell = self._dwell
        self.register.fields.fill_all = self._fill
        self.register._await_page = self._await_page
        self.register._screenshot = self._screenshot
        self.register._click = self._click
        self.register._await_enabled = self._await_enabled

    class _Page:
        def wait_for_timeout(self, ms):
            pass

    def _step(self, **extra):
        step = {"name": "your_details", "fields": [{"name": "f"}],
                "submit": {"role": "button", "name": "Save"}}
        step.update(extra)
        return step

    def test_settle_fires_before_fill_and_dwell_after(self):
        result = WaitlistResult("AE-CHE", "c", "p", Status.PENDING)
        self.register._run_step(
            self._Page(), self._step(settle_seconds=30, dwell_seconds=20),
            {}, result, dry_run=False)
        self.assertEqual(
            self.events,
            ["await_page", "wait:settle_seconds=30", "fill",
             "wait:dwell_seconds=20", "submit"])

    def test_settle_alone_is_honoured(self):
        result = WaitlistResult("AE-CHE", "c", "p", Status.PENDING)
        self.register._run_step(self._Page(), self._step(settle_seconds=30),
                                {}, result, dry_run=False)
        self.assertEqual(self.events.index("wait:settle_seconds=30"),
                         self.events.index("fill") - 1)

    def test_neither_wait_configured_still_fills_and_submits(self):
        result = WaitlistResult("AE-CHE", "c", "p", Status.PENDING)
        self.register._run_step(self._Page(), self._step(), {}, result,
                                dry_run=False)
        self.assertEqual(self.events, ["await_page", "fill", "submit"])

    def test_dry_run_waits_but_never_submits(self):
        result = WaitlistResult("AE-CHE", "c", "p", Status.PENDING)
        self.register._run_step(
            self._Page(), self._step(settle_seconds=30, dwell_seconds=20),
            {}, result, dry_run=True)
        self.assertIn("wait:settle_seconds=30", self.events)
        self.assertNotIn("submit", self.events)


class SharedDefaultTests(unittest.TestCase):
    """config/waitlist/_default.json — the shared base new routes extend.

    Extracted only AFTER Switzerland was proven end to end, so every line in it
    is something a working route actually needed.
    """

    def test_default_exists_and_defines_the_common_flow(self):
        default = wcfg._load_file("_default")
        self.assertIsNotNone(default)
        self.assertEqual([s["name"] for s in default["steps"]],
                         ["appointment_details", "your_details",
                          "details_summary", "review_pay"])

    def test_default_is_disabled_so_inheriting_cannot_arm_a_route(self):
        # Each route must set "enabled": true deliberately.
        self.assertFalse(wcfg._load_file("_default").get("enabled"))

    def test_default_declares_no_fields(self):
        # The applicant form and the consents are the biggest per-portal
        # difference; shipping a guess would be worse than shipping nothing.
        for step in wcfg._load_file("_default")["steps"]:
            self.assertEqual(step.get("fields", []), [])

    def test_default_marks_exactly_one_committing_step(self):
        steps = wcfg._load_file("_default")["steps"]
        self.assertEqual(sum(1 for s in steps if s.get("commits")), 1)

    def test_an_unconfigured_route_is_not_enabled_by_the_fallback(self):
        # THE SAFETY PROPERTY: get() falls back to _default for a route with no
        # file of its own. That must never make the route registerable.
        self.assertFalse(wcfg.is_enabled("AE-NOSUCHROUTE"))

    def test_ae_che_still_resolves_after_the_default_was_added(self):
        # AE-CHE does not extend _default (it predates it and is self-
        # contained). Adding the base must not have changed it.
        cfg = wcfg.get("AE-CHE")
        self.assertTrue(cfg.get("enabled"))
        self.assertEqual(len(cfg["steps"]), 4)


class ItalyConfigTests(unittest.TestCase):
    """AE-ITA — the second route, and structurally different from AE-CHE.

    It proves the config model handles real portal variation: a bound waitlist
    checkbox, an upload-driven applicant form instead of typed fields, and an
    extra page in the MIDDLE of the flow.
    """

    def _cfg(self):
        return wcfg.get("AE-ITA")

    def _step(self, name):
        return next(s for s in self._cfg()["steps"] if s["name"] == name)

    def test_route_is_disabled_until_otp_is_implemented(self):
        # The OTP step is unimplemented, so the route must not be runnable —
        # otherwise a run would sail past it and commit without verifying.
        self.assertFalse(wcfg.is_enabled("AE-ITA"))

    def test_inherits_the_shared_flow(self):
        self.assertEqual(self._cfg().get("extends"), None)  # resolved away
        names = [s["name"] for s in self._cfg()["steps"]]
        self.assertIn("appointment_details", names)   # never restated in the file

    def test_otp_sits_before_the_committing_step(self):
        names = [s["name"] for s in self._cfg()["steps"]]
        self.assertLess(names.index("otp"), names.index("review_pay"))

    def test_otp_step_is_disabled(self):
        self.assertTrue(self._step("otp").get("disabled"))

    def test_waitlist_checkbox_is_pinned_by_form_control(self):
        # Unlike Switzerland, this portal DOES bind the control — naming it
        # beats inferring it from position or wording.
        self.assertIn("agreeToWaitlist", wcfg.checkbox_selector("AE-ITA"))

    def test_your_details_is_an_upload_not_typed_fields(self):
        fields = self._step("your_details")["fields"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["widget"], "file")

    def test_upload_confirms_and_waits_for_extraction(self):
        # VFS OCRs the document server-side; Save would be rejected against an
        # empty form if the step moved on immediately.
        upload = self._step("your_details")["fields"][0]
        self.assertTrue(upload.get("after_upload"))
        self.assertGreater(upload.get("wait_after_ms", 0), 0)

    def test_waits_out_the_full_countdown(self):
        # The banner counts DOWN, so a mid-timer capture ("wait 8 seconds")
        # understates the real 30s total. Anything below that clicks a button
        # that looks live but does nothing.
        self.assertGreaterEqual(self._step("your_details")["dwell_seconds"], 30)

    def test_consents_match_switzerlands_shape(self):
        names = [f["name"] for f in self._step("review_pay")["fields"]]
        self.assertEqual(names, ["accept_terms", "agree_waitlist"])

    def test_waitlist_consent_needs_no_index(self):
        field = next(f for f in self._step("review_pay")["fields"]
                     if f["name"] == "agree_waitlist")
        self.assertIsNone(field.get("index"))


class ComboLabelTests(unittest.TestCase):
    """A client file names the combination it wants BY LABEL, so labels must be
    unique within a route. Two sharing one is a config bug with teeth: the
    lookup would silently take the first and register for a category the client
    never asked for."""

    def test_italy_labels_are_unique(self):
        from src.utils.route_schema import get_route_schema
        from src.vfs_bot.slot_check import combo_label

        # These were both "Italy Visa Application Center ,Dubai" — two genuinely
        # different categories (Schengen Visa vs Short Stay/Tourist) sharing one
        # label.
        labels = [combo_label(c).strip().lower() for c in
                  get_route_schema("AE", "ITA")
                  .get("slot_check", {}).get("combinations", [])]
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_configured_route_has_unique_labels(self):
        import glob
        import os

        from src.utils.route_schema import get_route_schema
        from src.vfs_bot.slot_check import combo_label

        for path in glob.glob(os.path.join("config", "routes", "*.json")):
            key = os.path.splitext(os.path.basename(path))[0]
            if key.startswith("_"):
                continue
            source, _, dest = key.partition("-")
            labels = [combo_label(c).strip().lower() for c in
                      get_route_schema(source, dest)
                      .get("slot_check", {}).get("combinations", [])]
            duplicates = {l for l in labels if labels.count(l) > 1}
            self.assertEqual(
                duplicates, set(),
                f"{key}.json has duplicate combination label(s): {duplicates}. "
                "Give each a distinct \"label\" so a client file can name "
                "exactly the combination it wants.")

    def test_a_duplicate_label_raises_rather_than_guessing(self):
        from unittest import mock

        from src.waitlist.runner import _combo_parts

        duped = {"slot_check": {"combinations": [
            {"label": "Same", "centre": "Dubai", "category": "A"},
            {"label": "Same", "centre": "Dubai", "category": "B"},
        ]}}
        with mock.patch("src.utils.route_schema.get_route_schema",
                        return_value=duped):
            with self.assertRaises(WaitlistConfigError) as cm:
                _combo_parts("Same", "AE-XX")
        message = str(cm.exception)
        self.assertIn("matches 2 combinations", message)
        # The error must name the alternatives, or it is not actionable.
        self.assertIn("category", message)


class SubmitEnableTests(unittest.TestCase):
    """The portal's countdown gates only the SUBMIT BUTTON, and its length
    varies (10s and 18s both observed). So the flow polls the button rather than
    sleeping a guessed duration."""

    class _Button:
        """Enables after `enable_after` polls; optionally keeps Material's
        disabled class for a poll longer than the disabled attribute."""

        def __init__(self, enable_after=0, stale_class_for=0):
            self.polls = 0
            self.enable_after = enable_after
            self.stale_class_for = stale_class_for

        @property
        def first(self):
            return self

        def is_enabled(self):
            self.polls += 1
            return self.polls > self.enable_after

        def get_attribute(self, name):
            if name == "disabled":
                return None if self.polls > self.enable_after else "true"
            if name == "class":
                stale = self.polls <= self.enable_after + self.stale_class_for
                return "mat-mdc-button-disabled" if stale else "btn"
            return None

    class _Page:
        def __init__(self, button):
            self.button = button
            self.waits = 0

        def get_by_role(self, role, name="", exact=False):
            return self.button

        def wait_for_timeout(self, ms):
            self.waits += 1

    def _await(self, button, timeout_ms=10000):
        from src.waitlist.register import _await_enabled
        page = self._Page(button)
        _await_enabled(page, {"role": "button", "name": "Save"}, timeout_ms)
        return page

    def test_returns_immediately_when_already_enabled(self):
        page = self._await(self._Button(enable_after=0))
        self.assertEqual(page.waits, 0)

    def test_waits_for_a_short_countdown(self):
        button = self._Button(enable_after=3)
        page = self._await(button)
        self.assertGreaterEqual(page.waits, 3)
        self.assertTrue(button.is_enabled())

    def test_waits_longer_for_a_longer_countdown(self):
        # Same code path handles 10s and 18s — nothing is hardcoded.
        short = self._await(self._Button(enable_after=4)).waits
        long = self._await(self._Button(enable_after=12)).waits
        self.assertGreater(long, short)

    def test_material_disabled_class_still_counts_as_disabled(self):
        # is_enabled() alone can report ready while Material still has the
        # button logically off — clicking then would silently do nothing.
        button = self._Button(enable_after=2, stale_class_for=3)
        self._await(button)
        self.assertNotIn("mat-mdc-button-disabled", button.get_attribute("class"))

    def test_gives_up_and_warns_rather_than_hanging(self):
        # A never-enabling button must not block the run forever.
        page = self._await(self._Button(enable_after=10**6), timeout_ms=1500)
        self.assertGreater(page.waits, 0)

    def test_no_submit_spec_is_a_no_op(self):
        from src.waitlist.register import _await_enabled
        _await_enabled(self._Page(self._Button()), None, 1000)


class RealConfigTests(unittest.TestCase):
    """Guards the shipped AE-CHE config against edits that would break it."""

    def _step(self, name):
        return next(s for s in wcfg.get("AE-CHE")["steps"] if s["name"] == name)

    def test_your_details_waits_out_the_full_countdown(self):
        # Save ENABLES before the 30s timer expires, but clicking early does
        # nothing. So the wait is mandatory — polling the button alone would
        # click a live-looking button and silently fail to advance.
        self.assertGreaterEqual(self._step("your_details").get("dwell_seconds", 0),
                                30)

    def test_your_details_allows_time_for_the_countdown(self):
        # The dwell + poll both run inside the step's timeout.
        self.assertGreaterEqual(self._step("your_details").get("timeout_ms", 0),
                                60000)

    def test_details_summary_is_distinguished_by_text_not_url(self):
        # After Save the URL STAYS on /your-details and only the content
        # changes, so a URL gate alone could not tell the two steps apart.
        step = self._step("details_summary")
        self.assertIn("your-details", step.get("url_contains", ""))
        self.assertTrue(step.get("wait_for_text"))

    def test_review_pay_ticks_terms_and_the_waitlist_agreement(self):
        names = [f["name"] for f in self._step("review_pay")["fields"]]
        self.assertEqual(names, ["accept_terms", "agree_waitlist"])

    def test_waitlist_agreement_is_matched_by_wording_not_position(self):
        # Its text is unique on the page, so no index is needed — which keeps it
        # working when VFS adds or reorders a consent box.
        field = next(f for f in self._step("review_pay")["fields"]
                     if f["name"] == "agree_waitlist")
        self.assertIn("waitlist", field["label"].lower())
        self.assertIsNone(field.get("index"))

    def test_both_consents_are_required(self):
        # Confirm stays disabled until both are ticked.
        self.assertTrue(all(f.get("required")
                            for f in self._step("review_pay")["fields"]))

    def test_review_pay_scrolls_before_clicking(self):
        # The consents and Confirm sit below the fold.
        self.assertTrue(self._step("review_pay").get("scroll_to_bottom"))

    def test_step_order_matches_the_real_flow(self):
        names = [s["name"] for s in wcfg.get("AE-CHE")["steps"]]
        self.assertEqual(names, ["appointment_details", "your_details",
                                 "details_summary", "review_pay"])

    def test_confirmation_is_specific_not_a_bare_waitlist_match(self):
        # The word "waitlist" also appears in the page's disclaimer and in
        # "notified once your waitlist status is updated" — matching those
        # would report success on a page that merely mentions waitlists.
        confirmation = wcfg.get("AE-CHE")["confirmation"]
        self.assertEqual(confirmation.get("url_contains"), "confirmation")
        self.assertIn("your appointment is waitlisted",
                      [t.lower() for t in confirmation["success_text"]])

    def test_reference_pattern_matches_a_real_booking_reference(self):
        import re
        pattern = wcfg.get("AE-CHE")["confirmation"]["reference_pattern"]
        match = re.search(pattern, "Ref SWDB79918334684 shown top-right")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "SWDB79918334684")

    def test_exactly_one_committing_step(self):
        steps = wcfg.get("AE-CHE")["steps"]
        self.assertEqual(sum(1 for s in steps if s.get("commits")), 1)

    def test_the_committing_step_is_the_last_one(self):
        steps = wcfg.get("AE-CHE")["steps"]
        self.assertTrue(steps[-1].get("commits"))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dir, self._file = journal.JOURNAL_DIR, journal.JOURNAL_FILE
        journal.JOURNAL_DIR = self.tmp
        journal.JOURNAL_FILE = os.path.join(self.tmp, "journal.jsonl")

    def tearDown(self):
        journal.JOURNAL_DIR, journal.JOURNAL_FILE = self._dir, self._file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _result(self, status, combo="Dubai - SCHENGEN", registrant="ahmed"):
        r = WaitlistResult(route="AE-CHE", combo=combo,
                           registrant_id=registrant, status=status)
        return r.finish(status)

    def test_empty_journal_blocks_nothing(self):
        self.assertIsNone(journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_success_blocks_a_repeat(self):
        journal.append(self._result(Status.SUCCESS))
        self.assertIsNotNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_pending_blocks_a_repeat(self):
        # An in-flight submit may well have landed — it must block.
        journal.append(self._result(Status.PENDING))
        self.assertIsNotNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_failed_does_not_block(self):
        # Nothing was submitted, so retrying is safe.
        journal.append(self._result(Status.FAILED))
        self.assertIsNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_dry_run_does_not_block(self):
        journal.append(self._result(Status.DRY_RUN))
        self.assertIsNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_a_different_combo_is_not_blocked(self):
        journal.append(self._result(Status.SUCCESS, combo="Dubai - SCHENGEN"))
        self.assertIsNone(
            journal.blocking_entry("AE-CHE", "Abu Dhabi - SCHENGEN", "ahmed"))

    def test_a_different_registrant_is_not_blocked(self):
        journal.append(self._result(Status.SUCCESS, registrant="ahmed"))
        self.assertIsNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "fatima"))

    def test_dedup_key_ignores_case_and_spacing(self):
        journal.append(self._result(Status.SUCCESS, combo="Dubai - SCHENGEN"))
        self.assertIsNotNone(
            journal.blocking_entry("ae-che", "dubai  -  schengen", "AHMED"))

    def test_latest_row_wins(self):
        # A 'pending' later resolved to 'failed' must stop blocking.
        journal.append(self._result(Status.PENDING))
        journal.append(self._result(Status.FAILED))
        self.assertIsNone(
            journal.blocking_entry("AE-CHE", "Dubai - SCHENGEN", "ahmed"))

    def test_dangling_lists_unresolved_entries(self):
        journal.append(self._result(Status.PENDING))
        self.assertEqual(len(journal.dangling()), 1)

    def test_dangling_ignores_resolved_entries(self):
        journal.append(self._result(Status.PENDING))
        journal.append(self._result(Status.SUCCESS))
        self.assertEqual(journal.dangling(), [])

    def test_resolve_records_the_human_verdict(self):
        journal.append(self._result(Status.UNKNOWN))
        self.assertEqual(len(journal.dangling()), 1)
        journal.resolve("AE-CHE", "Dubai - SCHENGEN", "ahmed",
                        status=Status.FAILED, reason="not on the portal")
        self.assertEqual(journal.dangling(), [])

    def test_resolve_rejects_a_nonsense_status(self):
        with self.assertRaises(ValueError):
            journal.resolve("AE-CHE", "c", "ahmed", status=Status.PENDING)

    def test_history_is_append_only(self):
        journal.append(self._result(Status.PENDING))
        journal.append(self._result(Status.SUCCESS))
        self.assertEqual(len(journal.entries()), 2)

    def test_truncated_final_line_is_survivable(self):
        journal.append(self._result(Status.SUCCESS))
        with open(journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write('{"route": "AE-CHE", "trunc')
        self.assertEqual(len(journal.entries()), 1)

    def test_count_since_excludes_dry_runs_and_skips(self):
        journal.append(self._result(Status.SUCCESS, combo="a"))
        journal.append(self._result(Status.DRY_RUN, combo="b"))
        journal.append(self._result(Status.SKIPPED, combo="c"))
        self.assertEqual(journal.count_since("1970-01-01"), 1)


class ResultTests(unittest.TestCase):
    def test_committed_states(self):
        for status in (Status.PENDING, Status.SUCCESS, Status.UNKNOWN):
            self.assertTrue(
                WaitlistResult("r", "c", "p", status).committed, status)
        for status in (Status.FAILED, Status.SKIPPED, Status.DRY_RUN):
            self.assertFalse(
                WaitlistResult("r", "c", "p", status).committed, status)

    def test_needs_attention(self):
        self.assertTrue(WaitlistResult("r", "c", "p", Status.UNKNOWN).needs_attention)
        self.assertFalse(WaitlistResult("r", "c", "p", Status.SUCCESS).needs_attention)

    def test_round_trips_through_a_dict(self):
        original = WaitlistResult("AE-CHE", "combo", "ahmed", Status.SUCCESS,
                                  vfs_reference="X-1")
        restored = WaitlistResult.from_dict(original.to_dict())
        self.assertEqual(restored.vfs_reference, "X-1")
        self.assertEqual(restored.status, Status.SUCCESS)


if __name__ == "__main__":
    unittest.main()
