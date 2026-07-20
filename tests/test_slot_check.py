"""Unit tests for the pure cascade-selection logic in src/vfs_bot/slot_check.py.

The Appointment Details dropdowns cascade (centre -> category -> sub-category):
picking a parent resets its children in VFS's form. cascade_steps() decides,
for consecutive combinations, which dropdown levels actually need re-selecting
— this is the one piece of real decision-making in the slot-check flow, so
it's kept as a pure function (no `page`, no Playwright) precisely so it can be
tested here without a browser.

Run: python -m unittest tests.test_slot_check
"""

import unittest

from src.vfs_bot import slot_check


class TestCascadeSteps(unittest.TestCase):
    def test_first_combo_selects_every_level_present(self):
        combo = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        steps = slot_check.cascade_steps(combo, prev={})
        self.assertEqual(steps, [
            ("centerCode", "centre", "Dubai"),
            ("selectedSubvisaCategory", "category", "Tourism"),
            ("visaCategoryCode", "sub_category", "Single"),
        ])

    def test_missing_levels_are_skipped(self):
        combo = {"centre": "Dubai", "sub_category": "Single"}  # no category
        steps = slot_check.cascade_steps(combo, prev={})
        self.assertEqual(steps, [
            ("centerCode", "centre", "Dubai"),
            ("visaCategoryCode", "sub_category", "Single"),
        ])

    def test_unchanged_trailing_combo_needs_no_reselection(self):
        prev = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        combo = dict(prev)
        self.assertEqual(slot_check.cascade_steps(combo, prev), [])

    def test_only_the_changed_tail_is_reselected_when_parents_match(self):
        prev = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        combo = {"centre": "Dubai", "category": "Tourism", "sub_category": "Multiple"}
        steps = slot_check.cascade_steps(combo, prev)
        self.assertEqual(steps, [("visaCategoryCode", "sub_category", "Multiple")])

    def test_parent_change_forces_reselection_of_every_level_below(self):
        # sub_category value is IDENTICAL to prev, but centre changed, which
        # resets category + sub_category in VFS's form — both must be redone.
        prev = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        combo = {"centre": "Abu Dhabi", "category": "Tourism", "sub_category": "Single"}
        steps = slot_check.cascade_steps(combo, prev)
        self.assertEqual(steps, [
            ("centerCode", "centre", "Abu Dhabi"),
            ("selectedSubvisaCategory", "category", "Tourism"),
            ("visaCategoryCode", "sub_category", "Single"),
        ])

    def test_category_change_forces_reselection_of_sub_category_only(self):
        prev = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        combo = {"centre": "Dubai", "category": "Business", "sub_category": "Single"}
        steps = slot_check.cascade_steps(combo, prev)
        self.assertEqual(steps, [
            ("selectedSubvisaCategory", "category", "Business"),
            ("visaCategoryCode", "sub_category", "Single"),
        ])


class TestComboLabel(unittest.TestCase):
    def test_explicit_label_wins(self):
        combo = {"label": "Custom Label", "centre": "Dubai", "category": "Tourism"}
        self.assertEqual(slot_check.combo_label(combo), "Custom Label")

    def test_falls_back_to_joined_fields(self):
        combo = {"centre": "Dubai", "category": "Tourism", "sub_category": "Single"}
        self.assertEqual(slot_check.combo_label(combo), "Dubai / Tourism / Single")

    def test_falls_back_skips_missing_fields(self):
        combo = {"centre": "Dubai", "sub_category": "Single"}
        self.assertEqual(slot_check.combo_label(combo), "Dubai / Single")


if __name__ == "__main__":
    unittest.main()
