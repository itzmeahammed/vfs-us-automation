"""Parsing real mail, and surviving the ways a mailbox misbehaves.

The IMAP conversation itself is stubbed — what matters here is everything
around it: turning a raw RFC822 message into the plain value the matcher takes,
and the failure handling that decides whether one bad mailbox costs the whole
pass.

The MIME cases are not hypothetical. VFS sends HTML-only mail to some portals,
mail arrives hard-wrapped and quoted-printable, and headers come RFC 2047
encoded. Each of those silently produces an empty body if handled wrongly — and
an empty body matches nothing, which looks exactly like "no invitation arrived".
"""

import pytest

from src.inbox.matcher import INVITATION, Email
from src.inbox.watcher import (
    Observation,
    PassResult,
    _decode_header,
    _parse_message,
    _strip_html,
    fetch_mailbox,
)


def raw(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

def test_a_plain_text_message_parses():
    message = _parse_message(raw(
        "From: VFS <info@vfshelpline.com>\n"
        "Subject: Slots available for booking an appointment\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "Dear IRINA KONOVALOVA,\n"
    ), uid="7", mailbox="m", received=1756000000.0)

    assert message.subject == "Slots available for booking an appointment"
    assert "info@vfshelpline.com" in message.sender
    assert "IRINA KONOVALOVA" in message.body
    assert message.uid == "7"
    assert message.received_epoch == 1756000000.0


def test_an_html_only_message_falls_back_to_stripped_text():
    """VFS sends HTML-only mail to some portals. Without the fallback the body
    is empty and the matcher can only see the subject."""
    message = _parse_message(raw(
        "Subject: Successfully Added to Waitlist\n"
        "Content-Type: text/html; charset=utf-8\n"
        "\n"
        "<html><body><p>Dear IRINA KONOVALOVA,</p>"
        "<p>Your Unique Reference Number is <b>ITD125298020335</b>.</p>"
        "</body></html>\n"
    ), uid="1", mailbox="m", received=0.0)

    assert "IRINA KONOVALOVA" in message.body
    assert "ITD125298020335" in message.body


def test_multipart_alternative_prefers_the_plain_part():
    message = _parse_message(raw(
        "Subject: Test\n"
        'Content-Type: multipart/alternative; boundary="B"\n'
        "\n"
        "--B\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "PLAIN VERSION\n"
        "--B\n"
        "Content-Type: text/html; charset=utf-8\n"
        "\n"
        "<p>HTML VERSION</p>\n"
        "--B--\n"
    ), uid="1", mailbox="m", received=0.0)

    assert "PLAIN VERSION" in message.body
    assert "HTML VERSION" not in message.body


def test_quoted_printable_is_decoded():
    """Real mail encodes non-ASCII and long lines this way; undecoded, a name
    reads as 'KONOV=C3=81LOV=C3=81' and matches nothing."""
    message = _parse_message(raw(
        "Subject: Test\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: quoted-printable\n"
        "\n"
        "Dear IRINA KONOVALOVA=2C\n"
    ), uid="1", mailbox="m", received=0.0)

    assert "IRINA KONOVALOVA," in message.body


def test_an_rfc2047_encoded_subject_is_decoded():
    message = _parse_message(raw(
        "Subject: =?utf-8?B?U2xvdHMgYXZhaWxhYmxl?=\n"
        "Content-Type: text/plain\n"
        "\n"
        "body\n"
    ), uid="1", mailbox="m", received=0.0)

    assert message.subject == "Slots available"


def test_an_unknown_charset_does_not_lose_the_message():
    message = _parse_message(raw(
        "Subject: Test\n"
        "Content-Type: text/plain; charset=definitely-not-a-charset\n"
        "\n"
        "Dear IRINA,\n"
    ), uid="1", mailbox="m", received=0.0)
    assert isinstance(message.body, str)


def test_a_message_with_no_body_parts_still_yields_an_email():
    message = _parse_message(raw("Subject: Empty\n\n"), uid="1", mailbox="m", received=0.0)
    assert message.subject == "Empty"
    assert message.body == ""


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>Hello</p>", "Hello"),
        ("<script>var x=1;</script><p>Hi</p>", "Hi"),
        ("<style>p{color:red}</style><p>Hi</p>", "Hi"),
        ("A&nbsp;B", "A B"),
        ("A &amp; B", "A & B"),
        ("&lt;tag&gt;", "<tag>"),
        ("It&#39;s", "It's"),
        ("", ""),
    ],
)
def test_html_stripping(html, expected):
    assert _strip_html(html) == expected


