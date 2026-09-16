"""Document upload — the missing half of "this route wants a file".

Some portals do not ask for typed applicant details at all: Italy wants the
passport bio page uploaded and OCRs the fields out of it. `readiness` reports
that as `{"kind": "file"}`, but until this endpoint existed there was no way to
GET a file in through the API — a web app could create an AE-ITA client that
could never register, and the failure only surfaced minutes into a browser run.

Two properties carry the weight:

  1. **Bytes are validated before they are stored.** The extension is a claim;
     documents.validate() sniffs magic bytes and rejects a .png that is really
     a PDF. A rejected upload leaves nothing on disk.
  2. **The 64 KB body cap does not apply here.** It is right for JSON and wrong
     for a 2 MB scan — a blanket cap rejected every real passport with a
     message naming the wrong limit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TOKEN = "t" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
PDF = b"%PDF-1.4" + b"\x00" * 400
AUTH = {"X-Webhook-Secret-Token": TOKEN}

CLIENT = {
    "client_id": "doc-test", "route": "AE-CHE",
    "combos": ["Dubai - SCHENGEN"],
    "account": "p@example.com", "account_password": "x" * 10,
    "first_name": "AVA", "last_name": "STONE", "nationality": "India",
    "passport_number": "Z9876543", "phone_country_code": "971",
    "phone_number": "501112233", "email": "ava@example.com",
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A client with an isolated document store — never the real one."""
    # Saved and restored explicitly at the end of this fixture rather than left
    # assigned: os.environ is process-wide, so leaking this token made later
    # modules (which capture their HEADERS at import time) send a stale one and
    # get 401s in a full run while passing when run alone.
    _saved_token = os.environ.get("VFSAPI_SECRET_TOKEN")
    os.environ["VFSAPI_SECRET_TOKEN"] = TOKEN
    monkeypatch.setenv("VFS_DOCUMENT_ROOT", str(tmp_path / "docs"))

    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()

    # The per-IP limiter is process-global and every test here shares one
    # client IP, so without this the later tests 429 instead of exercising what
    # they claim to. Reset per test rather than raising the limit — the limit
    # itself is worth keeping honest.
    from src.api.security import _reset_rate_limiter
    _reset_rate_limiter()

    from src.utils.config_reader import initialize_config
    initialize_config()
    from src.waitlist import documents
    monkeypatch.setattr(documents, "root", lambda: str(tmp_path / "docs"))

    import src.api.main as main_mod
    client = TestClient(main_mod.app, raise_server_exceptions=False)

    client.post("/clients", json=CLIENT, headers=AUTH)
    try:
        yield client
    finally:
        # These deletes still need THIS fixture's token, so the restore below
        # must come after them.
        client.delete("/clients/doc-test/documents", headers=AUTH)
        client.delete("/clients/doc-test", headers=AUTH)
        if _saved_token is None:
            os.environ.pop("VFSAPI_SECRET_TOKEN", None)
        else:
            os.environ["VFSAPI_SECRET_TOKEN"] = _saved_token
        # Restoring the env is not enough on its own — the cache still holds
        # settings built from TOKEN.
        config_mod.get_settings.cache_clear()


def _upload(api, data: bytes, filename: str = "passport.png"):
    return api.post("/clients/doc-test/documents", headers=AUTH,
                    files={"file": (filename, data, "application/octet-stream")})


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_a_valid_png_is_accepted(api):
    r = _upload(api, PNG)
    assert r.status_code == 201, r.text
    assert r.json()["stored"] is True


def test_the_upload_is_listed_afterwards(api):
    _upload(api, PNG)
    body = api.get("/clients/doc-test/documents", headers=AUTH).json()
    assert body["count"] == 1
    assert body["documents"][0]["kind"] == "passport_bio"


def test_the_stored_path_is_never_returned(api):
    """It is a location on the bot's disk; a caller has no use for it."""
    payload = str(_upload(api, PNG).json())
    assert "documents" not in payload.lower() or "\\" not in payload
    assert "path" not in _upload(api, PNG).json()


