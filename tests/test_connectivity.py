"""The offline gate: internet_available() must say False only when EVERY probe
host is unreachable, and require_internet_or_log() must log exactly once and
return False in that case. This is what lets the supervisor skip a whole run
cleanly when the machine is offline, instead of failing every route with
connection-refused (striking accounts, flooding the log)."""

import unittest
from unittest import mock

from src.utils import connectivity


class _FakeSock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestConnectivity(unittest.TestCase):
    def test_online_when_first_probe_connects(self):
        with mock.patch.object(connectivity.socket, "create_connection",
                               return_value=_FakeSock()) as cc:
            self.assertTrue(connectivity.internet_available())
        self.assertEqual(cc.call_count, 1)  # returns on the FIRST success

    def test_offline_only_when_all_probes_fail(self):
        with mock.patch.object(connectivity.socket, "create_connection",
                               side_effect=OSError("no route")) as cc:
            self.assertFalse(connectivity.internet_available())
        self.assertEqual(cc.call_count, len(connectivity._PROBES))  # tried them all

    def test_online_if_any_probe_connects(self):
        # First host down, second up -> still online.
        calls = [OSError("down"), _FakeSock()]
        with mock.patch.object(connectivity.socket, "create_connection",
                               side_effect=calls):
            self.assertTrue(connectivity.internet_available())

    def test_gate_logs_once_when_offline(self):
        with mock.patch.object(connectivity, "internet_available", return_value=False):
            with self.assertLogs(level="ERROR") as cm:
                self.assertFalse(connectivity.require_internet_or_log())
        self.assertEqual(len(cm.records), 1)  # exactly one line, no spam

    def test_gate_silent_when_online(self):
        with mock.patch.object(connectivity, "internet_available", return_value=True):
            self.assertTrue(connectivity.require_internet_or_log())


if __name__ == "__main__":
    unittest.main()