def test_script_contents_never_leak_into_the_body():
    """Script text is never prose, and letting it through would let a keyword
    inside JavaScript trip a matcher."""
    stripped = _strip_html("<script>var slots='Slots available for booking';</script><p>Hi</p>")
    assert "Slots available" not in stripped


def test_decode_header_handles_none_and_plain():
    assert _decode_header(None) == ""
    assert _decode_header("Plain Subject") == "Plain Subject"


# --------------------------------------------------------------------------- #
# Observation — the 48-hour clock                                              #
# --------------------------------------------------------------------------- #

def observation(hours=48, received=1756000000.0, classification=INVITATION):
    from src.inbox.matcher import Match

    return Observation(
        email=Email(subject="s", received_epoch=received),
        match=Match(classification=classification, route="AE-ITA", validity_hours=hours),
    )


def test_expiry_is_measured_from_the_servers_timestamp(monkeypatch):
    """From INTERNALDATE, never from now — a watcher that was down for a day
    must not silently extend a 48-hour deadline."""
    received = 1756000000.0
    obs = observation(hours=48, received=received)
    assert obs.expires_at() == received + 48 * 3600


def test_hours_left_goes_negative_once_the_window_has_closed(monkeypatch):
    import src.inbox.watcher as watcher_mod

    received = 1756000000.0
    monkeypatch.setattr(watcher_mod.time, "time", lambda: received + 50 * 3600)
    assert observation(hours=48, received=received).hours_left() < 0


def test_a_confirmation_has_no_expiry():
    from src.inbox.matcher import CONFIRMATION

    assert observation(classification=CONFIRMATION).expires_at() is None


def test_an_invitation_without_a_timestamp_has_no_expiry():
    """Better to report no deadline than to invent one from a missing date."""
    assert observation(received=0.0).expires_at() is None


def test_an_invitation_without_validity_hours_has_no_expiry():
    assert observation(hours=None).expires_at() is None


# --------------------------------------------------------------------------- #
# PassResult                                                                   #
# --------------------------------------------------------------------------- #

def test_pass_result_partitions_by_classification():
    from src.inbox.matcher import CONFIRMATION, Match

    invite = observation()
    confirm = Observation(email=Email(), match=Match(classification=CONFIRMATION))
    result = PassResult(observations=[invite, confirm])

    assert result.invitations() == [invite]
    assert result.confirmations() == [confirm]


def test_an_empty_pass_partitions_cleanly():
    result = PassResult()
    assert result.invitations() == []
    assert result.confirmations() == []


# --------------------------------------------------------------------------- #
# fetch_mailbox — failure handling                                             #
# --------------------------------------------------------------------------- #

class FakeIMAP:
    """A stub IMAP server. Only what fetch_mailbox actually calls."""

    def __init__(self, messages, uidvalidity="1", fail_on=()):
        self.messages = messages            # {uid: raw bytes}
        self.uidvalidity = uidvalidity
        self.fail_on = set(fail_on)
        self.logged_out = False
        self.readonly = None

    def login(self, user, password):
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def response(self, key):
        return key, [self.uidvalidity.encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(u.encode() for u in sorted(self.messages, key=int))]
        if command == "FETCH":
            uid = args[0]
            if uid in self.fail_on:
                raise RuntimeError("simulated fetch failure")
            if "INTERNALDATE" in args[1]:
                return "OK", [b'1 (INTERNALDATE "01-Aug-2026 10:00:00 +0000")']
            return "OK", [(b"1", self.messages[uid])]
        return "NO", []

    def logout(self):
        self.logged_out = True


INVITE_RAW = raw(
    "From: VFS <info.italyuae@vfshelpline.com>\n"
    "Subject: Slots available for booking an appointment\n"
    "Content-Type: text/plain\n"
    "\n"
    "Dear IRINA KONOVALOVA,\n"
    "Appointment slots ... are now available for booking.\n"
)

