"""A waitlist command that stops must say why, in one line and in its JSON.

The bug this pins: VFS answered a login with "The entered email id is not
registered with us", EmailNotRegisteredError escaped `python -m src.waitlist
run --json` as a 35-line traceback, and the API job could only report
"Process exited with code 1".
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.log_setup import ConsoleFormatter  # noqa: E402
from src.vfs_bot import errors as bot_errors  # noqa: E402
from src.waitlist import __main__ as cli  # noqa: E402
from src.waitlist.errors import WaitlistConfigError  # noqa: E402

NOT_REGISTERED = "The entered email id is not registered with us."


@pytest.fixture()
def console():
    """What a job log would show: a console handler on the root logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ConsoleFormatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)


def _raised(exc: Exception) -> Exception:
    try:
        raise exc
    except Exception as caught:                  # noqa: BLE001
        return caught


def _block(out: str) -> dict:
    body = out.split(cli.RESULT_JSON_BEGIN)[1].split(cli.RESULT_JSON_END)[0]
    return json.loads(body)


def test_a_refused_login_is_one_line_and_a_result_block(console, capsys):
    exc = _raised(bot_errors.EmailNotRegisteredError(
        "Login page: 'The entered email id is not registered with us' — "
        "skipping this URL for this account."))

    code = cli._report_failure(argparse.Namespace(json_output=True), exc)

    assert code == 1
    assert console.getvalue().strip() == f"ERROR Run failed: {NOT_REGISTERED}"
    block = _block(capsys.readouterr().out)
    assert block["outcome"] == "failed"
    assert block["error"] == {"code": "email_not_registered",
                              "message": NOT_REGISTERED}


def test_an_unexpected_failure_keeps_its_traceback(console, capsys):
    exc = _raised(KeyError("selector"))

    cli._report_failure(argparse.Namespace(json_output=True), exc)

    assert "Traceback (most recent call last)" in console.getvalue()
    assert _block(capsys.readouterr().out)["error"]["code"] == "unexpected_error"


def test_no_result_block_without_json(console, capsys):
    cli._report_failure(argparse.Namespace(json_output=False),
                        _raised(WaitlistConfigError("No login URL for AE-XXX.")))

    assert console.getvalue().strip() == "ERROR No login URL for AE-XXX."
    assert cli.RESULT_JSON_BEGIN not in capsys.readouterr().out


@pytest.mark.parametrize("exc,code", [
    (bot_errors.InvalidCredentialsError("x"), "invalid_credentials"),
    (bot_errors.AccountLockedError("Account Locked (429202)"), "account_locked"),
    (bot_errors.LoginBouncedError("bounced"), "login_bounced"),
    (bot_errors.OtpReadError("no code"), "otp_read"),
    (WaitlistConfigError("bad"), "waitlist_config"),
])
def test_every_named_failure_has_a_code_and_is_understood(exc, code):
    assert cli._explain_failure(exc)[0] == code
    assert cli._explain_failure(exc)[2] is True
