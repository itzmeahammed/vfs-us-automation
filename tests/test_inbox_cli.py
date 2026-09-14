"""The commands you actually type.

Exit codes matter more than they look: `check` and `test` are the offline
preflight, and if they exit 0 on a broken config then a scheduled run or a CI
step will happily proceed with matchers that classify nothing.

`reconcile` gets the closest attention because it WRITES — a wrong reconciliation
marks a client registered when they are not, and they are then never retried. It
must be dry by default and must not touch the journal without --apply.
"""

import pytest

from src.inbox.__main__ import main


@pytest.fixture
def no_network(monkeypatch):
    """Stops any command from opening a real mailbox."""
    import src.inbox.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "mailbox_accounts", lambda: [])
    return watcher_mod


# --------------------------------------------------------------------------- #
# Dispatch                                                                     #
# --------------------------------------------------------------------------- #

def test_no_command_prints_help_and_fails():
    """Exit 1, so a mistyped invocation in a script is not mistaken for success."""
    assert main([]) == 1


def test_an_unknown_command_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        main(["nonsense"])


# --------------------------------------------------------------------------- #
# check — the offline preflight                                                #
# --------------------------------------------------------------------------- #

def test_check_passes_on_the_real_shipped_configs(capsys):
    assert main(["check"]) == 0
    assert "all valid" in capsys.readouterr().out


