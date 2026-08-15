"""Tests for client identity-document handling.

A passport bio page is a materially bigger liability than the passport NUMBER
already held in a client file — it carries the photo, the MRZ and the signature.
Two properties therefore matter more than anything else here and are what these
tests pin down:

  * documents live OUTSIDE the git repo, and
  * they are DELETED once a registration is confirmed, with a sweep as backstop.

Run: python -m unittest tests.test_waitlist_documents
"""

import os
import shutil
import tempfile
import time
import unittest

from src.waitlist import documents
from src.waitlist.errors import WaitlistConfigError, WaitlistStepError

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPG = b"\xff\xd8\xff" + b"\x00" * 64
_PDF = b"%PDF-1.4" + b"\x00" * 64


class _StoreTestCase(unittest.TestCase):
    """Points the store at a temp directory so tests never touch a real one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "documents")
        self._root = documents.root
        documents.root = lambda: self.store

    def tearDown(self):
        documents.root = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, content=_PNG, suffix=".png", name=None):
        path = os.path.join(self.tmp, name or f"src{suffix}")
        with open(path, "wb") as f:
            f.write(content)
        return path


class RootLocationTests(unittest.TestCase):
    """THE property that matters most: documents must not be inside the repo."""

    def test_default_root_is_outside_the_project(self):
        project = os.path.abspath(os.getcwd())
        self.assertFalse(
            os.path.abspath(documents.root()).startswith(project),
            "Documents must live outside the git working tree — .gitignore in "
            "config/registrants/ covers *.json only, so a scan there would be "
            "committed, and a passport in git history cannot be recalled.")

    def test_default_root_is_not_under_config(self):
        self.assertNotIn(os.path.join("config", "registrants"),
                         documents.root())


class PathTests(_StoreTestCase):
    def test_one_directory_per_client(self):
        # Makes erasing a client a single rmtree — a GDPR Art.17 request should
        # be a one-liner, not a search.
        self.assertTrue(documents.dir_for("ahmed").endswith("ahmed"))

    def test_client_id_is_normalised(self):
        self.assertEqual(documents.dir_for("Ahmed"), documents.dir_for("ahmed"))

    def test_a_traversing_id_is_rejected(self):
        # The client id is the only thing interpolated into the path, so
        # constraining it constrains the path.
        for bad in ("../../etc", "a/b", "..", "", "ahmed;rm"):
            with self.assertRaises(WaitlistConfigError):
                documents.dir_for(bad)

    def test_path_for_returns_none_when_absent(self):
        self.assertIsNone(documents.path_for("ahmed"))

    def test_stored_filename_does_not_contain_the_clients_name(self):
        # A real name in a path would surface in logs, stack traces and Telegram.
        path = documents.store("ahmed", self._file())
        self.assertEqual(os.path.basename(path), "passport_bio.png")


class ValidationTests(_StoreTestCase):
    """Validation runs BEFORE a browser is launched: failing here costs a
    second, failing at the portal costs a run and an opaque rejection."""

    def test_accepts_png_jpg_pdf(self):
        for content, suffix in ((_PNG, ".png"), (_JPG, ".jpg"), (_PDF, ".pdf")):
            path = self._file(content, suffix, name=f"ok{suffix}")
            self.assertTrue(documents.validate(path))

    def test_missing_file_is_named_clearly(self):
        with self.assertRaises(WaitlistStepError) as cm:
            documents.validate("/no/such/file.png")
        self.assertIn("no file at", str(cm.exception))

    def test_wrong_extension_is_rejected(self):
        path = self._file(_PNG, ".gif", name="x.gif")
        with self.assertRaises(WaitlistStepError):
            documents.validate(path)

    def test_empty_file_is_rejected(self):
        path = self._file(b"", ".png", name="empty.png")
        with self.assertRaises(WaitlistStepError):
            documents.validate(path)

    def test_oversized_file_is_rejected_before_the_portal_sees_it(self):
        path = self._file(_PNG + b"\x00" * documents.MAX_BYTES, ".png",
                          name="big.png")
        with self.assertRaises(WaitlistStepError) as cm:
            documents.validate(path)
        self.assertIn("MB", str(cm.exception))

    def test_extension_lying_about_contents_is_rejected(self):
        # An extension is a claim, not a check.
        path = self._file(_PDF, ".png", name="liar.png")
        with self.assertRaises(WaitlistStepError) as cm:
            documents.validate(path)
        self.assertIn("contents", str(cm.exception))

    def test_jpeg_and_jpg_are_the_same_thing(self):
        path = self._file(_JPG, ".jpeg", name="x.jpeg")
        self.assertTrue(documents.validate(path))


class StoreTests(_StoreTestCase):
    def test_store_copies_into_the_managed_location(self):
        source = self._file()
        stored = documents.store("ahmed", source)
        self.assertTrue(os.path.isfile(stored))
        self.assertTrue(os.path.isfile(source), "the original is not moved")
        self.assertEqual(documents.path_for("ahmed"), stored)

    def test_storing_again_replaces_a_different_format(self):
        # A client must never end up with two passports and an ambiguous
        # "which one is current".
        documents.store("ahmed", self._file(_PNG, ".png", name="a.png"))
        documents.store("ahmed", self._file(_PDF, ".pdf", name="a.pdf"))
        held = [i["path"] for i in documents.inventory()]
        self.assertEqual(len(held), 1)
        self.assertTrue(held[0].endswith(".pdf"))

    def test_jpeg_is_normalised_to_jpg(self):
        stored = documents.store("ahmed", self._file(_JPG, ".jpeg",
                                                     name="a.jpeg"))
        self.assertTrue(stored.endswith(".jpg"))

    def test_storing_an_invalid_file_raises(self):
        with self.assertRaises(WaitlistStepError):
            documents.store("ahmed", self._file(b"", ".png", name="e.png"))


class ResolveTests(_StoreTestCase):
    """A client file may name a document by explicit path or by the sentinel
    "managed"; either way it is validated before a browser sees it."""

    def test_explicit_path_is_used_as_given(self):
        path = self._file()
        self.assertEqual(documents.resolve("ahmed", path), path)

    def test_managed_sentinel_reads_the_store(self):
        stored = documents.store("ahmed", self._file())
        self.assertEqual(documents.resolve("ahmed", "managed"), stored)

    def test_managed_with_nothing_stored_says_how_to_fix_it(self):
        with self.assertRaises(WaitlistStepError) as cm:
            documents.resolve("ahmed", "managed")
        self.assertIn("documents add", str(cm.exception))

    def test_explicit_path_is_still_validated(self):
        bad = self._file(b"", ".png", name="e.png")
        with self.assertRaises(WaitlistStepError):
            documents.resolve("ahmed", bad)


class DeletionTests(_StoreTestCase):
    """Deletion is the control that actually reduces exposure — more than
    encryption would, for an unattended bot whose key must live on the same
    machine as the file."""

    def test_delete_removes_the_clients_documents(self):
        documents.store("ahmed", self._file())
        self.assertEqual(documents.delete_for("ahmed"), 1)
        self.assertIsNone(documents.path_for("ahmed"))

    def test_delete_removes_the_directory_too(self):
        documents.store("ahmed", self._file())
        documents.delete_for("ahmed")
        self.assertFalse(os.path.isdir(documents.dir_for("ahmed")))

    def test_deleting_nothing_is_not_an_error(self):
        self.assertEqual(documents.delete_for("nobody"), 0)

    def test_delete_does_not_touch_other_clients(self):
        documents.store("ahmed", self._file(name="a.png"))
        documents.store("fatima", self._file(name="b.png"))
        documents.delete_for("ahmed")
        self.assertIsNotNone(documents.path_for("fatima"))


class RetentionSweepTests(_StoreTestCase):
    """The backstop that makes "we delete after use" true rather than
    aspirational: a crashed run must not leave a passport scan on disk."""

    def _age(self, path, days):
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_old_documents_are_removed(self):
        path = documents.store("ahmed", self._file())
        self._age(path, 40)
        removed = documents.purge_older_than(30)
        self.assertEqual(removed, [path])
        self.assertIsNone(documents.path_for("ahmed"))

    def test_recent_documents_are_kept(self):
        documents.store("ahmed", self._file())
        self.assertEqual(documents.purge_older_than(30), [])
        self.assertIsNotNone(documents.path_for("ahmed"))

    def test_dry_run_reports_without_deleting(self):
        path = documents.store("ahmed", self._file())
        self._age(path, 40)
        self.assertEqual(documents.purge_older_than(30, dry_run=True), [path])
        self.assertTrue(os.path.isfile(path))

    def test_sweep_on_an_empty_store_is_safe(self):
        self.assertEqual(documents.purge_older_than(30), [])


class InventoryTests(_StoreTestCase):
    def test_reports_what_is_held_and_how_old(self):
        documents.store("ahmed", self._file())
        items = documents.inventory()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["registrant_id"], "ahmed")
        self.assertEqual(items[0]["kind"], "passport_bio")
        self.assertLess(items[0]["age_days"], 1)

    def test_empty_store_reports_nothing(self):
        self.assertEqual(documents.inventory(), [])


class ScreenshotSuppressionTests(unittest.TestCase):
    """On a page that RENDERS the uploaded passport, a debug screenshot would
    write the photo, MRZ and signature to disk as pixels — which the logging
    redaction filter cannot reach."""

    def test_upload_step_suppresses_screenshots(self):
        from src.waitlist import config as wcfg
        from src.waitlist.register import _screenshot
        from src.waitlist.result import Status, WaitlistResult

        step = next(s for s in wcfg.get("AE-ITA")["steps"]
                    if s["name"] == "your_details")
        self.assertTrue(step.get("no_screenshots"),
                        "the page showing the passport must not be photographed")

        result = WaitlistResult("AE-ITA", "c", "p", Status.PENDING)
        # A page object that would explode if touched proves nothing was.
        _screenshot(object(), result, "test", step)
        self.assertEqual(result.screenshots, [])

    def test_other_steps_still_screenshot(self):
        from src.waitlist import config as wcfg

        step = next(s for s in wcfg.get("AE-ITA")["steps"]
                    if s["name"] == "review_pay")
        self.assertFalse(step.get("no_screenshots"),
                         "the committing step keeps its evidence")


class JournalIntegrationTests(_StoreTestCase):
    """The journal is the single point that knows a document has served its
    only purpose, so the retention rule lives there."""

    def setUp(self):
        super().setUp()
        from src.waitlist import journal

        self.journal = journal
        self._dir, self._file_path = journal.JOURNAL_DIR, journal.JOURNAL_FILE
        journal.JOURNAL_DIR = self.tmp
        journal.JOURNAL_FILE = os.path.join(self.tmp, "journal.jsonl")

    def tearDown(self):
        self.journal.JOURNAL_DIR = self._dir
        self.journal.JOURNAL_FILE = self._file_path
        super().tearDown()

    def _result(self, status):
        from src.waitlist.result import WaitlistResult

        return WaitlistResult(route="AE-ITA", combo="Dubai",
                              registrant_id="ahmed", status=status).finish(status)

    def test_success_deletes_the_document(self):
        from src.waitlist.result import Status

        documents.store("ahmed", self._file())
        self.journal.update_status(self._result(Status.SUCCESS))
        self.assertIsNone(documents.path_for("ahmed"))

    def test_pending_keeps_the_document(self):
        # An in-flight submit may need a retry.
        from src.waitlist.result import Status

        documents.store("ahmed", self._file())
        self.journal.update_status(self._result(Status.PENDING))
        self.assertIsNotNone(documents.path_for("ahmed"))

    def test_unknown_keeps_the_document(self):
        # A human still has to reconcile it against the portal.
        from src.waitlist.result import Status

        documents.store("ahmed", self._file())
        self.journal.update_status(self._result(Status.UNKNOWN))
        self.assertIsNotNone(documents.path_for("ahmed"))

    def test_failure_keeps_the_document(self):
        from src.waitlist.result import Status

        documents.store("ahmed", self._file())
        self.journal.update_status(self._result(Status.FAILED))
        self.assertIsNotNone(documents.path_for("ahmed"))

    def test_cleanup_failure_never_fails_the_registration(self):
        from unittest import mock

        from src.waitlist.result import Status

        documents.store("ahmed", self._file())
        with mock.patch.object(documents, "delete_for",
                               side_effect=OSError("disk gone")):
            self.journal.update_status(self._result(Status.SUCCESS))
        # The row was still written — a cleanup problem must not turn a
        # successful registration into a failed one.
        self.assertEqual(self.journal.entries()[-1]["status"], Status.SUCCESS)


if __name__ == "__main__":
    unittest.main()
