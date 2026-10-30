"""`?q=` free-text search, and the disclosure boundary it must not cross.

WHY THIS FILE EXISTS
--------------------
The console had no search at all: every list was dropdown filters over 25-row
pages, so finding one applicant among hundreds meant paging. `q` fixes that,
and it is exactly the sort of convenience that turns into a disclosure bug if
written the obvious way.

The obvious way is "match the needle against every value in the record". But
`GET /clients/{id}` MASKS passport number, date of birth, email and phone
(A1****67) specifically so the dashboard never receives them. A `q` that
matched the unmasked value would let any caller confirm a guess one request at
a time — ?q=A1234567 returning a row says "yes, that is the passport". That is
worse than no masking, because the masking is still there looking like it
works.

So `src/api/core/search.py` forces every call site to name its searchable
fields, under one rule: a field may be searchable only if the SAME response
already returns it. These tests pin that rule on all three endpoints, because
it is invisible at the call site — nothing crashes if someone adds
"passport_number" to a `_SEARCHABLE` tuple.

They also pin the cheap half: `q` is applied BEFORE the per-client pre-flight,
so searching a large fleet costs a string match per client and not a full
validation per client.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# setdefault, NOT assignment — the token is process-wide and another test file
# may already have captured it into its own HEADERS.
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "c" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.api.core import search  # noqa: E402
from src.api.modules.clients import service  # noqa: E402
from src.waitlist import store  # noqa: E402

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

PASSPORT = "A12945678"
EMAIL = "ahmed.khan@example.com"
PHONE = "556024553"
DOB = "1990-04-12"

CLIENT = {
    "route": "AE-CZE",
    "combos": ["Dubai - Tourism"],
    "enabled": False,
    "first_name": "AHMED",
    "last_name": "KHAN",
    "gender": "Male",
    "nationality": "INDIA",
    "passport_number": PASSPORT,
    "passport_expiry": "2030-07-05",
    "date_of_birth": DOB,
    "phone_country_code": "971",
    "phone_number": PHONE,
    "email": EMAIL,
    "account": "mailbox@example.com",
    "account_password": "p",
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Three waitlist clients in a temp dir, so the assertions are exact.

    Patched by dotted path (not on the imported module object) so the reader
    and the writer both follow — same approach as test_api_clients.py.
    """
    monkeypatch.setattr("src.waitlist.registrant.REGISTRANT_DIR",
                        str(tmp_path), raising=False)
    monkeypatch.setattr("src.waitlist.store.REGISTRANT_DIR",
                        str(tmp_path), raising=False)
    monkeypatch.setattr("src.waitlist.store.path_for",
                        lambda rid: str(tmp_path / f"{rid}.json"),
                        raising=False)

    store.create("zz-ahmed-cze", dict(CLIENT))
    store.create("zz-ahmed-deu", dict(CLIENT, route="AE-DEU",
                                      combos=["Dubai - Germany"]))
    store.create("zz-sara-cze", dict(CLIENT, first_name="SARA",
                                     last_name="ALI",
                                     passport_number="B76543210"))
    return ["zz-ahmed-cze", "zz-ahmed-deu", "zz-sara-cze"]


def ids(rows):
    return sorted(r["client_id"] for r in rows)


def listing(**kw):
    """Waitlist-only: the live store points at real files this test cannot own."""
    return service.list_clients(flow="waitlist", **kw)


# ── The disclosure boundary — the reason search.py exists ───────────────────


@pytest.mark.parametrize("secret, label", [
    (PASSPORT, "passport number"),
    (EMAIL, "email address"),
    (PHONE, "phone number"),
    (DOB, "date of birth"),
])
def test_q_cannot_find_a_client_by_a_masked_field(fleet, secret, label):
    """The whole point. These values exist on the record and must stay unfindable.

    If this fails, `?q=<guess>` has become an oracle: a caller can confirm a
    passport number, email, phone or date of birth one request at a time,
    defeating the masking in `detail()` while leaving it apparently intact.
    """
    assert listing(q=secret) == [], (
        f"searching a client's {label} found it — q has reached a masked field"
    )


def test_a_masked_field_is_not_merely_mangled_by_casing(fleet):
    """Guards the lazy fix of lowercasing the needle and calling it safe."""
    assert listing(q=PASSPORT.lower()) == []
    assert listing(q=PASSPORT.upper()) == []


