"""Two failures seen live on AE-NLD, 2026-10-20 18:43.

1. The client record said "Indian"; VFS's Current Nationality list says
   "INDIA". The field waited 3 x 15s for an option that did not exist and
   failed without saying what was offered.
2. That failure left the browser on Your Details, so the next two
   combinations could not find the Appointment Details dropdowns at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist import runner  # noqa: E402
from src.waitlist.errors import WaitlistConfigError  # noqa: E402
from src.waitlist.fields import _NoSuchOption, best_option  # noqa: E402

COUNTRIES = ["INDIA", "INDONESIA", "UNITED ARAB EMIRATES", "UNITED KINGDOM",
             "UNITED STATES", "PAKISTAN", "SRI LANKA", "PHILIPPINES"]


# --------------------------------------------------------------------------- #
# best_option                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, expected", [
    ("Indian", "INDIA"),             # THE live case
    ("india", "INDIA"),              # case
    ("INDIA", "INDIA"),
    ("  India ", "INDIA"),           # spacing
    ("Emirati", "UNITED ARAB EMIRATES"),
    ("UAE", "UNITED ARAB EMIRATES"),
    ("Sri Lankan", "SRI LANKA"),
    ("Filipino", "PHILIPPINES"),
    ("Pakistani", "PAKISTAN"),
    ("Philip", "PHILIPPINES"),       # unique prefix
])
def test_a_value_finds_the_country_vfs_lists(value, expected):
    assert best_option(value, COUNTRIES) == expected


@pytest.mark.parametrize("value", [
    "United",     # three countries start with it — never guess
    "Ind",        # INDIA and INDONESIA
    "Martian",
    "",
])
def test_ambiguous_or_unknown_values_are_refused(value):
    assert best_option(value, COUNTRIES) is None


def test_exact_only_still_honours_case_and_aliases_but_not_prefixes():
    assert best_option("indian", COUNTRIES, exact_only=True) == "INDIA"
    assert best_option("Philip", COUNTRIES, exact_only=True) is None


def test_non_nationality_dropdowns_match_by_text():
    titles = ["Mr.", "Mrs.", "Ms."]
    assert best_option("mr.", titles) == "Mr."
    assert best_option("Mrs", titles) == "Mrs."


def test_the_error_names_the_closest_options():
    err = _NoSuchOption("Indain", COUNTRIES)
    assert "INDIA" in err.nearest()


# --------------------------------------------------------------------------- #
# Recovery between combinations                                                #
# --------------------------------------------------------------------------- #

class FakePage:
    def __init__(self, url):
        self.url = url
        self.visited = []

    def goto(self, url, **_):
        self.visited.append(url)
        self.url = url


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def start_booking(self, page, await_page=False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no Start New Booking button")
        page.url = "https://visa.vfsglobal.com/are/en/nld/application-detail"


LOGIN = "https://visa.vfsglobal.com/are/en/nld/login"


@pytest.fixture(autouse=True)
def no_loader(monkeypatch):
    monkeypatch.setattr(runner.turnstile, "wait_for_loader", lambda page: None)


def test_already_on_appointment_details_is_a_no_op():
    page = FakePage("https://visa.vfsglobal.com/are/en/nld/application-detail")
    bot = FakeBot()
    assert runner._ensure_appointment_page(bot, page, LOGIN) is page
    assert page.visited == [] and bot.calls == 0


def test_a_page_left_on_your_details_goes_back_via_the_dashboard():
    page = FakePage("https://visa.vfsglobal.com/are/en/nld/your-details")
    bot = FakeBot()
    runner._ensure_appointment_page(bot, page, LOGIN)
    assert page.visited == ["https://visa.vfsglobal.com/are/en/nld/dashboard"]
    assert bot.calls == 1
    assert page.url.endswith("application-detail")


def test_a_recovery_that_fails_is_a_clean_per_combo_failure():
    page = FakePage("https://visa.vfsglobal.com/are/en/nld/your-details")
    with pytest.raises(WaitlistConfigError, match="Appointment Details"):
        runner._ensure_appointment_page(FakeBot(fail=True), page, LOGIN)
