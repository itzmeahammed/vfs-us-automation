"""The Relayion client: the API contract, as assumptions written down.

Every test here encodes something this code BELIEVES about an API it does not
own — the `{success, data}` envelope, the field names, the timestamp format.
When Relayion changes one of them, these fail with a clear name instead of the
payment path quietly finding no codes forever.

The shapes come from sms_inbox/server.py and sms_inbox/index.html, which are a
working console against this API rather than a guess at it, and were confirmed
against the live endpoint.
"""

import json
from datetime import datetime, timezone

import pytest

from src.otp_relay import relayion
from src.otp_relay.errors import OtpNotConfigured, OtpRelayUnavailable
from src.otp_relay.relayion import Message


# --------------------------------------------------------------------------- #
# Timestamps                                                                   #
# --------------------------------------------------------------------------- #

def test_a_zulu_timestamp_parses_to_aware_utc():
    """AWARE, ALWAYS. A naive datetime compares unequally against the armed
    anchor and would silently reject every code — a failure indistinguishable
    from 'no SMS arrived'."""
    parsed = relayion._parse_time("2026-10-14T13:40:07.000Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 10, 14, 13, 40, 7, tzinfo=timezone.utc)


def test_an_offset_timestamp_is_normalised_to_utc():
    parsed = relayion._parse_time("2026-10-14T17:40:07+04:00")
    assert parsed == datetime(2026, 10, 14, 13, 40, 7, tzinfo=timezone.utc)


def test_a_naive_timestamp_is_assumed_utc_not_local():
    parsed = relayion._parse_time("2026-10-14T13:40:07")
    assert parsed == datetime(2026, 10, 14, 13, 40, 7, tzinfo=timezone.utc)


def test_a_junk_timestamp_is_none_rather_than_an_exception():
    assert relayion._parse_time("not a date") is None
    assert relayion._parse_time("") is None
    assert relayion._parse_time(None) is None


# --------------------------------------------------------------------------- #
# Message mapping                                                              #
# --------------------------------------------------------------------------- #

def test_the_api_field_names_are_mapped():
    """THE CONTRACT. These exact names came off the live endpoint."""
    raw = {
        "id": "cbe4e066-6142-4bdc-9006-9ac25f3833cb",
        "senderNumber": "+971588226379",
        "body": "Your OTP is 483920",
        "receivedAt": "2026-10-14T13:40:07.000Z",
        "simSlotIndex": 0,
    }
    message = relayion._message(raw)
    assert message.id == "cbe4e066-6142-4bdc-9006-9ac25f3833cb"
    assert message.sender == "+971588226379"
    assert message.body == "Your OTP is 483920"
    assert message.received_at.year == 2026
    assert message.sim_slot == "0"


def test_created_at_is_the_fallback_when_received_at_is_absent():
    message = relayion._message({"id": "x", "createdAt":
                                 "2026-10-14T13:40:07Z"})
    assert message.received_at is not None


def test_a_message_with_missing_fields_does_not_raise():
    message = relayion._message({})
    assert message.id == ""
    assert message.body == ""
    assert message.received_at is None


def test_a_message_repr_does_not_contain_the_body():
    """The body IS the code. The default dataclass repr prints every field,
    and the first place that bites is a traceback being pasted into chat."""
    message = Message(id="m1", sender="BANK", body="Your OTP is 483920")
    assert "483920" not in repr(message)
    assert "483920" not in str(message)
    assert "m1" in repr(message)


# --------------------------------------------------------------------------- #
# The envelope                                                                 #
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload.encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload):
    monkeypatch.setattr(relayion.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(payload))


def test_the_data_envelope_is_unwrapped(monkeypatch):
    _patch_urlopen(monkeypatch, json.dumps({
        "success": True,
        "data": [{"id": "m1", "senderNumber": "BANK",
                  "body": "Your OTP is 483920",
                  "receivedAt": "2026-10-14T13:40:07Z"}],
    }))
    messages = relayion.list_inbound(key="rlyn_test")
    assert len(messages) == 1
    assert messages[0].id == "m1"


def test_success_false_is_an_error_carrying_the_api_message(monkeypatch):
    """Relayion answers HTTP 200 with success:false. Treating that as an empty
    inbox would make a bad key look like 'no SMS came'."""
    _patch_urlopen(monkeypatch, json.dumps({
        "success": False,
        "error": {"message": "Invalid API key", "code": "AUTH"},
    }))
    with pytest.raises(OtpRelayUnavailable, match="Invalid API key"):
        relayion.list_inbound(key="rlyn_wrong")


def test_a_non_json_body_is_an_error_not_a_crash(monkeypatch):
    _patch_urlopen(monkeypatch, "<html>502 Bad Gateway</html>")
    with pytest.raises(OtpRelayUnavailable):
        relayion.list_inbound(key="rlyn_test")


