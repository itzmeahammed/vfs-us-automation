from ai_assistant.redact import REDACTED, Redactor, StreamRestorer


def test_secret_keys_are_destroyed_whatever_the_value():
    r = Redactor()
    out = r.obj({"password": "hunter2", "api_key": "abc", "bot_token": "x",
                 "account_password": "p", "otp": {"ok": 3}, "tokens": 120,
                 "nested": [{"Authorization": "Bearer abc"}]})
    assert out["password"] == out["api_key"] == out["bot_token"] == REDACTED
    assert out["account_password"] == REDACTED
    assert out["otp"] == {"ok": 3}            # stats named like secrets survive
    assert out["tokens"] == 120
    assert out["nested"][0]["Authorization"] == REDACTED


def test_secret_shaped_values_are_scrubbed_in_free_text():
    r = Redactor(pseudonymise_emails=False)
    text = ("proxy http://user123:s3cr3t@res.proxy-seller.com:10002 failed; "
            "key sk-proj-abcdefghijklmnopqrstuvwx used; Authorization: Bearer abc.def.ghi123; "
            "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ; url ?token=zzz&x=1; "
            "password=letmein card 4111 1111 1111 1111")
    out = r.text(text)
    for secret in ("user123", "s3cr3t", "sk-proj-abcdef", "abc.def.ghi123",
                   "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ", "zzz", "letmein", "4111 1111 1111 1111"):
        assert secret not in out, secret
    assert "res.proxy-seller.com:10002" in out          # the useful part stays
    assert "[card ****1111]" in out


def test_long_ids_that_are_not_cards_survive():
    r = Redactor()
    assert "79199388732462654041" in r.text("TransactionId=79199388732462654041")
    assert "2026-10-15 09:55:42" in r.text("at 2026-10-15 09:55:42")


def test_emails_become_stable_pseudonyms_and_restore():
    r = Redactor(salt="s")
    a = r.text("ahmed@travnook.com failed, then Ahmed@Travnook.com again; bob@gmail.com ok")
    assert "ahmed" not in a.lower() and "bob@" not in a
    tokens = set(t for t in a.replace(",", " ").replace(";", " ").split() if t.startswith("acct-"))
    assert len(tokens) == 2                              # case-insensitive identity
    assert all(t.endswith(("@travnook.com", "@gmail.com")) for t in tokens)
    assert r.restore(a).count("@travnook.com") == 2
    assert "bob@gmail.com" in r.restore(a)


def test_pseudonyms_differ_per_request_salt():
    assert Redactor(salt="a").text("x@y.com") != Redactor(salt="b").text("x@y.com")


def test_stream_restorer_never_splits_a_pseudonym():
    r = Redactor(salt="s")
    token = r.text("alice@corp.com")
    stream = f"Account {token} is benched.\n"
    restorer = StreamRestorer(r)
    out = "".join(restorer.feed(stream[i:i + 3]) for i in range(0, len(stream), 3)) + restorer.flush()
    assert out == "Account alice@corp.com is benched.\n"
