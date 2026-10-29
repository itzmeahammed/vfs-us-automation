"""The OTP page VFS shows mid-booking, after Your Details.

2026-10-29, AE-FRA job 4cb31459180a1750: 'your_details' and 'details_summary'
both submitted, then VFS presented "One-time Password (OTP)". The walk had no
step for it, so 'select_slot' waited 45s for a book-appointment URL that was
never coming and the run died three pages deep with a completed application
form behind it.

What these pin, in order of what it costs to get wrong:

  * GENERATE IS CLICKED ONCE. The page says "You have left 3 attempt of
    generating new OTP. After crossing limit You will be logged off." Being
    logged off here abandons a submitted form, so a rejected code is recovered
    by RE-READING THE SAME EMAIL, never by asking for a new one.
  * A missing mailbox password is refused by name, before any IMAP connection,
    and never falls back to the VFS portal password.
  * A dry run never requests a code at all.

Driven against a reconstruction of the real page in a real browser, like
test_field_label_ambiguity.py: the step is a sequence of clicks against live
markup, and a stubbed DOM would prove nothing about it. The mailbox itself is
stubbed — reading the code is otp_service's job and the Greece route already
covers it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "b" * 64)

playwright = pytest.importorskip("playwright.sync_api")

from src.booking import config as booking_config                # noqa: E402
from src.booking import otp_step                                # noqa: E402
from src.waitlist.errors import WaitlistStepError               # noqa: E402

#: The page as VFS renders it: a Generate button that reveals the code field,
#: a Verify button, and the rejection sentence the bot matches on.
PAGE = """
<div id="panel">
  <h3>One-time Password (OTP)</h3>
  <p>A One-time password (OTP) will be sent to the registered email address
     and/ or contact number/ or What's app.</p>
  <button id="gen" onclick="document.getElementById('entry').style.display='block'">
    Generate OTP</button>
  <div id="entry" style="display:none">
    <p>Enter One-time Password (OTP)</p>
    <input placeholder="OTP" id="otp">
    <button id="verify" onclick="doVerify()">Verify</button>
  </div>
  <p id="err" style="display:none">Please enter a valid one time password (OTP).</p>
</div>
<button id="continue">Continue</button>
<script>
  window.__generates = 0;
  document.getElementById('gen').addEventListener('click', () => window.__generates++);
  function doVerify() {
    if (document.getElementById('otp').value === window.__good) {
      document.getElementById('panel').remove();
    } else {
      document.getElementById('err').style.display = 'block';
    }
  }
</script>
"""


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def real_step():
    """The real AE-FRA step, not a hand-written copy of it.

    If the config stops carrying this step, or its selectors drift from the
    markup above, these tests are what notices.
    """
    return next(s for s in booking_config.steps_for("AE-FRA", "new")
                if s.get("name") == "email_otp")


@pytest.fixture
def step(real_step):
    """The real step with only the post-Verify pause shortened.

    That pause exists to give VFS time to answer and is 4s in the config, which
    a retry test pays twice. Every selector, every button name and the attempt
    budget stay exactly as shipped — those are the parts that drift.
    """
    return dict(real_step, verify_wait_ms=250)


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.set_content("<body>" + PAGE + "</body>")
    p.evaluate("window.__good = '160064'")
    yield p
    p.close()


#: VFS's actual OTP mail for France, 2026-10-29. Plain text, and the helpline
#: number is in it on purpose: a reader that grabs the first run of digits it
#: sees would return part of that instead of the code.
MAIL_BODY = """Dear Applicant,

The OTP for your application with VFS Global is {code}. The OTP will expire in
3 mins.

If you require any assistance, please contact our helpline.

Thank you.