MATCHERS = {"AE-ITA": [{
    "name": "invite", "classify": INVITATION,
    "subject_contains": ["Slots available for booking"],
    "extract": {"applicant_name": r"Dear\s+([A-Za-z][A-Za-z\s'.-]+?)\s*,"},
    "validity_hours": 48,
}]}


@pytest.fixture
def fake_imap(monkeypatch):
    import src.inbox.watcher as watcher_mod

    holder = {}

    def install(imap):
        holder["imap"] = imap
        monkeypatch.setattr(watcher_mod.imaplib, "IMAP4_SSL", lambda *a, **k: imap)
        return imap

    return install


def test_a_matching_message_becomes_an_observation(fake_imap):
    from src.inbox.seen import SeenState

    fake_imap(FakeIMAP({"1": INVITE_RAW}))
    observations, examined = fetch_mailbox(
        "h", 993, "u@e.com", "p", SeenState(), MATCHERS, "u***@e.com"
    )

    assert examined == 1
    assert len(observations) == 1
    assert observations[0].match.is_invitation
    assert observations[0].match.get("applicant_name") == "IRINA KONOVALOVA"
    assert observations[0].account == "u***@e.com"


def test_the_mailbox_is_opened_read_only(fake_imap):
    """Never mark VFS's mail as seen: a human reading the same mailbox must find
    it exactly as VFS left it."""
    from src.inbox.seen import SeenState

    imap = fake_imap(FakeIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", SeenState(), MATCHERS)
    assert imap.readonly is True


def test_non_matching_mail_is_counted_but_not_observed(fake_imap):
    """An account mailbox also holds ordinary mail that is none of our business."""
    from src.inbox.seen import SeenState

    fake_imap(FakeIMAP({"1": raw("Subject: Lunch?\nContent-Type: text/plain\n\nhi\n")}))
    observations, examined = fetch_mailbox(
        "h", 993, "u@e.com", "p", SeenState(), MATCHERS
    )
    assert examined == 1
    assert observations == []


def test_a_message_is_not_reprocessed_on_the_next_pass(fake_imap):
    from src.inbox.seen import SeenState

    state = SeenState()
    fake_imap(FakeIMAP({"1": INVITE_RAW}))

    first, _ = fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    second, examined = fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)

    assert len(first) == 1
    assert second == [] and examined == 0


def test_an_unfetchable_message_is_marked_so_it_cannot_loop(fake_imap):
    """A message that will not parse now will not parse next pass either;
    retrying it forever would wedge the mailbox."""
    from src.inbox.seen import SeenState

    state = SeenState()
    fake_imap(FakeIMAP({"1": INVITE_RAW}, fail_on={"1"}))

    observations, _ = fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    assert observations == []
    assert state.is_seen("u@e.com", "1")


def test_one_bad_message_does_not_abort_the_mailbox(fake_imap):
    from src.inbox.seen import SeenState

    fake_imap(FakeIMAP({"1": INVITE_RAW, "2": INVITE_RAW}, fail_on={"1"}))
    observations, _ = fetch_mailbox(
        "h", 993, "u@e.com", "p", SeenState(), MATCHERS
    )
    assert len(observations) == 1, "the second message must still be read"