def test_data_that_is_not_a_list_yields_no_messages(monkeypatch):
    _patch_urlopen(monkeypatch, json.dumps({"success": True, "data": None}))
    assert relayion.list_inbound(key="rlyn_test") == []


def test_a_network_failure_is_an_error(monkeypatch):
    def boom(req, timeout=None):
        raise relayion.urllib.error.URLError("no route to host")

    monkeypatch.setattr(relayion.urllib.request, "urlopen", boom)
    with pytest.raises(OtpRelayUnavailable, match="Could not reach"):
        relayion.list_inbound(key="rlyn_test")


# --------------------------------------------------------------------------- #
# The query                                                                    #
# --------------------------------------------------------------------------- #

def test_since_and_sender_are_sent_as_the_api_expects(monkeypatch):
    """`from` and `senderNumber` are the parameter names the working console
    uses. A typo here means an unfiltered query that still 'works' in testing
    and quietly reads the whole inbox in production."""
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _FakeResponse(json.dumps({"success": True, "data": []}))

    monkeypatch.setattr(relayion.urllib.request, "urlopen", capture)
    relayion.list_inbound(since=datetime(2026, 10, 14, tzinfo=timezone.utc),
                          sender="BANK", key="rlyn_test")

    assert "sortBy=receivedAt" in seen["url"]
    assert "sortOrder=desc" in seen["url"]
    assert "from=2026-10-14" in seen["url"]
    assert "senderNumber=BANK" in seen["url"]
    assert seen["auth"] == "Bearer rlyn_test"


def test_the_key_falls_back_from_argument_to_env(monkeypatch):
    monkeypatch.setenv(relayion.ENV_KEY, "rlyn_from_env")
    assert relayion.api_key() == "rlyn_from_env"
    assert relayion.api_key("rlyn_explicit") == "rlyn_explicit"


def test_no_key_anywhere_raises_not_configured(monkeypatch):
    """An ordinary state on a developer machine, and the reason the payment
    path is allowed to fall back rather than fail."""
    monkeypatch.delenv(relayion.ENV_KEY, raising=False)
    monkeypatch.setattr(relayion, "CONFIG_SECTION", "no_such_section")
    with pytest.raises(OtpNotConfigured):
        relayion.api_key()
    assert relayion.is_configured() is False


# --------------------------------------------------------------------------- #
# Device readiness                                                             #
#                                                                              #
# The payload below is the REAL one, from the live endpoint on 2026-10-15.     #
# --------------------------------------------------------------------------- #

LIVE_DEVICE = {
    "id": "8a83105e-aa6d-47db-8643-64a0db724ec1",
    "name": "POCO C85",
    "lastSeenAt": "2026-10-15T09:25:21.103Z",
    "sims": [{"slotIndex": 0, "phoneNumber": "+971582771608",
              "label": None, "isDefault": True}],
    "status": "ONLINE",
    "isPaired": True,
    "pendingSetup": False,
}


def test_status_is_a_string_not_a_boolean():
    """*** THE REGRESSION THIS FILE EXISTS FOR. ***

    Relayion sends `"status": "ONLINE"`. An earlier cut read `isOnline` /
    `online`, got None for both, and reported a live paired handset as
    OFFLINE — a false negative on the single check an operator runs to decide
    whether the relay can be trusted before a payment. It cost a round trip
    of 'the relay is on' / 'the tool says it is not'.
    """
    assert relayion.is_online(LIVE_DEVICE) is True
    assert relayion.is_online({"status": "OFFLINE"}) is False
    assert relayion.is_online({"status": "offline"}) is False


def test_the_boolean_spellings_still_work_as_fallbacks():
    """Kept in case the field is ever added; `status` wins when both exist."""
    assert relayion.is_online({"isOnline": True}) is True
    assert relayion.is_online({"online": True}) is True
    assert relayion.is_online({"status": "ONLINE", "isOnline": False}) is True


def test_a_device_with_no_status_at_all_is_not_assumed_online():
    assert relayion.is_online({}) is False


def test_the_sim_numbers_are_extracted():
    """The readiness question in one list: the bank texts the number on the
    card, so unless it is here no code can arrive."""
    assert relayion.sim_numbers(LIVE_DEVICE) == ["+971582771608"]
    assert relayion.sim_numbers({}) == []
    assert relayion.sim_numbers({"sims": [{"slotIndex": 0}]}) == []


def test_distinct_senders_are_listed_for_the_diagnostic():
    messages = [Message(id="a", sender="BANK", body=""),
                Message(id="b", sender="BANK", body=""),
                Message(id="c", sender="OTHER", body=""),
                Message(id="d", sender="", body="")]
    assert relayion.senders_of(messages) == ["BANK", "OTHER"]