Regards,
Application Centre Helpdesk Team
Helpline Number: + 27 (0) 87 825 0675
Email: info@vfshelpline.com
"""


class _Mail:
    def __init__(self, code):
        self.body_text = MAIL_BODY.format(code=code)
        self.html_text = ""


@pytest.fixture
def mailbox(monkeypatch):
    """The account's mailbox, holding one real VFS OTP mail.

    Only the IMAP fetch is stubbed. The code is read by the SHIPPED reader —
    text_otp for the text mode France uses — against the real message body,
    so a change that breaks the parse shows up here rather than on a live run.
    Mutate `codes[0]` to change which code the mail carries.
    """
    import src.utils.otp_service as service

    codes = ["160064"]
    monkeypatch.setattr(service, "wait_for_otp_mail",
                        lambda *a, **k: _Mail(codes[0]))
    monkeypatch.setattr(service, "otp_length", lambda: 6)
    return codes


def _generates(page) -> int:
    return int(page.evaluate("window.__generates"))


def test_the_step_is_configured_for_france(real_step):
    """The config half of the fix, asserted on its own: a perfect step
    implementation nothing routes to is not a fix."""
    assert real_step["type"] == "email_otp"
    assert real_step.get("if_present") is True, (
        "VFS does not ask every session; a mandatory step would fail the runs "
        "where it never appears")


def test_a_code_is_read_and_verified(page, step, mailbox):
    assert otp_step.is_asked(page, step) is True

    detail = otp_step.verify(page, step, account_email="a@b.com",
                             mailbox_password="pw")

    assert "verified" in detail
    assert otp_step.is_asked(page, step) is False, "the panel should be gone"


def test_a_rejected_code_never_regenerates(page, step, mailbox):
    """*** THE ONE THAT COSTS A BOOKING. ***

    VFS allows three generations and logs the session off after that, with a
    submitted application form behind it. So a rejection must never ask for a
    new code, whatever else it does.

    France reads the code as TEXT, which is deterministic: re-reading the same
    mail cannot produce different digits, so the step says so and stops rather
    than spending a second verify on the same value. Either way Generate is
    clicked exactly once for the whole step.
    """
    page.evaluate("window.__good = '999999'")       # VFS rejects what we send

    with pytest.raises(WaitlistStepError) as caught:
        otp_step.verify(page, step, account_email="a@b.com",
                        mailbox_password="pw")

    assert "nothing was charged" in str(caught.value).lower()
    assert _generates(page) == 1, (
        f"clicked Generate {_generates(page)} times — each one spends a "
        "rationed attempt and the third logs the session off")


def test_an_image_mode_rejection_is_recovered_by_a_re_read(page, real_step,
                                                           monkeypatch):
    """Where a re-read CAN differ, it is used — still without regenerating.

    A route without "otp_mode": "text" reads the PNG via OpenAI, where a second
    reading at a higher temperature genuinely can correct a misread digit.
    """
    import src.utils.otp_service as service

    readings = iter(["000000", "160064"])
    monkeypatch.setattr(service, "wait_for_otp_mail", lambda *a, **k: object())
    monkeypatch.setattr(service, "otp_length", lambda: 6)
    monkeypatch.setattr(service, "extract_code", lambda *a, **k: next(readings))

    image_step = {k: v for k, v in real_step.items() if k != "otp_mode"}
    image_step["verify_wait_ms"] = 250

    detail = otp_step.verify(page, image_step, account_email="a@b.com",
                             mailbox_password="pw")

    assert "attempt 2" in detail
    assert _generates(page) == 1


def test_an_already_visible_field_is_not_regenerated(page, step, mailbox):
    """A code already sent must not be thrown away by asking for another."""
    page.evaluate("document.getElementById('entry').style.display='block'")

    otp_step.verify(page, step, account_email="a@b.com", mailbox_password="pw")

    assert _generates(page) == 0




def test_a_missing_mailbox_password_is_refused_by_name(page, step, mailbox):
    """email_password NEVER falls back to the VFS portal password — the two
    were one value until 2026-10-16 and assuming they still match is the bug
    that split exists to prevent."""
    with pytest.raises(WaitlistStepError) as caught:
        otp_step.verify(page, step, account_email="a@b.com",
                        mailbox_password="")
    message = str(caught.value)

    assert "a@b.com" in message, "should name the account"
    assert "credentials.local.ini" in message, "should name the fix"
    assert _generates(page) == 0, "asked VFS for a code it could never read"


def test_an_unknown_account_is_refused_before_clicking(page, step, mailbox):
    with pytest.raises(WaitlistStepError):
        otp_step.verify(page, step, account_email="", mailbox_password="pw")

    assert _generates(page) == 0


def test_a_dry_run_never_requests_a_code(page, step, mailbox):
    """A mapping run must not spend one of the account's rationed codes."""
    detail = otp_step.verify(page, step, account_email="a@b.com",
                             mailbox_password="pw", dry_run=True)

    assert "dry run" in detail
    assert _generates(page) == 0
    assert otp_step.is_asked(page, step) is True


