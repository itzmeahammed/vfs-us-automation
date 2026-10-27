"""runs/ retention: what it may delete, and everything it may not.

This module deletes files that cannot be recreated — a blocked IP, a locked
account, a half-filled form, a payment page. So the tests here are mostly about
what SURVIVES. The one that matters most is
test_an_unknown_screenshot_name_is_kept: the rule is an allowlist, and a new
capture added upstream must never start being deleted because nobody thought to
update this module.
"""

import os
import time

import pytest

from src.utils import runs_retention as retention


def _write(path, when=None, size=16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    if when is not None:
        os.utime(path, (when, when))
    return path


@pytest.fixture
def runs(tmp_path):
    return str(tmp_path / "runs")


OLD = time.time() - 30 * 86400
NEW = time.time()


# --------------------------------------------------------------------------- #
# The allowlist                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("filename", [
    "20261026_110035_AUT_after_signin.png",
    "20261026_110040_AUT_dashboard.png",
    "20261026_110045_AUT_final.png",
    "20261026_110045_final.png",               # before set_route(), no code
])
def test_the_three_milestones_are_recognised(filename):
    assert retention.milestone_name(filename)


@pytest.mark.parametrize("filename", [
    "20261002_143341_NOR_turnstile_fail_1.png",
    "20261022_183321_HUN_geo_blocked.png",
    "20261022_171317_SWE_account_locked.png",
    "20260929_201119_NLD_access_restricted.png",
    "20260929_173727_HUN_session_dead.png",
    "20261020_185304_NLD_waitlist_your_details_filled.png",
    "20261026_123553_select_slot_FAILED.png",
    "20261026_111303_ITA_otp_read_failed.png",
    "20261026_110035_AUT_dashboard_not_ready.png",
])
def test_evidence_is_never_a_milestone(filename):
    assert retention.milestone_name(filename) == "", filename


def test_an_unknown_screenshot_name_is_kept():
    """THE SAFETY PROPERTY. A capture this module has never heard of is
    evidence until someone says otherwise — the cost of keeping it is disk, the
    cost of deleting it is the only copy."""
    assert retention.milestone_name("20261026_110035_NOR_brand_new_block.png") == ""


def test_a_file_that_is_not_a_stamped_screenshot_is_kept():
    """DOM dumps, payment captures and anything written by hand."""
    for name in ("page.html", "notes.txt", "dashboard.png", "final.png"):
        assert retention.milestone_name(name) == "", name


# --------------------------------------------------------------------------- #
# Pruning                                                                      #
# --------------------------------------------------------------------------- #

def test_old_milestones_go_and_evidence_stays(runs):
    gone = _write(f"{runs}/abc123/20261001_100000_NOR_final.png", OLD)
    kept = _write(f"{runs}/abc123/20261001_100001_NOR_geo_blocked.png", OLD)

    report = retention.prune_runs(runs, max_age_days=7)

    assert not os.path.exists(gone)
    assert os.path.exists(kept), "a geo block was deleted as housekeeping"
    assert (report.files_removed, report.kept_evidence) == (1, 1)


def test_a_recent_milestone_is_kept(runs):
    """The run that just happened is the one someone is about to look at."""
    fresh = _write(f"{runs}/abc123/20261026_100000_NOR_dashboard.png", NEW)

    report = retention.prune_runs(runs, max_age_days=7)

    assert os.path.exists(fresh)
    assert report.kept_recent == 1


def test_the_walk_and_payment_captures_survive(runs):
    """runs/<ROUTE>/<run_id>/ and its payment/ subfolder are not milestones."""
    shot = _write(f"{runs}/AE-NOR/abc/20261001_120000_select_slot_FAILED.png", OLD)
    paid = _write(f"{runs}/AE-NOR/abc/payment/20261001_120500_otp_page.png", OLD)

    retention.prune_runs(runs, max_age_days=7)

    assert os.path.exists(shot) and os.path.exists(paid)


def test_a_dry_run_deletes_nothing(runs):
    path = _write(f"{runs}/abc123/20261001_100000_NOR_final.png", OLD)

    report = retention.prune_runs(runs, max_age_days=7, dry_run=True)

    assert os.path.exists(path), "dry run deleted a file"
    assert report.files_removed == 1, "dry run did not report what it would do"


def test_empty_run_folders_are_removed_but_runs_itself_is_not(runs):
    """VfsBot creates its folder when Chrome starts, before anything decides
    there is evidence to write — most empty folders are that."""
    os.makedirs(f"{runs}/empty_one", exist_ok=True)
    os.makedirs(f"{runs}/empty_two", exist_ok=True)

    report = retention.prune_runs(runs, max_age_days=7)

    assert report.dirs_removed == 2
    assert os.path.isdir(runs), "pruning removed runs/ itself"


def test_a_folder_emptied_by_the_prune_is_collected(runs):
    _write(f"{runs}/abc123/20261001_100000_NOR_final.png", OLD)

    retention.prune_runs(runs, max_age_days=7)

    assert not os.path.isdir(f"{runs}/abc123")


def test_a_missing_runs_directory_is_not_an_error(runs):
    """Housekeeping runs at startup and must never stop the server booting."""
    assert retention.prune_runs(runs + "_nope").files_removed == 0


def test_zero_days_removes_every_milestone(runs):
    _write(f"{runs}/abc/20261026_100000_NOR_final.png", NEW)
    kept = _write(f"{runs}/abc/20261026_100001_NOR_geo_blocked.png", NEW)

    report = retention.prune_runs(runs, max_age_days=0)

    assert report.files_removed == 1
    assert os.path.exists(kept)