def test_the_connection_is_always_logged_out(fake_imap):
    from src.inbox.seen import SeenState

    imap = fake_imap(FakeIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", SeenState(), MATCHERS)
    assert imap.logged_out


def test_a_connect_failure_raises_so_the_caller_can_report_it(monkeypatch):
    """run_pass catches this per-mailbox: one bad password must not blind the
    watcher to every other account."""
    import src.inbox.watcher as watcher_mod
    from src.inbox.seen import SeenState

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(watcher_mod.imaplib, "IMAP4_SSL", boom)
    with pytest.raises(OSError):
        fetch_mailbox("h", 993, "u@e.com", "p", SeenState(), MATCHERS)


def test_a_uidvalidity_change_causes_a_reread(fake_imap):
    from src.inbox.seen import SeenState

    state = SeenState()
    fake_imap(FakeIMAP({"1": INVITE_RAW}, uidvalidity="111"))
    first, _ = fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    assert len(first) == 1

    fake_imap(FakeIMAP({"1": INVITE_RAW}, uidvalidity="222"))
    second, _ = fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    assert len(second) == 1, "renumbered mailbox must be re-read, not skipped"


def test_the_per_pass_cap_is_honoured(fake_imap, monkeypatch):
    """A runaway mailbox must not turn one pass into an unbounded download."""
    import src.inbox.watcher as watcher_mod
    from src.inbox.seen import SeenState

    monkeypatch.setattr(watcher_mod, "_tunables", lambda: (30, 3, 300))
    fake_imap(FakeIMAP({str(i): INVITE_RAW for i in range(1, 11)}))

    observations, examined = fetch_mailbox(
        "h", 993, "u@e.com", "p", SeenState(), MATCHERS
    )
    assert examined == 3, "only the newest 3 should be fetched this pass"


# --------------------------------------------------------------------------- #
# run_pass — orchestration across accounts                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def pass_env(monkeypatch):
    """Wires run_pass to fakes: IMAP settings, matchers, accounts, mailbox reads."""
    import src.inbox.config as config_mod
    import src.inbox.watcher as watcher_mod

    def install(accounts, fetch=None, host="imap.example.com", matchers=None):
        monkeypatch.setattr(watcher_mod, "_imap_settings", lambda: (host, 993))
        monkeypatch.setattr(
            config_mod, "all_matchers",
            lambda: matchers if matchers is not None else MATCHERS,
        )
        monkeypatch.setattr(watcher_mod, "mailbox_accounts", lambda: accounts)
        if fetch is not None:
            monkeypatch.setattr(watcher_mod, "fetch_mailbox", fetch)
        return watcher_mod

    return install


def test_run_pass_aggregates_across_mailboxes(pass_env):
    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    def fetch(host, port, user, password, state, matchers, label=""):
        return [observation()], 3

    pass_env([("a@e.com", "p"), ("b@e.com", "p")], fetch=fetch)
    result = run_pass(SeenState())

    assert result.mailboxes_checked == 2
    assert result.messages_seen == 6
    assert len(result.observations) == 2


def test_one_failing_mailbox_does_not_stop_the_others(pass_env):
    """One bad password must not blind the watcher to every other account."""
    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    def fetch(host, port, user, password, state, matchers, label=""):
        if user == "bad@e.com":
            raise OSError("authentication failed")
        return [observation()], 1

    pass_env([("bad@e.com", "p"), ("good@e.com", "p")], fetch=fetch)
    result = run_pass(SeenState())

    assert result.mailboxes_checked == 1
    assert len(result.mailboxes_failed) == 1
    assert len(result.observations) == 1, "the good mailbox must still be read"


def test_run_pass_stops_early_without_an_imap_host(pass_env, caplog):
    """The current state of this repo: [otp] imap_host is blank. That must be an
    explicit error, not a silent empty pass that reads as 'no mail'."""
    import logging

    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    pass_env([("a@e.com", "p")], host="")
    with caplog.at_level(logging.ERROR):
        result = run_pass(SeenState())

    assert result.mailboxes_checked == 0
    assert any("IMAP host" in r.message for r in caplog.records)


def test_run_pass_stops_early_without_any_matchers(pass_env, caplog):
    import logging

    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    pass_env([("a@e.com", "p")], matchers={})
    with caplog.at_level(logging.ERROR):
        result = run_pass(SeenState())

    assert result.mailboxes_checked == 0
    assert any("config/inbox" in r.message for r in caplog.records)


def test_run_pass_warns_when_there_are_no_accounts(pass_env, caplog):
    import logging

    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    pass_env([])
    with caplog.at_level(logging.WARNING):
        result = run_pass(SeenState())

    assert result.mailboxes_checked == 0
    assert any("No waitlist accounts" in r.message for r in caplog.records)


def test_state_is_saved_after_each_mailbox_not_only_at_the_end(pass_env, monkeypatch):
    """A crash part-way through must not re-report the mailboxes already done."""
    import src.inbox.seen as seen_mod
    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    saves = []
    monkeypatch.setattr(seen_mod, "save", lambda s: saves.append(1))

    def fetch(host, port, user, password, state, matchers, label=""):
        return [], 0

    pass_env([("a@e.com", "p"), ("b@e.com", "p")], fetch=fetch)
    run_pass(SeenState())

    assert len(saves) == 2


def test_state_is_saved_even_when_a_mailbox_raises(pass_env, monkeypatch):
    import src.inbox.seen as seen_mod
    from src.inbox.seen import SeenState
    from src.inbox.watcher import run_pass

    saves = []
    monkeypatch.setattr(seen_mod, "save", lambda s: saves.append(1))

    def fetch(host, port, user, password, state, matchers, label=""):
        raise OSError("boom")

    pass_env([("a@e.com", "p")], fetch=fetch)
    run_pass(SeenState())

    assert len(saves) == 1


def test_watch_once_returns_the_pass_result(pass_env, monkeypatch):
    import src.inbox.report as report_mod
    from src.inbox.watcher import watch

    def fetch(host, port, user, password, state, matchers, label=""):
        return [observation()], 1

    monkeypatch.setattr(report_mod, "report", lambda r: None)
    pass_env([("a@e.com", "p")], fetch=fetch)

    result = watch(poll_seconds=1, once=True)
    assert len(result.observations) == 1


def test_watch_survives_an_unexpected_error_in_a_pass(pass_env, monkeypatch):
    """The watcher is long-lived; an unexpected error must not end it."""
    import src.inbox.report as report_mod
    import src.inbox.watcher as watcher_mod
    from src.inbox.watcher import watch

    monkeypatch.setattr(report_mod, "report", lambda r: None)
    monkeypatch.setattr(watcher_mod, "run_pass",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = watch(poll_seconds=1, once=True)   # must not raise
    assert result.observations == []


# --------------------------------------------------------------------------- #
# Incremental scanning (added 2026-09-02)                                      #
# --------------------------------------------------------------------------- #
# Measured effect on the real mailboxes: a fresh pass had the server offer 312
# messages and took ~3 minutes; the next pass offered 30 and took ~6 seconds.

class RecordingIMAP(FakeIMAP):
    """FakeIMAP that remembers the SEARCH criteria it was given."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.criteria = []

    def uid(self, command, *args):
        if command == "SEARCH":
            self.criteria.append(args[1] if len(args) > 1 else args[0])
        return super().uid(command, *args)


def test_a_first_pass_is_bounded_by_date_not_unbounded(fake_imap):
    """A mailbox with years of history must not be downloaded whole on day one."""
    from src.inbox.seen import SeenState

    imap = fake_imap(RecordingIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", SeenState(), MATCHERS)
    assert "SINCE" in imap.criteria[0]


def test_a_later_pass_searches_only_since_the_last_one(fake_imap):
    """THE INCREMENTAL WIN. Without this every pass re-lists the whole mailbox."""
    from src.inbox.seen import SeenState

    state = SeenState()
    fake_imap(RecordingIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    assert state.last_pass("u@e.com") > 0

    imap = fake_imap(RecordingIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)
    assert "SINCE" in imap.criteria[0]


def test_a_failed_pass_does_not_advance_the_window(fake_imap):
    """Otherwise the next pass would skip the window this one failed to cover."""
    from src.inbox.seen import SeenState

    state = SeenState()
    fake_imap(FakeIMAP({"1": INVITE_RAW}, fail_on={"1"}))

    class Exploding(FakeIMAP):
        def select(self, mailbox, readonly=False):
            raise RuntimeError("mailbox unavailable")

    fake_imap(Exploding({}))
    with pytest.raises(RuntimeError):
        fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)

    assert state.last_pass("u@e.com") == 0.0


def test_the_search_date_is_backed_off_by_a_day(fake_imap):
    """SINCE has day granularity and the mail server may be in another timezone,
    so a pass must never query right up to its own edge."""
    import time

    from src.inbox.seen import SeenState

    state = SeenState()
    state.record_pass("u@e.com", time.time())
    imap = fake_imap(RecordingIMAP({"1": INVITE_RAW}))
    fetch_mailbox("h", 993, "u@e.com", "p", state, MATCHERS)

    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
    assert yesterday in imap.criteria[0]


# --------------------------------------------------------------------------- #
# Explicit mailboxes — [inbox] mailboxes                                       #
# --------------------------------------------------------------------------- #
# Client files only cover accounts somebody is registered under. A mailbox worth
# watching may have no client at all — a shared inbox VFS mail is forwarded to,
# or an account being trialled. Without this there is no way to watch one short
# of inventing a fake client file.

@pytest.fixture
def inbox_config_value(monkeypatch):
    """Stubs [inbox] mailboxes without touching the real config."""
    import src.utils.config_reader as reader

    def install(value):
        real = reader.get_config_value

        def fake(section, key, default=""):
            if section == "inbox" and key == "mailboxes":
                return value
            return real(section, key, default)

        monkeypatch.setattr(reader, "get_config_value", fake)
        monkeypatch.setattr(reader, "initialize_config", lambda: None)

    return install


def test_an_explicit_mailbox_is_parsed(inbox_config_value):
    from src.inbox.watcher import _extra_mailboxes

    inbox_config_value("someone@example.com:secret")
    assert _extra_mailboxes() == {"someone@example.com": "secret"}


def test_several_mailboxes_are_comma_separated(inbox_config_value):
    from src.inbox.watcher import _extra_mailboxes

    inbox_config_value("a@x.com:one, b@x.com:two")
    assert _extra_mailboxes() == {"a@x.com": "one", "b@x.com": "two"}


def test_no_configured_mailboxes_yields_nothing(inbox_config_value):
    from src.inbox.watcher import _extra_mailboxes

    inbox_config_value("")
    assert _extra_mailboxes() == {}


@pytest.mark.parametrize("bad", ["notanentry", "missing-password:", ":nopassword"])
def test_a_malformed_entry_is_skipped_not_fatal(inbox_config_value, bad, caplog):
    """One bad entry must not stop every other mailbox being watched."""
    import logging

    from src.inbox.watcher import _extra_mailboxes

    inbox_config_value(f"{bad}, good@x.com:pw")
    with caplog.at_level(logging.WARNING):
        found = _extra_mailboxes()

    assert found == {"good@x.com": "pw"}
    assert any("malformed" in r.message for r in caplog.records)


def test_a_password_containing_a_colon_survives(inbox_config_value):
    """partition() splits on the FIRST colon, so the rest stays intact."""
    from src.inbox.watcher import _extra_mailboxes

    inbox_config_value("a@x.com:pa:ss:word")
    assert _extra_mailboxes() == {"a@x.com": "pa:ss:word"}


def test_an_explicit_mailbox_wins_over_a_client_file(monkeypatch, inbox_config_value):
    """Naming a mailbox and its password in config means THAT password."""
    import src.inbox.watcher as watcher_mod
    from src.inbox.watcher import mailbox_accounts

    inbox_config_value("shared@x.com:from-config")

    class FakeClient:
        id = "c1"

    class FakeAccount:
        email = "shared@x.com"
        password = "from-client-file"

    from src.waitlist import accounts as acc
    from src.waitlist import registrant as reg

    monkeypatch.setattr(reg, "load_all", lambda skip_invalid=True: [FakeClient()])
    monkeypatch.setattr(acc, "resolve", lambda c: FakeAccount())

    assert dict(mailbox_accounts())["shared@x.com"] == "from-config"


def test_client_mailboxes_are_still_included(monkeypatch, inbox_config_value):
    from src.inbox.watcher import mailbox_accounts

    inbox_config_value("extra@x.com:pw")

    class FakeClient:
        id = "c1"

    class FakeAccount:
        email = "client@x.com"
        password = "pw2"

    from src.waitlist import accounts as acc
    from src.waitlist import registrant as reg

    monkeypatch.setattr(reg, "load_all", lambda skip_invalid=True: [FakeClient()])
    monkeypatch.setattr(acc, "resolve", lambda c: FakeAccount())

    found = dict(mailbox_accounts())
    assert set(found) == {"extra@x.com", "client@x.com"}