def test_a_page_without_the_panel_is_not_asking(browser, step):
    """The skip path: VFS does not show this every session, and a step that is
    absent must cost a second, not a step timeout."""
    p = browser.new_page()
    try:
        p.set_content("<body><h3>Your Details Summary</h3></body>")
        assert otp_step.is_asked(p, step) is False
    finally:
        p.close()


def test_the_mail_wait_is_shorter_than_the_pages_own_expiry(real_step):
    """The page says the code is valid for 3 minutes. Waiting longer returns a
    code it has already expired, and submitting a dead code spends a verify."""
    assert int(real_step["mail_wait_seconds"]) < 180


def test_the_shipped_step_pauses_after_verify(real_step):
    """The fixture above shortens this; the shipped value still has to be long
    enough for VFS to answer before the panel is re-read."""
    assert int(real_step.get("verify_wait_ms") or 4000) >= 2000


def test_the_france_step_reads_the_real_vfs_mail(real_step):
    """End to end on the parse: the shipped otp_mode, the shipped reader, and
    the message body VFS actually sends."""
    from src.utils import text_otp

    assert real_step["otp_mode"] == "text", "no AI call for a plain-text mail"
    assert text_otp.extract_code(_Mail("160064"), 6) == "160064"


def test_the_helpline_number_is_not_mistaken_for_the_code(real_step):
    """'+ 27 (0) 87 825 0675' sits in every one of these emails."""
    from src.utils import text_otp

    assert text_otp.extract_code(_Mail("482915"), 6) == "482915"


#: What VFS actually does, learned from b698e31e5c818f07: a correct code is
#: accepted, the OTP panel STAYS ON SCREEN, and the page's Continue button
#: becomes enabled. Nothing disappears.
PAGE_PANEL_STAYS = """
<div id="panel">
  <h3>One-time Password (OTP)</h3>
  <button id="gen" onclick="document.getElementById('entry').style.display='block'">
    Generate OTP</button>
  <div id="entry" style="display:none">
    <input placeholder="OTP" id="otp">
    <button id="verify" onclick="doVerify()">Verify</button>
  </div>
  <p id="err" style="display:none">Please enter a valid one time password (OTP).</p>
</div>
<button id="continue" disabled>Continue</button>
<script>
  window.__generates = 0;
  document.getElementById('gen').addEventListener('click', () => window.__generates++);
  function doVerify() {
    if (document.getElementById('otp').value === window.__good) {
      document.getElementById('continue').removeAttribute('disabled');
    } else {
      document.getElementById('err').style.display = 'block';
    }
  }
</script>
"""


def test_a_panel_that_stays_after_a_correct_code_is_still_accepted(browser,
                                                                   step,
                                                                   mailbox):
    """*** THE REGRESSION. ***

    2026-10-29, b698e31e5c818f07: code 366635 was read and ACCEPTED on attempt
    1. VFS left the panel up and enabled Continue. This read the lingering
    panel as "not verified", retried, and the retry tried to fill an OTP box on
    a page that had meanwhile advanced to book-appointment — a 30s
    Locator.fill timeout that failed a booking VFS had already accepted, after
    the operator clicked Continue by hand.
    """
    page = browser.new_page()
    page.set_content("<body>" + PAGE_PANEL_STAYS + "</body>")
    page.evaluate("window.__good = '160064'")
    try:
        detail = otp_step.verify(page, step, account_email="a@b.com",
                                 mailbox_password="pw")

        assert "verified on attempt 1" in detail, (
            "accepted on the first try, so there must be no second attempt")
        assert page.locator("#continue").is_enabled(), (
            "VFS enabled Continue — walk.py's submit is what clicks it")
        assert int(page.evaluate("window.__generates")) == 1
    finally:
        page.close()