def test_the_searchable_tuple_names_no_masked_field():
    """A second lock, on the declaration rather than the behaviour.

    The behavioural tests above only cover the four fields this fixture sets.
    This one fails for ANY masked field someone adds later.
    """
    masked = {"passport_number", "passport_expiry", "date_of_birth", "email",
              "phone_number", "phone_country_code", "account",
              "account_password", "address_line_1", "address_line_2"}
    leaked = masked & set(service._SEARCHABLE)
    assert not leaked, (
        f"{sorted(leaked)} is searchable but masked in detail() — searching it "
        "turns GET /clients into a confirmation oracle"
    )


# ── What search SHOULD find ────────────────────────────────────────────────


def test_q_finds_a_client_by_name(fleet):
    assert ids(listing(q="ahmed")) == ["zz-ahmed-cze", "zz-ahmed-deu"]


def test_q_finds_a_client_by_id(fleet):
    assert ids(listing(q="zz-sara-cze")) == ["zz-sara-cze"]


def test_q_finds_a_client_by_route(fleet):
    assert ids(listing(q="ae-deu")) == ["zz-ahmed-deu"]


def test_q_finds_a_client_by_combo(fleet):
    assert ids(listing(q="tourism")) == ["zz-ahmed-cze", "zz-sara-cze"]


def test_q_is_case_insensitive(fleet):
    assert ids(listing(q="AHMED")) == ids(listing(q="ahmed"))


# ── All terms must match, each may match a different field ─────────────────


def test_every_term_must_match(fleet):
    """"ahmed deu" is one person on one route, not everyone called Ahmed."""
    assert ids(listing(q="ahmed deu")) == ["zz-ahmed-deu"]


def test_terms_may_match_different_fields(fleet):
    """How someone searches without knowing the field names: a name and a route."""
    assert ids(listing(q="khan tourism")) == ["zz-ahmed-cze"]


def test_a_term_that_matches_nothing_empties_the_result(fleet):
    assert listing(q="ahmed nosuchroute") == []


# ── An absent or blank q is not a filter ───────────────────────────────────


def test_no_q_returns_everyone(fleet):
    assert len(listing()) == 3


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_q_behaves_like_an_omitted_one(fleet, blank):
    """`?q=` must not read as "match nothing" and silently empty the console."""
    assert len(listing(q=blank)) == 3


# ── The cheap half: q runs before the pre-flight ───────────────────────────


def test_q_filters_before_running_the_preflight(fleet, monkeypatch):
    """Otherwise searching a 500-client fleet validates all 500 to show one.

    `include=problems` is the console's normal listing call, and the audit
    recorded it as the slow endpoint precisely because it pre-flights per row.
    """
    from src.waitlist import validate

    calls = []
    # Bound BEFORE patching: `validate.precheck_client` would resolve to the
    # replacement and recurse forever.
    original = validate.precheck_client

    def counted(cid, data):
        calls.append(cid)
        return original(cid, data)

    monkeypatch.setattr("src.waitlist.validate.precheck_client", counted)

    rows = listing(q="sara", include_problems=True)
    assert ids(rows) == ["zz-sara-cze"]
    assert calls == ["zz-sara-cze"], (
        f"pre-flighted {len(calls)} clients to return 1 — q must filter first"
    )


def test_a_matching_row_still_gets_its_problems(fleet):
    """Filtering early must not skip the work for the rows that survive."""
    row = listing(q="sara", include_problems=True)[0]
    assert isinstance(row["runnable"], bool)
    assert isinstance(row["problem_count"], int)


# ── The matcher itself ─────────────────────────────────────────────────────


def test_terms_splits_and_lowercases():
    assert search.terms("  Ahmed   AE-CHE ") == ["ahmed", "ae-che"]


def test_terms_is_empty_for_no_query():
    assert search.terms(None) == []
    assert search.terms("   ") == []


def test_terms_is_capped():
    """A 500-word "search" is not a search, and it costs a scan per term."""
    assert len(search.terms(" ".join(["x"] * 50))) == search.MAX_TERMS


def test_matches_with_no_terms_is_true():
    """So callers can pass the result of `terms()` through unconditionally."""
    assert search.matches({"a": "zzz"}, [], ("a",)) is True


def test_matches_ignores_fields_not_named():
    assert search.matches({"shown": "x", "secret": "hunter2"},
                          ["hunter2"], ("shown",)) is False


def test_matches_flattens_lists():
    assert search.matches({"combos": ["Dubai - SCHENGEN"]},
                          ["schengen"], ("combos",)) is True


def test_matches_reads_only_a_dicts_keys():
    """For `clients: {id: {...}}` the ids are the searchable part; the nested
    values are another record's fields and are not cleared for search."""
    row = {"clients": {"u10432-che": {"note": "hunter2"}}}
    assert search.matches(row, ["u10432"], ("clients",)) is True
    assert search.matches(row, ["hunter2"], ("clients",)) is False


