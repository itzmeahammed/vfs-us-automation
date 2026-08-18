"""Unit tests for {{placeholder}} resolution — the join between the two config
folders (config/waitlist/<ROUTE>.json and config/registrants/<id>.json).

context.py is deliberately PURE (no page, no I/O), so every rule here is testable
without Playwright — the same discipline slot_check.cascade_steps() follows.

Run: python -m unittest tests.test_waitlist_context
"""

import unittest
from datetime import date

from src.waitlist import context as ctx
from src.waitlist.errors import WaitlistConfigError
from src.waitlist.registrant import Registrant


def _person(**overrides):
    data = {
        "route": "AE-CHE",
        "combos": ["Dubai - SCHENGEN"],
        "first_name": "Ahmed",
        "last_name": "Khan",
        "nationality": "India",
        "passport_number": "a1234567",
        "date_of_birth": "1990-04-12",
        "phone_country_code": "+971",
        "phone_number": "50 123 4567",
        "email": "ahmed@example.com",
    }
    data.update(overrides)
    return Registrant("ahmed", data)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx.build(_person(), route="AE-CHE", combo="Dubai - SCHENGEN")

    def test_plain_field(self):
        self.assertEqual(ctx.resolve("{{first_name}}", self.ctx), "Ahmed")

    def test_namespaced_field(self):
        self.assertEqual(ctx.resolve("{{registrant.first_name}}", self.ctx), "Ahmed")

    def test_route_and_combo(self):
        self.assertEqual(ctx.resolve("{{route}}", self.ctx), "AE-CHE")
        self.assertEqual(ctx.resolve("{{source}}", self.ctx), "AE")
        self.assertEqual(ctx.resolve("{{dest}}", self.ctx), "CHE")
        self.assertEqual(ctx.resolve("{{combo}}", self.ctx), "Dubai - SCHENGEN")

    def test_mixed_text_and_placeholders(self):
        self.assertEqual(
            ctx.resolve("{{first_name}} {{last_name}}", self.ctx), "Ahmed Khan")

    def test_non_string_passes_through(self):
        self.assertIs(ctx.resolve(True, self.ctx), True)
        self.assertEqual(ctx.resolve(42, self.ctx), 42)

    def test_lone_placeholder_keeps_type(self):
        # {{index1}} must stay an int, not become "1" — some widgets need the type.
        self.assertEqual(ctx.resolve("{{index1}}", self.ctx), 1)
        self.assertIsInstance(ctx.resolve("{{index1}}", self.ctx), int)