def test_a_page_that_moved_on_mid_retry_is_not_refilled(browser, real_step,
                                                        monkeypatch):
    """The 30s timeout itself.

    A human can click Continue while this is running, which takes the OTP box
    off the page. A retry must RE-LOCATE and stop when it is gone, instead of
    blocking on a locator that can never resolve — that wait was the 30s
    Locator.fill timeout which turned an accepted booking into a failed one.

    Image mode, because text mode refuses a second attempt earlier (a
    deterministic re-read cannot differ) and so never reaches this guard.
    """
    import src.utils.otp_service as service

    readings = iter(["000000", "160064"])
    monkeypatch.setattr(service, "wait_for_otp_mail", lambda *a, **k: object())
    monkeypatch.setattr(service, "otp_length", lambda: 6)
    monkeypatch.setattr(service, "extract_code", lambda *a, **k: next(readings))

    page = browser.new_page()
    # Verify refuses the code AND the entry box goes away, as it does when the
    # page advances under the bot's feet.
    page.set_content("""<body>
      <div id="panel">
        <h3>One-time Password (OTP)</h3>
        <button id="gen" onclick="document.getElementById('entry').style.display='block'">
          Generate OTP</button>
        <div id="entry" style="display:none">
          <input placeholder="OTP" id="otp">
          <button id="verify" onclick="gone()">Verify</button>
        </div>
        <p id="err" style="display:none">Please enter a valid one time password (OTP).</p>
      </div>
      <script>
        window.__generates = 0;
        document.getElementById('gen').addEventListener('click', () => window.__generates++);
        function gone() {
          document.getElementById('err').style.display = 'block';
          document.getElementById('entry').remove();
        }
      </script>
    </body>""")
    step = dict(real_step, verify_wait_ms=250)
    step.pop("otp_mode", None)
    try:
        detail = otp_step.verify(page, step, account_email="a@b.com",
                                 mailbox_password="pw")

        assert "advanced" in detail, detail
        assert int(page.evaluate("window.__generates")) == 1, (
            "asked VFS for another code instead of noticing the page moved")
    finally:
        page.close()


def test_a_live_continue_is_reported_as_positive_evidence(browser, step,
                                                          mailbox, caplog):
    """Continue going live is the portal AGREEING, which is stronger than no
    banner having appeared."""
    import logging

    page = browser.new_page()
    page.set_content("<body>" + PAGE_PANEL_STAYS + "</body>")
    page.evaluate("window.__good = '160064'")
    try:
        with caplog.at_level(logging.INFO, logger="src.booking.otp_step"):
            otp_step.verify(page, step, account_email="a@b.com",
                            mailbox_password="pw")

        assert "Continue is live" in caplog.text
    finally:
        page.close()


def test_a_dead_continue_without_a_banner_is_said_out_loud(browser, step,
                                                           mailbox, caplog):
    """The run will stop at the next step; the log has to say why HERE, not
    leave it reading as a slot-picker fault three steps later."""
    import logging

    page = browser.new_page()
    # Accepts the code silently and never enables Continue - the odd state.
    page.set_content("""<body>
      <div id="panel">
        <h3>One-time Password (OTP)</h3>
        <button id="gen" onclick="document.getElementById('entry').style.display='block'">
          Generate OTP</button>
        <div id="entry" style="display:none">
          <input placeholder="OTP" id="otp">
          <button id="verify">Verify</button>
        </div>
      </div>
      <button id="continue" disabled>Continue</button>
      <script>
        window.__generates = 0;
        document.getElementById('gen').addEventListener('click', () => window.__generates++);
      </script>
    </body>""")
    fast = dict(step, continue_wait_ms=1200)
    try:
        with caplog.at_level(logging.WARNING, logger="src.booking.otp_step"):
            detail = otp_step.verify(page, fast, account_email="a@b.com",
                                     mailbox_password="pw")

        assert "verified" in detail, "no banner means it must not hard-fail here"
        assert "Continue is still disabled" in caplog.text
        assert "did not accept this code" in caplog.text
    finally:
        page.close()
