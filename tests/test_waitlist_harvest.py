"""`doctor --harvest` — dropdown options must be OBSERVED, never guessed.

A route config says `"widget": "mat-select"` but not which values the portal
accepts. That gap is where wrong data comes from: the client files in this repo
disagree about what a country is called ("India", "Belize", "Lebanese"), because
each was typed from memory. A guess only fails minutes into a live run, when the
option cannot be clicked — after a login, a Turnstile solve and a form step.

The harvester closes that by reading each dropdown off the real page. These
tests cover the two halves that can go wrong without anyone noticing:

  * REFUSING a list that is probably incomplete. A searchable or virtually
    scrolled dropdown renders a slice of its options; writing that slice would
    flip the field to options_status="known" and start REJECTING valid
    countries. That is strictly worse than never harvesting, because it looks
    authoritative. Staying "unknown" costs nothing.

  * WRITING BACK without destroying the file. Those configs are mostly
    "_comment" keys explaining why each selector looks the way it does — the
    reason anyone can maintain a route months later. An update that dropped
    them would trade one kind of knowledge for another.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.waitlist import doctor  # noqa: E402

playwright = pytest.importorskip("playwright.sync_api")

OPTIONS = ["Female", "Male", "Not Specified", "Others / Transgender"]
SPEC = {"name": "gender", "label": "Gender", "widget": "mat-select"}


def _page_html(options, overlay_extra=""):
    """A Material-shaped dropdown whose overlay opens on click."""
    opts = "".join(
        f'<mat-option role="option"><span>  {o}  </span></mat-option>'
        for o in options
    )
    return f"""
    <app-dynamic-control>
      <div> Gender<span class="asterisk">*</span></div>
      <mat-select role="combobox" id="mat-select-3"
                  onclick="document.getElementById('ov').style.display='block'">
        <span>Select</span>
      </mat-select>
    </app-dynamic-control>
    <div class="cdk-overlay-pane" id="ov" style="display:none">
      {overlay_extra}
      <div role="listbox">{opts}</div>
    </div>
    """


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _harvest(browser, html):
    page = browser.new_page()
    page.set_content("<body>" + html + "</body>")
    try:
        return doctor.harvest_select(page, SPEC, "your_details")
    finally:
        page.close()


def test_a_clean_dropdown_is_read_verbatim(browser):
    result = _harvest(browser, _page_html(OPTIONS))
    assert result.usable
    # Whitespace is stripped, but the wording is untouched: the bot matches the
    # option by this exact text, so normalising it would break the match.
    assert result.options == OPTIONS


def test_a_searchable_dropdown_is_refused(browser):
    result = _harvest(
        browser,
        _page_html(OPTIONS, '<input type="search" placeholder="Search">'),
    )
    assert not result.usable
    assert result.options == []
    assert "search box" in result.skipped


def test_a_virtually_scrolled_dropdown_is_refused(browser):
    result = _harvest(
        browser,
        _page_html(
            OPTIONS,
            "<cdk-virtual-scroll-viewport></cdk-virtual-scroll-viewport>"),
    )
    assert not result.usable
    assert result.options == []
    assert "virtual scrolling" in result.skipped


def test_an_unopenable_dropdown_is_reported_not_crashed(browser):
    page = browser.new_page()
    page.set_content("<body><div>no dropdown here</div></body>")
    try:
        result = doctor.harvest_select(page, SPEC, "your_details")
    finally:
        page.close()
    assert not result.usable
    assert result.skipped


# --------------------------------------------------------------------------
# Writing back
# --------------------------------------------------------------------------


@pytest.fixture
def route_file(tmp_path, monkeypatch):
    """A throwaway route config, so no real file is edited by the tests."""
    from src.waitlist import config as waitlist_config

    config_dir = tmp_path / "waitlist"
    config_dir.mkdir()
    path = config_dir / "ZZ-TST.json"
    path.write_text(json.dumps({
        "_comment_top": "why this route looks like this",
        "enabled": False,
        "steps": [{
            "name": "your_details",
            "_comment_step": "why this step looks like this",
            "fields": [
                {"name": "gender", "widget": "mat-select",
                 "value": "{{gender}}",
                 "_comment": "why this field looks like this"},
                {"name": "first_name", "widget": "text",
                 "value": "{{first_name}}"},
            ],
        }, {
            "name": "review_pay", "commits": True,
            "fields": [], "submit": {"role": "button", "name": "Confirm"},
        }],
    }, indent=2), encoding="utf-8")

    monkeypatch.setattr(waitlist_config, "WAITLIST_DIR", str(config_dir))
    waitlist_config.clear_cache()
    yield path
    waitlist_config.clear_cache()


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_writing_options_keeps_every_comment(route_file):
    doctor.apply_harvest(
        "ZZ-TST",
        [doctor.Harvest("your_details", "gender", OPTIONS)],
        "2026-08-26T12:00:00Z",
    )
    written = _load(route_file)
    assert written["_comment_top"] == "why this route looks like this"
    step = written["steps"][0]
    assert step["_comment_step"] == "why this step looks like this"
    field = step["fields"][0]
    assert field["_comment"] == "why this field looks like this"
    # ...and the actual point of the write
    assert field["options"] == OPTIONS
    assert field["options_captured_at"] == "2026-08-26T12:00:00Z"


def test_only_the_harvested_field_is_touched(route_file):
    before = _load(route_file)["steps"][0]["fields"][1]
    doctor.apply_harvest(
        "ZZ-TST",
        [doctor.Harvest("your_details", "gender", OPTIONS)],
        "2026-08-26T12:00:00Z",
    )
    assert _load(route_file)["steps"][0]["fields"][1] == before


def test_a_refused_list_is_never_written(route_file):
    """The guard is worthless if the write path ignores it."""
    changes = doctor.apply_harvest(
        "ZZ-TST",
        [doctor.Harvest("your_details", "gender", [],
                        skipped="the overlay has a search box")],
        "2026-08-26T12:00:00Z",
    )
    assert changes == []
    assert "options" not in _load(route_file)["steps"][0]["fields"][0]


def test_a_changed_list_is_reported_as_changed(route_file):
    doctor.apply_harvest("ZZ-TST",
                         [doctor.Harvest("your_details", "gender", OPTIONS)],
                         "2026-08-26T12:00:00Z")
    changes = doctor.apply_harvest(
        "ZZ-TST",
        [doctor.Harvest("your_details", "gender",
                        OPTIONS + ["Prefer not to say"])],
        "2026-08-27T12:00:00Z",
    )
    assert any("CHANGED" in c for c in changes), (
        "a portal that quietly adds an option must be surfaced, not absorbed"
    )


def test_an_unchanged_list_still_refreshes_the_date(route_file):
    doctor.apply_harvest("ZZ-TST",
                         [doctor.Harvest("your_details", "gender", OPTIONS)],
                         "2026-08-26T12:00:00Z")
    doctor.apply_harvest("ZZ-TST",
                         [doctor.Harvest("your_details", "gender", OPTIONS)],
                         "2026-08-27T12:00:00Z")
    field = _load(route_file)["steps"][0]["fields"][0]
    assert field["options_captured_at"] == "2026-08-27T12:00:00Z", (
        "the date records when the list was last CONFIRMED, so a re-run that "
        "finds no change must still move it forward"
    )