class ModifierTests(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx.build(_person(), route="AE-CHE")

    def test_case_modifiers(self):
        self.assertEqual(ctx.resolve("{{first_name|upper}}", self.ctx), "AHMED")
        self.assertEqual(ctx.resolve("{{first_name|lower}}", self.ctx), "ahmed")
        self.assertEqual(ctx.resolve("{{passport_number|upper}}", self.ctx), "A1234567")

    def test_digits_strips_plus_and_spaces(self):
        # The Switzerland country-code input is maxlength=3, so a stored '+971'
        # must arrive as '971' or the field silently truncates.
        self.assertEqual(ctx.resolve("{{phone_country_code|digits}}", self.ctx), "971")
        self.assertEqual(ctx.resolve("{{phone_number|digits}}", self.ctx), "501234567")

    def test_date_reformat(self):
        self.assertEqual(
            ctx.resolve("{{date_of_birth|date:%d/%m/%Y}}", self.ctx), "12/04/1990")

    def test_date_rejects_unparseable(self):
        bad = ctx.build(_person(date_of_birth="not-a-date"), route="AE-CHE")
        with self.assertRaises(WaitlistConfigError):
            ctx.resolve("{{date_of_birth|date:%d/%m/%Y}}", bad)

    def test_default_fills_missing(self):
        person = ctx.build(_person(middle_name=""), route="AE-CHE")
        self.assertEqual(ctx.resolve("{{middle_name|default:-}}", person), "-")

    def test_chained_modifiers(self):
        self.assertEqual(
            ctx.resolve("{{phone_number|digits|upper}}", self.ctx), "501234567")

    def test_unknown_modifier_is_an_error(self):
        with self.assertRaises(WaitlistConfigError):
            ctx.resolve("{{first_name|shout}}", self.ctx)

    def test_today(self):
        self.assertEqual(ctx.resolve("{{today}}", self.ctx), date.today().isoformat())


class MissingFieldTests(unittest.TestCase):
    """A blank passport field is far worse than a loud failure — a missing
    placeholder must always raise, never silently resolve to ""."""

    def setUp(self):
        self.ctx = ctx.build(_person(), route="AE-CHE")

    def test_missing_field_raises(self):
        with self.assertRaises(WaitlistConfigError):
            ctx.resolve("{{passport_expiry}}", self.ctx)

    def test_error_names_the_field_and_location(self):
        with self.assertRaises(WaitlistConfigError) as cm:
            ctx.resolve("{{passport_expiry}}", self.ctx, where="step 'your_details'")
        message = str(cm.exception)
        self.assertIn("passport_expiry", message)
        self.assertIn("your_details", message)

    def test_validate_collects_every_problem(self):
        problems = ctx.validate(
            [("f1", "{{nope}}"), ("f2", "{{first_name}}"), ("f3", "{{also_nope}}")],
            self.ctx,
        )
        self.assertEqual(len(problems), 2)

    def test_validate_passes_when_all_resolve(self):
        self.assertEqual(
            ctx.validate([("f", "{{first_name}} {{last_name}}")], self.ctx), [])


class PlaceholderIntrospectionTests(unittest.TestCase):
    def test_lists_names_without_modifiers(self):
        self.assertEqual(
            ctx.placeholders_in("{{a|upper}} and {{b|date:%Y}}"), ["a", "b"])

    def test_no_placeholders(self):
        self.assertEqual(ctx.placeholders_in("plain text"), [])


class RegistrantTests(unittest.TestCase):
    def test_nested_fields_flatten_to_dotted_keys(self):
        person = Registrant("x", {"phone": {"country_code": "971", "number": "50"}})
        self.assertEqual(person.get("phone.country_code"), "971")

    def test_dot_and_underscore_are_interchangeable(self):
        person = Registrant("x", {"phone": {"number": "50"}})
        self.assertEqual(person.get("phone_number"), "50")
        flat = Registrant("y", {"phone_number": "60"})
        self.assertEqual(flat.get("phone.number"), "60")

    def test_repr_does_not_leak_pii(self):
        text = repr(_person())
        self.assertNotIn("1234567", text)
        self.assertNotIn("1990-04-12", text)

    def test_label_is_safe_to_log(self):
        label = _person().label()
        self.assertIn("ahmed", label)
        self.assertNotIn("1234567", label)


class ClientTargetingTests(unittest.TestCase):
    """One file per client carries its own route + combos — there is no separate
    targets file, so these ARE the targeting rules."""

    def test_route_is_upper_cased(self):
        self.assertEqual(Registrant("x", {"route": "ae-che"}).route, "AE-CHE")

    def test_enabled_defaults_to_true(self):
        self.assertTrue(Registrant("x", {"route": "AE-CHE"}).enabled)

    def test_enabled_can_be_switched_off(self):
        self.assertFalse(
            Registrant("x", {"route": "AE-CHE", "enabled": False}).enabled)

    def test_wants_matches_a_listed_combo(self):
        self.assertTrue(_person().wants("Dubai - SCHENGEN"))

    def test_wants_ignores_case_and_extra_spacing(self):
        # A retyped label with a double space must still match, or the client
        # would be silently skipped.
        self.assertTrue(_person().wants("dubai  -  schengen"))

    def test_wants_rejects_an_unlisted_combo(self):
        self.assertFalse(_person().wants("Abu Dhabi - SCHENGEN"))

    def test_targeting_keys_are_not_form_data(self):
        # {{route}} must mean the run's route, never be shadowed by the client
        # file, and "combos" must never be typed into a field.
        person = _person()
        self.assertNotIn("route", person.keys())
        self.assertNotIn("combos", person.keys())
        self.assertNotIn("enabled", person.keys())

    def test_form_data_still_present(self):
        self.assertIn("first_name", _person().keys())


class ClientValidationTests(unittest.TestCase):
    """_validate runs at load time so a bad file costs a second, not a
    half-finished registration."""

    def setUp(self):
        import tempfile
        from src.waitlist import registrant as mod
        self.mod = mod
        self.tmp = tempfile.mkdtemp()
        self._original = mod.REGISTRANT_DIR
        mod.REGISTRANT_DIR = self.tmp

    def tearDown(self):
        import shutil
        self.mod.REGISTRANT_DIR = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        import json
        import os
        with open(os.path.join(self.tmp, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _valid(self, **overrides):
        data = {"route": "AE-CHE", "combos": ["Dubai - SCHENGEN"],
                "first_name": "Ahmed"}
        data.update(overrides)
        return data

    def test_valid_file_loads(self):
        self.write("ahmed", self._valid())
        self.assertEqual(self.mod.load("ahmed").route, "AE-CHE")

    def test_missing_route_raises(self):
        self.write("ahmed", {"combos": ["x"], "first_name": "A"})
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_malformed_route_raises(self):
        self.write("ahmed", self._valid(route="switzerland"))
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_route_as_a_list_is_rejected_with_guidance(self):
        # One file = one route. The error must say how to do two countries.
        self.write("ahmed", self._valid(route=["AE-CHE", "AE-ITA"]))
        with self.assertRaises(WaitlistConfigError) as cm:
            self.mod.load("ahmed")
        self.assertIn("one route", str(cm.exception).lower())

    def test_missing_combos_raises(self):
        self.write("ahmed", {"route": "AE-CHE", "first_name": "A"})
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_duplicate_combos_raise(self):
        self.write("ahmed", self._valid(combos=["Dubai - X", "dubai  -  x"]))
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_no_form_data_raises(self):
        self.write("ahmed", {"route": "AE-CHE", "combos": ["x"]})
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_account_without_a_password_is_rejected(self):
        # Waitlist accounts are a separate pool, so there is nowhere else to
        # look the password up — pinning must be all-or-nothing.
        self.write("ahmed", self._valid(account="acc1@x.com"))
        with self.assertRaises(WaitlistConfigError) as cm:
            self.mod.load("ahmed")
        self.assertIn("account_password", str(cm.exception))

    def test_password_without_an_account_is_rejected(self):
        self.write("ahmed", self._valid(account_password="pw"))
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_account_and_password_together_load(self):
        self.write("ahmed", self._valid(account="acc1@x.com",
                                        account_password="pw"))
        person = self.mod.load("ahmed")
        self.assertEqual(person.account, "acc1@x.com")

    def test_neither_key_is_fine(self):
        # Falls back to the shared [waitlist] account.
        self.write("ahmed", self._valid())
        self.assertEqual(self.mod.load("ahmed").account, "")

    def test_malformed_account_email_is_rejected(self):
        self.write("ahmed", self._valid(account="not-an-email",
                                        account_password="pw"))
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_selector_in_a_client_file_is_rejected(self):
        # The data/structure split is the whole design — breaking it must be loud.
        self.write("ahmed", self._valid(
            first_name="input[formcontrolname='firstName']"))
        with self.assertRaises(WaitlistConfigError) as cm:
            self.mod.load("ahmed")
        self.assertIn("SELECTOR", str(cm.exception))

    def test_list_field_is_rejected(self):
        self.write("ahmed", self._valid(address=["line 1", "line 2"]))
        with self.assertRaises(WaitlistConfigError):
            self.mod.load("ahmed")

    def test_for_route_finds_matching_clients(self):
        self.write("ahmed", self._valid(route="AE-CHE"))
        self.write("fatima", self._valid(route="AE-ITA"))
        self.assertEqual([p.id for p in self.mod.for_route("AE-CHE")], ["ahmed"])

    def test_for_route_skips_disabled_clients_by_default(self):
        self.write("ahmed", self._valid(enabled=False))
        self.assertEqual(self.mod.for_route("AE-CHE"), [])
        self.assertEqual(
            [p.id for p in self.mod.for_route("AE-CHE", include_disabled=True)],
            ["ahmed"])

    def test_load_all_can_skip_invalid_files(self):
        # One broken file must not hide the whole roster in `status`.
        self.write("ahmed", self._valid())
        self.write("broken", {"nonsense": True})
        self.assertEqual([p.id for p in self.mod.load_all(skip_invalid=True)],
                         ["ahmed"])
        with self.assertRaises(WaitlistConfigError):
            self.mod.load_all()


if __name__ == "__main__":
    unittest.main()
