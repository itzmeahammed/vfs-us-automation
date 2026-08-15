"""Unit tests for PII scrubbing.

Client files hold passport numbers and dates of birth. Those must never reach
app.log, the daily archive, the console, or — worst of all — a Telegram message,
which leaves the machine entirely.

Run: python -m unittest tests.test_waitlist_redaction
"""

import logging
import unittest

from src.waitlist import redaction
from src.waitlist.registrant import Registrant


def _person(**overrides):
    data = {
        "route": "AE-CHE", "combos": ["Dubai - SCHENGEN"],
        "first_name": "Ahmed", "last_name": "Khan",
        "passport_number": "A1234567",
        "date_of_birth": "1990-04-12",
        "phone_number": "501234567",
        "email": "ahmed@example.com",
        "address_line_1": "Flat 101, Al Barsha Tower",
    }
    data.update(overrides)
    return Registrant("ahmed", data)


class ScrubTests(unittest.TestCase):
    def setUp(self):
        redaction.clear()

    tearDown = setUp

    def test_registered_value_is_replaced(self):
        redaction.add_values(["A1234567"])
        self.assertEqual(redaction.scrub("passport A1234567 ok"),
                         "passport [redacted] ok")

    def test_scrub_is_case_insensitive(self):
        redaction.add_values(["A1234567"])
        self.assertNotIn("a1234567", redaction.scrub("value a1234567").lower())

    def test_unregistered_value_is_untouched(self):
        redaction.add_values(["A1234567"])
        self.assertEqual(redaction.scrub("passport B7654321"),
                         "passport B7654321")

    def test_short_values_are_not_registered(self):
        # "50" or "AE" would match half the log and make it unreadable.
        redaction.add_values(["50", "AE", "abc"])
        self.assertEqual(redaction.count(), 0)

    def test_longest_value_wins(self):
        # A shorter secret contained in a longer one must not partially replace
        # it and leave a fragment of the longer one exposed.
        redaction.add_values(["12345", "1234567890"])
        self.assertEqual(redaction.scrub("id 1234567890"), "id [redacted]")

    def test_scrub_handles_empty_and_none(self):
        redaction.add_values(["A1234567"])
        self.assertEqual(redaction.scrub(""), "")
        self.assertIsNone(redaction.scrub(None))

    def test_no_registered_values_is_a_passthrough(self):
        self.assertEqual(redaction.scrub("anything"), "anything")

    def test_regex_metacharacters_are_escaped(self):
        # An address like "Flat 101 (Tower B)" must not break the pattern.
        redaction.add_values(["Flat 101 (Tower B)"])
        self.assertEqual(redaction.scrub("at Flat 101 (Tower B) now"),
                         "at [redacted] now")


class RegisterTests(unittest.TestCase):
    def setUp(self):
        redaction.clear()

    tearDown = setUp

    def test_register_covers_sensitive_fields(self):
        redaction.register(_person())
        text = redaction.scrub(
            "A1234567 1990-04-12 501234567 ahmed@example.com")
        for secret in ("A1234567", "1990-04-12", "501234567",
                       "ahmed@example.com"):
            self.assertNotIn(secret, text)

    def test_name_is_not_redacted(self):
        # Names are needed to identify a client in the log; the identifying
        # documents are what must not leak.
        redaction.register(_person())
        self.assertIn("Ahmed", redaction.scrub("client Ahmed Khan"))

    def test_loading_a_client_registers_automatically(self):
        # No loader can forget: registration happens inside registrant.load().
        import json
        import os
        import shutil
        import tempfile

        from src.waitlist import registrant as mod

        tmp = tempfile.mkdtemp()
        original = mod.REGISTRANT_DIR
        mod.REGISTRANT_DIR = tmp
        try:
            with open(os.path.join(tmp, "x.json"), "w", encoding="utf-8") as f:
                json.dump({"route": "AE-CHE", "combos": ["c"],
                           "first_name": "A", "passport_number": "Z9876543"}, f)
            mod.load("x")
            self.assertEqual(redaction.scrub("Z9876543"), "[redacted]")
        finally:
            mod.REGISTRANT_DIR = original
            shutil.rmtree(tmp, ignore_errors=True)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class FilterTests(unittest.TestCase):
    """The filter must catch PII regardless of which module logged it, and
    regardless of whether the value is in the template or the args."""

    def setUp(self):
        redaction.clear()
        self.handler = _Capture()
        self.handler.addFilter(redaction.RedactionFilter())
        self.logger = logging.getLogger("test.redaction")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        redaction.clear()
        self.logger.handlers = []

    def test_message_is_scrubbed(self):
        redaction.add_values(["A1234567"])
        self.logger.info("filling passport A1234567")
        self.assertNotIn("A1234567", self.handler.lines[0])

    def test_percent_args_are_scrubbed(self):
        # "%s" formatting puts the value in args, not the template — a filter
        # that only rewrote msg would leak here.
        redaction.add_values(["A1234567"])
        self.logger.info("passport %s", "A1234567")
        self.assertNotIn("A1234567", self.handler.lines[0])

    def test_dict_args_are_scrubbed(self):
        redaction.add_values(["A1234567"])
        self.logger.info("passport %(p)s", {"p": "A1234567"})
        self.assertNotIn("A1234567", self.handler.lines[0])

    def test_non_string_args_survive(self):
        redaction.add_values(["A1234567"])
        self.logger.info("count %d", 42)
        self.assertIn("42", self.handler.lines[0])

    def test_nothing_registered_is_a_passthrough(self):
        self.logger.info("plain message")
        self.assertEqual(self.handler.lines[0], "plain message")


class InstallTests(unittest.TestCase):
    def tearDown(self):
        redaction.clear()
        root = logging.getLogger()
        for handler in root.handlers:
            handler.filters = [f for f in handler.filters
                               if not isinstance(f, redaction.RedactionFilter)]

    def test_install_attaches_to_handlers(self):
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        redaction.install()
        self.assertTrue(any(
            any(isinstance(f, redaction.RedactionFilter) for f in h.filters)
            for h in root.handlers))

    def test_install_is_idempotent(self):
        root = logging.getLogger()
        handler = logging.NullHandler()
        root.addHandler(handler)
        redaction.install()
        redaction.install()
        count = sum(1 for f in handler.filters
                    if isinstance(f, redaction.RedactionFilter))
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
