"""No traceback ever reaches the console (src/utils/cli_guard.py)."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from src.utils import cli_guard, public_errors as pe


@pytest.fixture
def crash_log(tmp_path, monkeypatch):
    path = tmp_path / "crashes.log"
    monkeypatch.setattr(cli_guard, "CRASH_LOG", str(path))
    return path


@pytest.fixture
def console(monkeypatch):
    """The root logger writing to a buffer, as an API job's log would."""
    from src.utils.log_setup import ConsoleFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(ConsoleFormatter("%(levelname)s | %(message)s"))
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = [handler]
    yield buf
    root.handlers = saved


def test_the_2026_10_23_unicode_crash_is_one_line(crash_log, console):
    def main():
        raise UnicodeEncodeError("charmap", "✓", 0, 1,
                                 "character maps to <undefined>")

    assert cli_guard.run(main) == 1
    out = console.getvalue()
    assert "Traceback" not in out and out.count("\n") == 1
    assert pe.UNIDENTIFIED in out
    assert "Traceback" in crash_log.read_text(encoding="utf-8")


def test_a_recognised_crash_says_what_vfs_did(crash_log, console):
    def main():
        raise RuntimeError("Page.goto: Timeout 30000ms exceeded")

    cli_guard.run(main)
    assert "VFS did not respond in time." in console.getvalue()


def test_the_web_app_gets_the_unidentified_sentence(crash_log, console):
    cli_guard.run(lambda: 1 / 0)
    line = console.getvalue().strip().split(" | ", 1)[1]
    assert pe.public_message(line) == pe.UNIDENTIFIED


def test_without_logging_it_prints_one_timestamped_line(crash_log, capsys,
                                                       monkeypatch):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    cli_guard.run(lambda: 1 / 0)
    err = capsys.readouterr().err
    assert "Traceback" not in err and "| ERROR" in err


def test_exit_and_ctrl_c_pass_through(crash_log):
    with pytest.raises(SystemExit):
        cli_guard.run(lambda: sys.exit(3))
    with pytest.raises(KeyboardInterrupt):
        cli_guard.run(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))


def test_a_normal_return_is_untouched(crash_log):
    assert cli_guard.run(lambda: 0) == 0
