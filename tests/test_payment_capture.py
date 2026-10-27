"""What a payment capture leaves on disk.

The screenshot is the artifact: past the committing click the appointment is
booked, so if the charge then fails that PNG is the only record of what was on
screen and the slot cannot be reproduced. The DOM is not, and is off by
default — it is the markup of a page with a card on it, and one AE-HUN 3-D
Secure run wrote 120 HTML files and 5.3 MB for 11 screenshots.
"""

import glob
import os

import pytest

from src.payment import gateway


class _Frame:
    def __init__(self, url, html):
        self.url = url
        self._html = html

    def content(self):
        return self._html


class _Page:
    """A gateway page with the card form in the parent and OTP in a frame."""

    def __init__(self):
        self.frames = [
            _Frame("https://online.vfsglobal.com/pay",
                   '<input name="card" value="4111111111111111">'),
            _Frame("https://up.cybersource.com/otp",
                   '<input name="otp" value="884422">'),
            _Frame("about:blank", "<html></html>"),
        ]

    def screenshot(self, path=None, **kwargs):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"png")


def _counts(directory):
    return (len(glob.glob(os.path.join(directory, "*.png"))),
            len(glob.glob(os.path.join(directory, "*.html"))))


def test_the_dom_is_not_written_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "log", gateway.log)
    out = str(tmp_path / "payment")

    written = gateway.capture_screen(_Page(), out, "submitted", dom=False)

    assert _counts(out) == (1, 0), "a card page's DOM was written to disk"
    assert len(written) == 1


def test_the_screenshot_is_written_even_with_the_dom_off(tmp_path):
    """The one artifact the payment path always keeps."""
    out = str(tmp_path / "payment")

    gateway.capture_screen(_Page(), out, "before_commit", dom=False)

    assert _counts(out)[0] == 1


def test_the_dom_is_written_when_explicitly_asked(tmp_path):
    """Mapping a new gateway still needs the frames."""
    out = str(tmp_path / "payment")

    gateway.capture_screen(_Page(), out, "submitted", dom=True)

    pngs, htmls = _counts(out)
    assert pngs == 1
    assert htmls == 2, "about:blank should be skipped, the other two kept"


def test_the_card_number_is_scrubbed_when_the_dom_is_written(tmp_path):
    out = str(tmp_path / "payment")

    gateway.capture_screen(_Page(), out, "submitted",
                           scrub=["4111111111111111", "884422"], dom=True)

    body = "".join(open(f, encoding="utf-8").read()
                   for f in glob.glob(os.path.join(out, "*.html")))
    assert "4111111111111111" not in body
    assert "884422" not in body


@pytest.mark.parametrize("configured, expected_html", [(False, 0), (True, 2)])
def test_the_setting_decides_when_no_override_is_given(tmp_path, monkeypatch,
                                                       configured,
                                                       expected_html):
    out = str(tmp_path / "payment")

    class _Booking:
        payment_dom_capture = configured

    class _Settings:
        booking = _Booking()

    monkeypatch.setattr("src.settings.settings", lambda: _Settings())

    gateway.capture_screen(_Page(), out, "submitted")

    assert _counts(out)[1] == expected_html


def test_unreadable_settings_never_write_a_card_dom(tmp_path, monkeypatch):
    """Failing closed is the only safe direction: a missing setting must not
    be the reason a card page's markup lands on disk — but it must also not be
    the reason the payment goes uncaptured."""
    out = str(tmp_path / "payment")

    def _boom():
        raise RuntimeError("settings are broken")

    monkeypatch.setattr("src.settings.settings", _boom)

    gateway.capture_screen(_Page(), out, "submitted")

    assert _counts(out) == (1, 0)
