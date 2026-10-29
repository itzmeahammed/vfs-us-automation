"""Two fields under one caption block must not aim at the same control.

2026-10-29, AE-FRA "Your Details": France renders Passport Number and Passport
Expiry Date as two columns of ONE <app-dynamic-control>. Both field specs
matched that single block, `.first` took the same <input> for both, and the
expiry OVERWROTE the passport number — the portal showed "06/05/2029" under
"Please enter a valid passport number" and an empty expiry box.

The config was right. The locator silently aimed at the wrong control, which is
the worst shape a bug can take on a form that leads to a payment.

Hungary and the Netherlands address the same pair the same way, so this was
never one route's problem; Sweden had already been papered over with a raw
selector (input#passportExpirtyDate) instead of being fixed here.

These run a real browser, like test_waitlist_harvest.py: the defect was in how
a Playwright locator resolves against real markup, so a stubbed DOM would prove
nothing about it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "c" * 64)

playwright = pytest.importorskip("playwright.sync_api")

from src.waitlist import fields                             # noqa: E402
from src.waitlist.errors import WaitlistStepError           # noqa: E402

NUMBER = {"name": "passport_number", "label": "Passport Number",
          "widget": "text"}
EXPIRY = {"name": "passport_expiry", "label": "Passport Expiry Date",
          "widget": "date"}

#: One wrapper per field — what Hungary appears to render.
SEPARATE = """
<app-dynamic-control><label>Passport Number</label><input id="num"></app-dynamic-control>
<app-dynamic-control><label>Passport Expiry Date</label><input id="exp"></app-dynamic-control>
"""

#: Both fields in ONE wrapper, as a two-column row — what France renders.
SHARED = """
<app-dynamic-control>
  <div class="row">
    <div><label>Passport Number*</label><input id="num"></div>
    <div><label>Passport Expiry Date*</label><input id="exp"></div>
  </div>
</app-dynamic-control>
"""

#: The same, with real label/control association, which the browser honours.
SHARED_WITH_FOR = """
<app-dynamic-control>
  <div class="row">
    <div><label for="exp">Passport Expiry Date</label><input id="exp"></div>
    <div><label for="num">Passport Number</label><input id="num"></div>
  </div>
</app-dynamic-control>
"""


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page(browser, html):
    page = browser.new_page()
    page.set_content("<body>" + html + "</body>")
    return page


@pytest.mark.parametrize("html", [SEPARATE, SHARED, SHARED_WITH_FOR],
                         ids=["separate", "shared-row", "shared-with-for"])
def test_each_passport_field_fills_its_own_box(browser, html):
    """The regression, stated as the operator saw it: a passport number in the
    passport box and a date in the date box, whatever the row looks like."""
    page = _page(browser, html)
    try:
        fields._fill_text(page, NUMBER, "A1234567", 5000)
        fields._fill_date(page, EXPIRY, "06/05/2029", 5000)

        assert page.locator("#num").input_value() == "A1234567"
        assert page.locator("#exp").input_value() == "06/05/2029"
    finally:
        page.close()


def test_the_expiry_no_longer_resolves_to_the_number_input(browser):
    """The mechanism itself: before the fix both specs returned the same
    element, which is the only way the value of one lands in the other."""
    page = _page(browser, SHARED)
    try:
        number, _ = fields._locator(page, NUMBER, "text")
        expiry, _ = fields._locator(page, EXPIRY, "date")

        assert number.get_attribute("id") == "num"
        assert expiry.get_attribute("id") == "exp"
    finally:
        page.close()


def test_an_explicit_index_still_wins(browser):
    """The split phone field addresses two inputs under one caption by index.
    That path must keep working untouched — it was never the broken case."""
    page = _page(browser, """
    <app-dynamic-control>
      <label>Contact number</label>
      <input id="cc"><input id="phone">
    </app-dynamic-control>
    """)
    try:
        code = dict(NUMBER, label="Contact number", index=0)
        rest = dict(NUMBER, label="Contact number", index=1)

        assert fields._locator(page, code, "text")[0].get_attribute("id") == "cc"
        assert fields._locator(page, rest, "text")[0].get_attribute("id") == "phone"
    finally:
        page.close()


def test_a_single_candidate_is_unchanged(browser):
    """The overwhelmingly common case must not go near the new code path."""
    page = _page(browser, SEPARATE)
    try:
        locator, described = fields._locator(page, NUMBER, "text")

        assert locator.get_attribute("id") == "num"
        assert "[" not in described, "took the disambiguating path needlessly"
    finally:
        page.close()


def test_an_unresolvable_block_stops_the_run_instead_of_guessing(browser):
    """Two controls, and no ELEMENT carrying the caption.

    The caption is a bare text node on the wrapper, so nothing but a container
    holds it — and a container cannot say which of its inputs it introduces.

    Guessing here writes a date into a passport number and VFS rejects the
    application — so this stops, loudly, and the message says what to put in
    the route config. Same rule as _reworded_option in the slot check: an
    ambiguous match on a form that leads to a payment is not recoverable.
    """
    page = _page(browser, """
    <app-dynamic-control>
      Passport Number
      <div><input id="a"><input id="b"></div>
    </app-dynamic-control>
    """)
    try:
        with pytest.raises(WaitlistStepError, match="index"):
            fields._locator(page, NUMBER, "text")
    finally:
        page.close()


def test_the_error_names_the_field_and_the_remedy(browser):
    page = _page(browser, """
    <app-dynamic-control>
      Passport Number
      <div><input id="a"><input id="b"></div>
    </app-dynamic-control>
    """)
    try:
        with pytest.raises(WaitlistStepError) as caught:
            fields._locator(page, NUMBER, "text")
        message = str(caught.value)

        assert "passport_number" in message
        assert "Passport Number" in message
        assert "selector" in message, "should name both ways out"
    finally:
        page.close()