def test_check_fails_when_a_config_is_broken(tmp_path, monkeypatch, capsys):
    from src.inbox import config as inbox_config

    directory = tmp_path / "inbox"
    directory.mkdir()
    (directory / "AE-BAD.json").write_text(
        '{"matchers": [{"name": "x", "classify": "bogus", "subject_contains": ["x"]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(inbox_config, "INBOX_DIR", str(directory))
    inbox_config.clear_cache()

    assert main(["check"]) == 1, "a broken config must not exit 0"
    assert "FAIL" in capsys.readouterr().out
    inbox_config.clear_cache()


def test_check_fails_when_there_are_no_configs_at_all(tmp_path, monkeypatch, capsys):
    from src.inbox import config as inbox_config

    monkeypatch.setattr(inbox_config, "INBOX_DIR", str(tmp_path / "empty"))
    inbox_config.clear_cache()
    assert main(["check"]) == 1
    assert "No inbox configs" in capsys.readouterr().out
    inbox_config.clear_cache()


# --------------------------------------------------------------------------- #
# test — matchers against fixtures                                             #
# --------------------------------------------------------------------------- #

def test_test_classifies_both_real_fixtures(capsys):
    """The end-to-end check that the shipped Italy config still reads the real
    emails correctly."""
    assert main(["test"]) == 0

    out = capsys.readouterr().out
    assert "INVITATION" in out
    assert "CONFIRMATION" in out
    assert "ITD125298020335" in out
    assert "0 unmatched" in out


def test_test_exits_nonzero_when_a_fixture_is_unmatched(tmp_path, monkeypatch, capsys):
    """An unmatched fixture is the signal that a matcher regressed."""
    import src.inbox.__main__ as cli

    directory = tmp_path / "emails"
    directory.mkdir()
    (directory / "unknown.eml").write_text(
        "Subject: Something Else\r\n\r\nbody\r\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "FIXTURE_DIR", str(directory))

    assert main(["test"]) == 1
    assert "UNMATCHED" in capsys.readouterr().out


def test_test_reports_when_there_are_no_fixtures(tmp_path, monkeypatch, capsys):
    import src.inbox.__main__ as cli

    monkeypatch.setattr(cli, "FIXTURE_DIR", str(tmp_path / "none"))
    assert main(["test"]) == 1
    assert "No .eml fixtures" in capsys.readouterr().out


def test_test_can_target_a_single_file_and_route(capsys):
    import os

    path = os.path.join("tests", "fixtures", "emails", "ae-ita-invitation.eml")
    assert main(["test", "--file", path, "--route", "AE-ITA"]) == 0
    assert "INVITATION" in capsys.readouterr().out


def test_test_reports_a_config_error_rather_than_crashing(monkeypatch, capsys):
    from src.inbox import config as inbox_config
    from src.inbox.matcher import MatcherConfigError

    def boom(route):
        raise MatcherConfigError("bad config")

    monkeypatch.setattr(inbox_config, "matchers_for", boom)
    assert main(["test", "--route", "AE-ITA"]) == 1
    assert "Config error" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #

def test_status_lists_configs_and_mailboxes(no_network, capsys):
    assert main(["status"]) == 0

    out = capsys.readouterr().out
    assert "AE-ITA" in out
    assert "invitation" in out
    assert "IMAP host" in out


def test_status_survives_an_unresolvable_mailbox_list(monkeypatch, capsys):
    """Diagnostics must still print the rest when one section cannot be read —
    that is precisely when someone is running it."""
    import src.inbox.watcher as watcher_mod

    def boom():
        raise RuntimeError("no registrants")

    monkeypatch.setattr(watcher_mod, "mailbox_accounts", boom)
    assert main(["status"]) == 0
    assert "Could not resolve mailboxes" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# reconcile — the one that writes                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture
def stub_pass(monkeypatch):
    """Replaces the mailbox sweep with a canned PassResult."""
    import src.inbox.__main__ as cli
    import src.inbox.watcher as watcher_mod

    def install(result):
        monkeypatch.setattr(watcher_mod, "run_pass", lambda *a, **k: result)
        monkeypatch.setattr(cli, "_install_redaction", lambda: None)
        return result

    return install


def test_reconcile_is_a_dry_run_by_default(stub_pass, monkeypatch, capsys):
    """It writes a 'registered' record. Applying that must be explicit."""
    from src.inbox.watcher import PassResult
    from src.waitlist import journal

    written = []
    monkeypatch.setattr(journal, "append", lambda r: written.append(r))
    stub_pass(PassResult(mailboxes_checked=1))

    assert main(["reconcile"]) == 0
    assert written == [], "a dry run must not touch the journal"


def test_reconcile_reports_when_there_is_nothing_to_do(stub_pass, capsys):
    from src.inbox.watcher import PassResult

    stub_pass(PassResult(mailboxes_checked=2))
    assert main(["reconcile"]) == 0
    assert "Nothing to reconcile" in capsys.readouterr().out


def test_reconcile_warns_about_mailboxes_it_could_not_read(stub_pass, capsys):
    """Rows those accounts could have settled stay dangling, and the operator
    has to know that before concluding 'nothing to reconcile'."""
    from src.inbox.watcher import PassResult

    stub_pass(PassResult(mailboxes_checked=1, mailboxes_failed=["ac***@example.com"]))
    main(["reconcile"])

    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "stay dangling" in out


def test_reconcile_applies_only_with_the_flag(stub_pass, monkeypatch, capsys):
    from src.inbox.matcher import CONFIRMATION, Email, Match
    from src.inbox.watcher import Observation, PassResult
    from src.waitlist import journal
    from src.waitlist.result import Status

    class FakeRegistrant:
        id = "irina"

        def get(self, key, default=None):
            return {"first_name": "IRINA", "last_name": "KONOVALOVA",
                    "route": "AE-ITA"}.get(key, default)

    row = {
        "route": "AE-ITA", "combo": "Dubai - Tourist", "registrant_id": "irina",
        "status": Status.UNKNOWN, "vfs_reference": None,
        "started_at": "2026-08-06T11:00:00",
    }
    written = []
    monkeypatch.setattr(journal, "entries", lambda: [row])
    monkeypatch.setattr(journal, "append", lambda r: written.append(r))

    from src.waitlist import registrant as registrant_mod
    monkeypatch.setattr(registrant_mod, "load_all",
                        lambda skip_invalid=True: [FakeRegistrant()])

    stub_pass(PassResult(observations=[Observation(
        email=Email(subject="Successfully Added to Waitlist", received_epoch=1.0),
        match=Match(classification=CONFIRMATION, route="AE-ITA",
                    fields={"applicant_name": "IRINA KONOVALOVA",
                            "reference": "ITD125298020335"}),
    )], mailboxes_checked=1))

    assert main(["reconcile", "--apply"]) == 0
    assert len(written) == 1
    assert written[0].status == Status.SUCCESS
    assert "Applied 1 change" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# watch                                                                        #
# --------------------------------------------------------------------------- #

def test_watch_once_runs_a_single_pass(monkeypatch):
    import src.inbox.__main__ as cli
    import src.inbox.report as report_mod
    import src.inbox.watcher as watcher_mod
    from src.inbox.watcher import PassResult

    calls = []
    monkeypatch.setattr(cli, "_install_redaction", lambda: None)
    monkeypatch.setattr(watcher_mod, "run_pass",
                        lambda *a, **k: calls.append(1) or PassResult(mailboxes_checked=1))
    monkeypatch.setattr(report_mod, "report", lambda r: None)

    assert main(["watch", "--once"]) == 0
    assert len(calls) == 1


def test_watch_once_exits_nonzero_if_a_mailbox_failed(monkeypatch):
    """So a scheduled run surfaces a bad password instead of looking healthy."""
    import src.inbox.__main__ as cli
    import src.inbox.report as report_mod
    import src.inbox.watcher as watcher_mod
    from src.inbox.watcher import PassResult

    monkeypatch.setattr(cli, "_install_redaction", lambda: None)
    monkeypatch.setattr(watcher_mod, "run_pass",
                        lambda *a, **k: PassResult(mailboxes_failed=["x"]))
    monkeypatch.setattr(report_mod, "report", lambda r: None)

    assert main(["watch", "--once"]) == 1