def test_documents_can_be_deleted(api):
    _upload(api, PNG)
    r = api.delete("/clients/doc-test/documents", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert api.get("/clients/doc-test/documents", headers=AUTH).json()["count"] == 0


# --------------------------------------------------------------------------
# The extension is a claim, not a check
# --------------------------------------------------------------------------


def test_pdf_bytes_named_png_are_rejected(api):
    """THE point of magic-byte sniffing."""
    r = _upload(api, PDF, "passport.png")
    assert r.status_code == 422
    assert "contents" in r.json()["detail"]


def test_an_unaccepted_extension_is_rejected(api):
    r = _upload(api, b"hello", "notes.txt")
    assert r.status_code == 422
    assert ".txt" in r.json()["detail"]


def test_an_empty_file_is_rejected(api):
    assert _upload(api, b"").status_code == 422


def test_a_rejected_upload_stores_nothing(api):
    """A failed validation must not leave a file behind."""
    _upload(api, PDF, "passport.png")
    assert api.get("/clients/doc-test/documents", headers=AUTH).json()["count"] == 0


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


def test_a_realistic_scan_is_not_blocked_by_the_json_body_cap(api):
    """Regression: the blanket 64 KB cap rejected every real passport scan.

    500 KB is an ordinary phone photo of a bio page. If this 413s, the
    middleware cap is being applied to uploads again.
    """
    r = _upload(api, b"\x89PNG\r\n\x1a\n" + b"\x00" * (500 * 1024))
    assert r.status_code == 201, "a realistic scan was rejected as too large"


def test_a_file_over_the_portal_limit_is_rejected(api):
    from src.waitlist import documents

    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (documents.MAX_BYTES + 1024)
    assert _upload(api, oversized).status_code == 413


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_uploading_for_an_unknown_client_is_refused(api):
    """Otherwise a typo'd id leaves an orphaned passport scan on disk."""
    r = api.post("/clients/nope/documents", headers=AUTH,
                 files={"file": ("p.png", PNG, "application/octet-stream")})
    assert r.status_code == 404


def test_upload_requires_the_token(api):
    r = api.post("/clients/doc-test/documents",
                 files={"file": ("p.png", PNG, "application/octet-stream")})
    assert r.status_code == 401


def test_listing_requires_the_token(api):
    assert api.get("/clients/doc-test/documents").status_code == 401


def test_an_unknown_document_kind_is_refused(api):
    r = api.post("/clients/doc-test/documents", headers=AUTH,
                 data={"kind": "birth_certificate"},
                 files={"file": ("p.png", PNG, "application/octet-stream")})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# The body cap is widened for uploads ONLY
# --------------------------------------------------------------------------


def test_json_endpoints_keep_the_small_cap(api):
    """Widening the cap for /documents must not widen it everywhere."""
    import src.api.main as main_mod

    assert main_mod.documents_max_upload_bytes() > 64 * 1024
    r = api.post("/clients", headers=AUTH,
                 json={"client_id": "x" * 70_000, "route": "AE-CHE",
                       "combos": ["Dubai - SCHENGEN"]})
    assert r.status_code == 413


def test_the_upload_cap_follows_the_documents_module(api):
    """One source of truth: raising MAX_BYTES must not need a second edit."""
    import src.api.main as main_mod
    from src.waitlist import documents

    assert main_mod.documents_max_upload_bytes() > documents.MAX_BYTES


def test_deleting_a_client_also_deletes_their_documents(api):
    """Otherwise a passport scan outlives the record of whose it was.

    Documents live outside the client file, so store.delete() never touched
    them — the scan stayed on disk with nothing referencing it.
    """
    _upload(api, PNG)
    assert api.get("/clients/doc-test/documents", headers=AUTH).json()["count"] == 1

    body = api.delete("/clients/doc-test", headers=AUTH).json()
    assert body["documents_removed"] == 1

    from src.waitlist import documents
    assert documents.path_for("doc-test") is None


def test_deleting_a_client_with_no_documents_still_succeeds(api):
    body = api.delete("/clients/doc-test", headers=AUTH).json()
    assert body["deleted"] is True
    assert body["documents_removed"] == 0
