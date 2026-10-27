"""Who takes screenshots, and who no longer does.

The slot check takes none. Everything else still does — and the thing that
makes that distinction hard is that they all run the SAME login, OTP, Turnstile
and block-detection code. The tests here pin the boundary, because the failure
mode is silent: a leak would simply mean the next booking or waitlist failure
has no picture, and nobody finds out until they go looking for one.
"""

import os

from src.vfs_bot import diagnostics


class _Page:
    """Records whether anything actually asked the browser for a picture."""

    def __init__(self):
        self.shots = 0

    def screenshot(self, **kwargs):
        self.shots += 1
        path = kwargs.get("path")
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"png")

    @property
    def url(self):
        return "https://visa.vfsglobal.com/are/en/nor/login"


def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "SCREENSHOT_DIR", str(tmp_path / "runs" / "x"))


# --------------------------------------------------------------------------- #
# The chokepoint                                                               #
# --------------------------------------------------------------------------- #

def test_a_suppressed_run_writes_nothing(tmp_path, monkeypatch):
    _in_tmp(tmp_path, monkeypatch)
    page = _Page()

    with diagnostics.suppressed():
        diagnostics.take_final_screenshot(page, "geo_blocked")
        diagnostics.take_screenshot(page, "otp_step")
        diagnostics.take_milestone_screenshot(page, "dashboard")
        diagnostics.write_screenshot(page, "raw")

    assert page.shots == 0, "a suppressed run still asked for a screenshot"


def test_a_failure_capture_is_written_when_not_suppressed(tmp_path, monkeypatch):
    """The default for every non-slot-check run: failures are always captured."""
    _in_tmp(tmp_path, monkeypatch)
    page = _Page()

    diagnostics.take_final_screenshot(page, "geo_blocked")

    assert page.shots == 1


def test_suppression_is_a_no_op_when_inactive(tmp_path, monkeypatch):
    """So a caller can pass a config flag straight in."""
    _in_tmp(tmp_path, monkeypatch)
    page = _Page()

    with diagnostics.suppressed(False):
        diagnostics.take_final_screenshot(page, "account_locked")

    assert page.shots == 1


# --------------------------------------------------------------------------- #
# The boundary                                                                 #
# --------------------------------------------------------------------------- #

def test_suppression_is_restored_after_the_block(tmp_path, monkeypatch):
    """THE LEAK THIS GUARDS AGAINST.

    The supervisor fires a waitlist registration in its OWN process once the
    slot-check browser has closed. Those captures diagnosed Italy's form on
    2026-10-26. If suppression outlived the slot check, the next waitlist
    failure would be a log line and nothing else.
    """
    _in_tmp(tmp_path, monkeypatch)
    page = _Page()

    with diagnostics.suppressed():
        diagnostics.take_final_screenshot(page, "slot_check_geo_blocked")
    assert page.shots == 0

    diagnostics.take_final_screenshot(page, "waitlist_your_details_filled")
    assert page.shots == 1, "suppression leaked past the slot-check run"
    assert not diagnostics.screenshots_suppressed()


def test_suppression_is_restored_even_when_the_run_raises(tmp_path, monkeypatch):
    """A slot check that dies on a geo block is the COMMON case, not the edge."""
    _in_tmp(tmp_path, monkeypatch)

    try:
        with diagnostics.suppressed():
            raise RuntimeError("geo-blocked")
    except RuntimeError:
        pass

    assert not diagnostics.screenshots_suppressed()
    page = _Page()
    diagnostics.take_final_screenshot(page, "waitlist_review_pay_filled")
    assert page.shots == 1


def test_nesting_does_not_un_suppress(tmp_path, monkeypatch):
    """An inner suppressed(False) must not re-enable an outer suppression."""
    _in_tmp(tmp_path, monkeypatch)
    page = _Page()

    with diagnostics.suppressed():
        with diagnostics.suppressed(False):
            diagnostics.take_final_screenshot(page, "session_dead")

    assert page.shots == 0


# --------------------------------------------------------------------------- #
# A run that shows nothing leaves nothing                                      #
# --------------------------------------------------------------------------- #

def test_a_suppressed_run_creates_no_folder(tmp_path, monkeypatch):
    """VfsBot used to mkdir the run folder when Chrome started, so a healthy
    run left an empty runs/<run_id>/ — ~48 a day once the slot check stopped
    capturing. The folder is now made by the first write that happens."""
    target = tmp_path / "runs" / "abc123"
    monkeypatch.setattr(diagnostics, "SCREENSHOT_DIR", str(target))

    with diagnostics.suppressed():
        diagnostics.take_final_screenshot(_Page(), "geo_blocked")

    assert not target.exists(), "a run that captured nothing still left a folder"


def test_the_folder_is_created_by_the_first_real_write(tmp_path, monkeypatch):
    target = tmp_path / "runs" / "abc123"
    monkeypatch.setattr(diagnostics, "SCREENSHOT_DIR", str(target))
    page = _Page()

    diagnostics.take_final_screenshot(page, "account_locked")

    assert page.shots == 1
    assert target.is_dir(), "the failure capture had nowhere to go"


def test_vfs_bot_does_not_create_the_folder_up_front():
    """Read off the source — the regression is invisible at runtime."""
    import inspect

    from src.vfs_bot import vfs_bot

    source = inspect.getsource(vfs_bot)
    assert "makedirs(diagnostics.SCREENSHOT_DIR" not in source, (
        "VfsBot creates the run folder again; healthy runs will leave empties")


# --------------------------------------------------------------------------- #
# The wiring                                                                   #
# --------------------------------------------------------------------------- #

def test_the_supervisor_suppresses_only_around_the_bot_run():
    """Read off the source: the `with` must contain bot.run() and nothing more.

    If it widened to cover the waitlist auto-trigger (which the supervisor calls
    later in the same process) that run would lose its captures, and the only
    symptom would be a missing picture months from now.
    """
    import inspect

    from src import supervisor

    source = inspect.getsource(supervisor)
    assert "diagnostics.suppressed(" in source, (
        "the slot check no longer scopes its screenshot policy")
    block = source[source.index("diagnostics.suppressed("):]
    block = block[:block.index("return getattr(bot")]
    assert "bot.run()" in block, "suppression does not cover the slot-check run"
    assert "autotrigger" not in block and "handle_waitlist_opened" not in block, (
        "suppression reaches the waitlist auto-trigger")
