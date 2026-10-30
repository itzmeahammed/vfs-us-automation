"""Cancelling a job must stop the child the way Ctrl-C does - and leave no
browser behind.

*** WHAT WENT WRONG. ***

`_terminate` called `process.terminate()`, which on Windows is TerminateProcess:
the child is destroyed where it stands. No `except KeyboardInterrupt`, no
`finally`. So nothing closed Chrome, and nothing recorded what the run had been
doing. Chrome is a GRANDchild of the job, so it was not killed either - it just
lost its parent and kept running.

Seen 2026-10-30 on AE-FRA job aa0986956806d353: cancelled from the web app one
step after `services`, on the way into payment. The log stops mid-sentence and
the browser was still on screen.

`src/booking/probe.py` has always handled an interrupt properly - it names the
step, says whether a payment is in flight, and its `finally` closes Chrome. The
API was simply never delivering an interrupt it could catch.

These tests use a stand-in child rather than the real bot: the thing under test
is the SIGNALLING, and a test that needed Chrome would never run in CI. Async
code is driven with `asyncio.run(scenario())`, as in test_api_jobs_durability.py
- pytest-asyncio is not a dependency.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "e" * 64)

from src.api.modules.jobs.manager import (                      # noqa: E402
    GRACEFUL_STOP_SECONDS,
    JobManager,
)

# A child that behaves like the bot: it installs the SAME handler the real
# entry points install, catches the interrupt, tidies up, and says so by writing
# a file. If it is killed outright the file never appears.
#
# It calls the shipped `install_graceful_stop` rather than rolling its own, so
# this test fails if that helper stops working - on Windows the whole mechanism
# depends on it, because SIGBREAK's default action is to terminate, and only
# SIGINT raises KeyboardInterrupt by itself.
POLITE_CHILD = """
import sys, time
sys.path.insert(0, REPO)
from src.utils.interrupts import install_graceful_stop
install_graceful_stop()
marker = sys.argv[1]
try:
    while True:
        time.sleep(0.05)
except BaseException:
    with open(marker, "w") as fh:
        fh.write("closed the browser")
""".replace("REPO", repr(str(REPO_ROOT)))

# A child that ignores the interrupt, like a run wedged in a blocking call.
STUBBORN_CHILD = """
import signal, sys, time
try:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
except Exception:
    pass
while True:
    time.sleep(0.05)
"""

# A child that spawns a GRANDchild and leaves it running - Chrome's shape.
CHILD_WITH_GRANDCHILD = """
import subprocess, sys, time
kid = subprocess.Popen([sys.executable, "-c",
                        "import time\\nwhile True: time.sleep(0.05)"])
with open(sys.argv[1], "w") as fh:
    fh.write(str(kid.pid))
while True:
    time.sleep(0.05)
"""


async def _spawn(source: str, *args: str):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", textwrap.dedent(source), *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        **JobManager._process_group_kwargs(),
    )


def _alive(pid: int) -> bool:
    """Is this pid still running? Without psutil, which is not a dependency."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=20, check=False).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.25)
    return not _alive(pid)


def test_the_child_gets_to_clean_up_after_itself(tmp_path):
    """*** THE ONE THAT LEFT A BROWSER ON SCREEN. ***

    The marker only exists if the child caught the interrupt and ran its own
    handler - which is where the real bot closes Chrome and writes its journal.
    """
    marker = tmp_path / "tidied.txt"

    async def scenario():
        process = await _spawn(POLITE_CHILD, str(marker))
        await asyncio.sleep(1.0)                  # let it reach its sleep loop
        await JobManager._terminate(process)
        return process.returncode

    returncode = asyncio.run(scenario())

    assert returncode is not None, "the child is still running"
    assert marker.exists(), "the child never ran its cleanup handler"
    assert marker.read_text() == "closed the browser"


def test_a_child_that_will_not_stop_is_killed(monkeypatch):
    """Politeness has a deadline. A run wedged in a blocking call must not keep
    the lane - and a stray browser is the lesser evil past that point."""
    monkeypatch.setattr(
        "src.api.modules.jobs.manager.GRACEFUL_STOP_SECONDS", 2)

    async def scenario():
        process = await _spawn(STUBBORN_CHILD)
        await asyncio.sleep(1.0)
        await JobManager._terminate(process)
        return process.returncode

    assert asyncio.run(scenario()) is not None


def test_a_grandchild_is_not_left_running(tmp_path, monkeypatch):
    """Chrome's shape. The browser is a grandchild of the job, so killing the
    job alone only orphans it - which is exactly what used to happen."""
    monkeypatch.setattr(
        "src.api.modules.jobs.manager.GRACEFUL_STOP_SECONDS", 2)
    pidfile = tmp_path / "grandchild.pid"

    async def scenario():
        process = await _spawn(CHILD_WITH_GRANDCHILD, str(pidfile))
        for _ in range(80):                       # wait for it to be spawned
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.25)
        pid = int(pidfile.read_text().strip())
        assert _alive(pid), "the grandchild never started"
        await JobManager._terminate(process)
        return pid

    grandchild = asyncio.run(scenario())

    assert _wait_gone(grandchild), (
        f"pid {grandchild} outlived the job it belonged to")


def test_terminating_a_child_that_already_exited_is_harmless():
    async def scenario():
        process = await _spawn("import sys\nsys.exit(0)")
        await process.wait()
        await JobManager._terminate(process)      # must not raise
        return process.returncode

    assert asyncio.run(scenario()) == 0


def test_the_grace_period_is_long_enough_to_be_worth_having():
    """It is not a tidiness nicety: in that window the bot closes the browser,
    records the step and reports whether a payment is in flight."""
    assert GRACEFUL_STOP_SECONDS >= 10


def test_children_are_spawned_as_their_own_process_group():
    """The interrupt is delivered to a GROUP. Without this the API would
    receive its own cancel signal, and on Windows there is no other way to
    interrupt another process at all."""
    kwargs = JobManager._process_group_kwargs()

    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


def test_the_handler_is_actually_installed():
    """On Windows the whole mechanism is this one handler. SIGBREAK's default
    action is to terminate; only SIGINT raises KeyboardInterrupt by itself."""
    import signal as sig

    from src.utils.interrupts import install_graceful_stop

    if sys.platform != "win32":
        assert install_graceful_stop() is False, "POSIX needs no handler"
        return

    previous = sig.getsignal(sig.SIGBREAK)
    try:
        assert install_graceful_stop() is True
        assert sig.getsignal(sig.SIGBREAK) is not sig.SIG_DFL
    finally:
        sig.signal(sig.SIGBREAK, previous)


def test_both_child_entry_points_install_it():
    """Every job the API spawns is one of these two. An entry point that does
    not install the handler dies abruptly again, and the only symptom is a
    browser window nobody closed - which is not a symptom anyone traces back
    here. So it is asserted rather than left to a comment."""
    for module in ("src/booking/__main__.py", "src/waitlist/__main__.py"):
        source = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "install_graceful_stop()" in source, module
