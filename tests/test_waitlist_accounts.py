"""Unit tests for waitlist account resolution and capacity.

The property under test throughout: a waitlist entry belongs to the account that
created it, so the account must be a stable, deliberate choice — never the
hourly slot-check rotation.

Run: python -m unittest tests.test_waitlist_accounts
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from src.waitlist import accounts, journal
from src.waitlist.errors import WaitlistConfigError
from src.waitlist.registrant import Registrant
from src.waitlist.result import Status, WaitlistResult


def _person(**overrides):
    data = {"route": "AE-CHE", "combos": ["Dubai - SCHENGEN"],
            "first_name": "Ahmed"}
    data.update(overrides)
    return Registrant(overrides.pop("_id", "ahmed"), data)


def _config(**values):
    """Stubs get_config_value for the [waitlist] section."""
    def fake(section, key, default=""):
        if section == "waitlist":
            return values.get(key, default)
        return default
    return mock.patch.object(accounts, "get_config_value", side_effect=fake)


class ResolutionOrderTests(unittest.TestCase):
    def test_cli_email_wins(self):
        person = _person(account="pinned@x.com", account_password="pw")
        with _config():
            got = accounts.resolve(person, cli_email="cli@x.com",
                                   cli_password="clipw")
        self.assertEqual(got.email, "cli@x.com")
        self.assertEqual(got.password, "clipw")

    def test_client_pin_beats_the_shared_default(self):
        person = _person(account="pinned@x.com", account_password="pw")
        with _config(account="default@x.com", account_password="dpw"):
            got = accounts.resolve(person)
        self.assertEqual(got.email, "pinned@x.com")
        self.assertIn("client file", got.source)

    def test_shared_default_used_when_client_has_no_pin(self):
        with _config(account="default@x.com", account_password="dpw"):
            got = accounts.resolve(_person())
        self.assertEqual(got.email, "default@x.com")

    def test_nothing_is_read_from_credentials_local_ini(self):
        # Waitlist accounts are a SEPARATE POOL. Even when the slot-check
        # credentials hold this exact email, its password must NOT be borrowed.
        person = _person(account="pinned@x.com")
        with _config(), mock.patch(
                "src.utils.credentials.password_for",
                return_value="slot-check-password") as looked_up:
            with self.assertRaises(WaitlistConfigError):
                accounts.resolve(person)
        looked_up.assert_not_called()

    def test_pinned_account_without_a_password_raises(self):
        person = _person(account="pinned@x.com")
        with _config():
            with self.assertRaises(WaitlistConfigError) as cm:
                accounts.resolve(person)
        message = str(cm.exception)
        self.assertIn("account_password", message)
        self.assertIn("credentials.local.ini", message)

    def test_cli_email_without_password_raises(self):
        with _config():
            with self.assertRaises(WaitlistConfigError) as cm:
                accounts.resolve(_person(), cli_email="cli@x.com")
        self.assertIn("--password", str(cm.exception))

    def test_shared_default_without_password_raises(self):
        with _config(account="default@x.com"):
            with self.assertRaises(WaitlistConfigError) as cm:
                accounts.resolve(_person())
        self.assertIn("account_password", str(cm.exception))

    def test_no_account_anywhere_raises_rather_than_rotating(self):
        # THE key behaviour: no silent fallback to credentials.get_credential().
        with _config():
            with self.assertRaises(WaitlistConfigError) as cm:
                accounts.resolve(_person())
        message = str(cm.exception)
        self.assertIn("No VFS account is configured", message)
        self.assertIn("SEPARATE POOL", message)
        self.assertIn("rotation", message.lower())

    def test_account_is_never_form_data(self):
        # The login must not be reachable as a {{placeholder}}, or a password
        # could be typed into a page field.
        person = _person(account="a@x.com", account_password="secret")
        self.assertNotIn("account", person.keys())
        self.assertNotIn("account_password", person.keys())

    def test_repr_does_not_leak_the_password(self):
        account = accounts.Account("a@x.com", "supersecret", "test")
        self.assertNotIn("supersecret", repr(account))


class ProxyResolutionTests(unittest.TestCase):
    """Which exit IP a waitlist account uses.

    The pool is config/proxylist.txt (shared with the slot checker); what differs
    is the PINNING. proxy_pool indexes accounts by their position in
    credentials.local.ini, which waitlist accounts are not in — so waitlist needs
    its own stable mapping or the IP would shift as the pool changes.
    """

    def _settings(self, proxy_enabled=True):
        stub = mock.MagicMock()
        stub.proxy.enabled = proxy_enabled
        return mock.patch.object(accounts, "settings", return_value=stub)

    def _pool(self, entries):
        return mock.patch("src.utils.proxy_pool.pool", return_value=entries)

    def test_cli_forced_proxy_wins(self):
        with self._settings(), _config():
            url, how = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE",
                cli_proxy="http://u:p@9.9.9.9:8080")
        self.assertIn("9.9.9.9", url)
        self.assertIn("--proxy-url", how)

    def test_cli_empty_string_forces_local(self):
        with self._settings(), _config():
            url, how = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE", cli_proxy="")
        self.assertEqual(url, "")
        self.assertIn("local", how)

    def test_client_pin_beats_the_master_switch(self):
        # An explicit pin is a deliberate instruction; silently ignoring it
        # would send a registration out from an IP nobody chose.
        account = accounts.Account("a@x.com", "p", "t",
                                   proxy="http://u:p@5.5.5.5:9000")
        with self._settings(proxy_enabled=False), _config():
            url, how = accounts.resolve_proxy(account, "AE-CHE")
        self.assertIn("5.5.5.5", url)
        self.assertIn("client file", how)

    def test_waitlist_default_proxy_used_when_no_client_pin(self):
        with self._settings(), _config(proxy="http://u:p@7.7.7.7:1000"):
            url, how = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE")
        self.assertIn("7.7.7.7", url)
        self.assertIn("[waitlist] proxy", how)

    def test_falls_back_to_the_pool(self):
        with self._settings(), _config(), self._pool(["http://1.1.1.1:80"]):
            url, how = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE")
        self.assertIn("1.1.1.1", url)
        self.assertIn("pool entry", how)

    def test_master_switch_off_means_local_when_nothing_is_pinned(self):
        with self._settings(proxy_enabled=False), _config():
            url, how = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE")
        self.assertEqual(url, "")
        self.assertIn("enabled = false", how)

    def test_empty_pool_falls_back_to_local(self):
        with self._settings(), _config(), self._pool([]):
            url, _ = accounts.resolve_proxy(
                accounts.Account("a@x.com", "p", "t"), "AE-CHE")
        self.assertEqual(url, "")

    def test_pool_choice_is_STABLE_for_an_account(self):
        # The whole point: a waitlist entry belongs to the account that made it,
        # so the same account must keep the same exit IP run after run.
        entries = [f"http://{n}.{n}.{n}.{n}:80" for n in range(1, 6)]
        account = accounts.Account("a@x.com", "p", "t")
        with self._settings(), _config(), self._pool(entries):
            picks = {accounts.resolve_proxy(account, "AE-CHE")[0]
                     for _ in range(5)}
        self.assertEqual(len(picks), 1)

    def test_accounts_list_order_decides_the_pool_entry(self):
        entries = ["http://1.1.1.1:80", "http://2.2.2.2:80"]
        with self._settings(), self._pool(entries), \
                _config(accounts="first@x.com, second@x.com"):
            first, _ = accounts.resolve_proxy(
                accounts.Account("first@x.com", "p", "t"), "AE-CHE")
            second, _ = accounts.resolve_proxy(
                accounts.Account("second@x.com", "p", "t"), "AE-CHE")
        self.assertIn("1.1.1.1", first)
        self.assertIn("2.2.2.2", second)

    def test_unlisted_accounts_still_get_a_deterministic_entry(self):
        entries = ["http://1.1.1.1:80", "http://2.2.2.2:80"]
        account = accounts.Account("unlisted@x.com", "p", "t")
        with self._settings(), _config(), self._pool(entries):
            a = accounts.resolve_proxy(account, "AE-CHE")[0]
            b = accounts.resolve_proxy(account, "AE-CHE")[0]
        self.assertEqual(a, b)
        self.assertIn(a, entries)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dir, self._file = journal.JOURNAL_DIR, journal.JOURNAL_FILE
        journal.JOURNAL_DIR = self.tmp
        journal.JOURNAL_FILE = os.path.join(self.tmp, "journal.jsonl")
        self.account = accounts.Account("shared@x.com", "pw", "test")

    def tearDown(self):
        journal.JOURNAL_DIR, journal.JOURNAL_FILE = self._dir, self._file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, client, combo="Dubai - SCHENGEN",
                  account="shared@x.com", status=Status.SUCCESS):
        journal.append(WaitlistResult(
            route="AE-CHE", combo=combo, registrant_id=client,
            status=status, account=account).finish(status))

    def _settings(self, max_clients=0, one_per_combo=False):
        stub = mock.MagicMock()
        stub.waitlist.max_clients_per_account = max_clients
        stub.waitlist.one_client_per_account_combo = one_per_combo
        return mock.patch.object(accounts, "settings", return_value=stub)

    # -- who is on an account --------------------------------------------- #

    def test_clients_on_lists_distinct_clients(self):
        self._register("ahmed")
        self._register("fatima")
        self.assertEqual(sorted(accounts.clients_on("shared@x.com")),
                         ["ahmed", "fatima"])

    def test_clients_on_ignores_dry_runs_and_skips(self):
        # Neither ever reached VFS, so neither occupies a slot.
        self._register("ahmed", status=Status.DRY_RUN)
        self._register("fatima", status=Status.SKIPPED)
        self.assertEqual(accounts.clients_on("shared@x.com"), [])

    def test_clients_on_counts_unknown_as_occupying(self):
        # An ambiguous submit may well have landed — treat it as occupied.
        self._register("ahmed", status=Status.UNKNOWN)
        self.assertEqual(accounts.clients_on("shared@x.com"), ["ahmed"])

    def test_clients_on_is_per_account(self):
        self._register("ahmed", account="a@x.com")
        self._register("fatima", account="b@x.com")
        self.assertEqual(accounts.clients_on("a@x.com"), ["ahmed"])

    # -- sharing is allowed by default ------------------------------------ #

    def test_sharing_allowed_when_unlimited(self):
        self._register("ahmed", combo="Dubai - SCHENGEN")
        with self._settings(max_clients=0):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "AE-CHE", "Abu Dhabi - X")
        self.assertTrue(ok)

    def test_max_clients_per_account_blocks_a_new_client(self):
        self._register("ahmed")
        with self._settings(max_clients=1):
            ok, why = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "AE-CHE", "Abu Dhabi - X")
        self.assertFalse(ok)
        self.assertIn("limit is 1", why)

    def test_an_existing_client_is_not_blocked_by_the_cap(self):
        # Ahmed already counts toward the total; a second combo for HIM is fine.
        self._register("ahmed")
        with self._settings(max_clients=1):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="ahmed"), "AE-CHE", "Abu Dhabi - X")
        self.assertTrue(ok)

    # -- same account, same combination ----------------------------------- #

    def test_same_combo_warns_but_proceeds_by_default(self):
        # VFS's real behaviour is unknown, so blocking wrongly is worse.
        self._register("ahmed", combo="Dubai - SCHENGEN")
        with self._settings(one_per_combo=False):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "AE-CHE", "Dubai - SCHENGEN")
        self.assertTrue(ok)

    def test_same_combo_blocks_when_configured(self):
        self._register("ahmed", combo="Dubai - SCHENGEN")
        with self._settings(one_per_combo=True):
            ok, why = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "AE-CHE", "Dubai - SCHENGEN")
        self.assertFalse(ok)
        self.assertIn("one_client_per_account_combo", why)

    def test_same_client_same_combo_is_not_a_collision(self):
        self._register("ahmed", combo="Dubai - SCHENGEN")
        with self._settings(one_per_combo=True):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="ahmed"), "AE-CHE", "Dubai - SCHENGEN")
        self.assertTrue(ok)

    def test_combo_collision_matching_ignores_spacing(self):
        self._register("ahmed", combo="Dubai - SCHENGEN")
        with self._settings(one_per_combo=True):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "ae-che",
                "dubai  -  schengen")
        self.assertFalse(ok)

    def test_a_different_account_never_collides(self):
        self._register("ahmed", combo="Dubai - SCHENGEN", account="other@x.com")
        with self._settings(one_per_combo=True):
            ok, _ = accounts.capacity_verdict(
                self.account, _person(_id="fatima"), "AE-CHE", "Dubai - SCHENGEN")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