def test_matches_skips_booleans():
    """Otherwise "true" matches half the fleet via enabled/test_mode."""
    assert search.matches({"enabled": True}, ["true"], ("enabled",)) is False


def test_matches_skips_none():
    assert search.matches({"vfs_reference": None}, ["none"],
                          ("vfs_reference",)) is False


# ── Jobs: the payload must not become a back door ──────────────────────────


def test_a_job_payload_does_not_leak_applicant_fields_into_search():
    """A live-flow job payload carries whatever the caller sent, which can
    include the applicant fields the clients endpoint masks. Only the few keys
    naming WHAT ran are lifted into the haystack."""
    from src.api.modules.jobs.router import _SEARCHABLE, _haystack

    row = {"job_id": "abc123", "status": "succeeded", "detail": "",
           "payload": {"registrant": "zz-ahmed-cze", "route": "AE-CZE",
                       "mode": "live", "passport_number": PASSPORT,
                       "email": EMAIL}}
    hay = _haystack(row)

    assert search.matches(hay, ["zz-ahmed-cze"], _SEARCHABLE) is True
    assert search.matches(hay, ["ae-cze"], _SEARCHABLE) is True
    assert search.matches(hay, [PASSPORT.lower()], _SEARCHABLE) is False, (
        "a job payload's passport number must not be searchable"
    )
    assert search.matches(hay, [EMAIL], _SEARCHABLE) is False


def test_a_job_payload_never_overwrites_the_jobs_own_field():
    """`payload.status` is the caller's word; the job's own status is the truth."""
    hay = __import__("src.api.modules.jobs.router", fromlist=["_haystack"])
    row = {"job_id": "a", "status": "failed", "payload": {"status": "succeeded"}}
    assert hay._haystack(row)["status"] == "failed"


# ── Invitations: the mailbox is shown, never searched ──────────────────────


def test_invitation_search_excludes_the_mailbox():
    """`_invite_view` serves `account` in full so an operator can see which
    mailbox to sign in to. That is NOT a reason to make it searchable: showing
    an address on a row someone is already reading is not the same as letting
    a guessed address be confirmed by a hit."""
    from src.api.modules.booking.router import _INVITE_SEARCHABLE

    assert "account" not in _INVITE_SEARCHABLE
    assert "history" not in _INVITE_SEARCHABLE
    assert "progress" not in _INVITE_SEARCHABLE


def test_invitation_search_finds_a_matched_client_id():
    from src.api.modules.booking.router import _INVITE_SEARCHABLE

    invite = {"key": "k1", "applicant_name": "AHMED KHAN", "category": "Tourist",
              "route": "AE-CHE", "status": "manual",
              "clients": {"zz-ahmed-cze": {"status": "pending"}}}
    assert search.matches(invite, ["zz-ahmed-cze"], _INVITE_SEARCHABLE) is True
    assert search.matches(invite, ["ahmed", "manual"], _INVITE_SEARCHABLE) is True


# ── Over HTTP, end to end ──────────────────────────────────────────────────


@pytest.fixture
def api():
    import src.api.core.config as config_mod
    config_mod.get_settings.cache_clear()

    # The limiter is a per-process singleton: correct in production, but it
    # makes one test file look like a flood and leaks into later files.
    from src.api.core.security import _reset_rate_limiter
    _reset_rate_limiter()

    from fastapi.testclient import TestClient
    import src.api.main as main_mod
    return TestClient(main_mod.app, raise_server_exceptions=False)


def test_q_reaches_the_clients_endpoint(api):
    r = api.get("/v1/clients?q=zz-no-such-client-anywhere", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total"] == 0, "total must count the filtered set, not all"


def test_q_reaches_the_jobs_endpoint(api):
    r = api.get("/v1/jobs?q=zz-no-such-job-anywhere", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_q_reaches_the_invitations_endpoint(api):
    r = api.get("/v1/booking/invitations?q=zz-no-such-invite", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.parametrize("path", [
    "/v1/clients", "/v1/jobs", "/v1/booking/invitations",
])
def test_an_overlong_q_is_refused_not_scanned(api, path):
    """A 10 KB needle is a scan per row per term; the cap is the cheap defence."""
    r = api.get(f"{path}?q={'x' * 500}", headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.parametrize("path", [
    "/v1/clients", "/v1/jobs", "/v1/booking/invitations",
])
def test_search_still_needs_a_token(api, path):
    assert api.get(f"{path}?q=ahmed").status_code == 401
