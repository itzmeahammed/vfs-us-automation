"""The company card: loaded from the environment, never persisted, never logged.

Every test here is about a failure that would otherwise be discovered on a LIVE
payment page, with a client's booking sitting on it unpaid.

The card numbers below are the publicly published test numbers every payment
processor documents (4111..., 3714...). They are not anyone's card.
"""

import os
from datetime import date

import pytest

from src.payment import card as card_mod
from src.payment.card import Card, CardError

VISA = "4111111111111111"
AMEX = "371449635398431"
FUTURE = "11/29"


def _env(**overrides):
    env = {"VFS_CARD_NUMBER": VISA, "VFS_CARD_EXPIRY": FUTURE,
           "VFS_CARD_CVN": "123"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


TODAY = date(2026, 9, 28)


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def test_a_good_card_loads():
    card = card_mod.load(_env(), today=TODAY)
    assert card.number == VISA
    assert card.expiry_month == "11" and card.expiry_year == "2029"
    assert card.card_type == "visa"


def test_no_card_configured_is_None_not_an_error():
    """An ordinary state on a developer machine. The CALLER decides whether
    that is fatal — this module does not."""
    assert card_mod.load({}, today=TODAY) is None


def test_spaces_and_dashes_are_accepted():
    """Operators paste card numbers in the form printed on the card."""
    card = card_mod.load(_env(VFS_CARD_NUMBER="4111-1111 1111-1111"),
                         today=TODAY)
    assert card.number == VISA


def test_a_two_digit_year_is_this_century():
    card = card_mod.load(_env(VFS_CARD_EXPIRY="03/2031"), today=TODAY)
    assert card.expiry_year == "2031"
    assert card_mod.load(_env(VFS_CARD_EXPIRY="03/31"),
                         today=TODAY).expiry_year == "2031"


# --------------------------------------------------------------------------- #
# Refusals — each of these would otherwise fail on a live gateway              #
# --------------------------------------------------------------------------- #

def test_a_mistyped_digit_is_caught_by_the_luhn_check():
    """THE MOST VALUABLE CHECK HERE. A transposed digit is invisible to a human
    re-reading the number and produces a declined payment mid-booking."""
    with pytest.raises(CardError, match="Luhn"):
        card_mod.load(_env(VFS_CARD_NUMBER="4111111111111112"), today=TODAY)


def test_an_expired_card_is_refused_offline():
    with pytest.raises(CardError, match="expired"):
        card_mod.load(_env(VFS_CARD_EXPIRY="01/25"), today=TODAY)


def test_a_card_is_valid_through_the_last_day_of_its_expiry_month():
    """11/26 is good on 30 November 2026 and dead on 1 December."""
    env = _env(VFS_CARD_EXPIRY="11/26")
    assert card_mod.load(env, today=date(2026, 11, 30)) is not None
    with pytest.raises(CardError, match="expired"):
        card_mod.load(env, today=date(2026, 12, 1))


def test_a_nonsense_month_is_refused():
    with pytest.raises(CardError, match="not a month"):
        card_mod.load(_env(VFS_CARD_EXPIRY="13/29"), today=TODAY)


def test_an_unparseable_expiry_is_refused():
    with pytest.raises(CardError, match="MM/YY"):
        card_mod.load(_env(VFS_CARD_EXPIRY="November 2029"), today=TODAY)


def test_a_number_without_an_expiry_is_refused():
    with pytest.raises(CardError, match="VFS_CARD_EXPIRY is not set"):
        card_mod.load({"VFS_CARD_NUMBER": VISA, "VFS_CARD_CVN": "123"},
                      today=TODAY)


def test_a_number_without_a_cvn_is_refused():
    with pytest.raises(CardError, match="VFS_CARD_CVN is not set"):
        card_mod.load({"VFS_CARD_NUMBER": VISA, "VFS_CARD_EXPIRY": FUTURE},
                      today=TODAY)


def test_an_amex_cvn_must_be_four_digits():
    """A 3-digit CVN with an amex number almost always means the CVN was
    copied from a different card."""
    with pytest.raises(CardError, match="amex CVN is 4 digits"):
        card_mod.load(_env(VFS_CARD_NUMBER=AMEX, VFS_CARD_CVN="123"),
                      today=TODAY)
    assert card_mod.load(_env(VFS_CARD_NUMBER=AMEX, VFS_CARD_CVN="1234"),
                         today=TODAY).card_type == "amex"


def test_a_number_of_the_wrong_length_is_refused():
    with pytest.raises(CardError, match="12-19 digits"):
        card_mod.load(_env(VFS_CARD_NUMBER="4111"), today=TODAY)


def test_the_card_type_is_inferred_from_the_number():
    assert card_mod.load(_env(), today=TODAY).card_type == "visa"
    assert card_mod.load(_env(VFS_CARD_NUMBER="5555555555554444"),
                         today=TODAY).card_type == "mastercard"


def test_an_explicit_type_overrides_the_inferred_one():
    card = card_mod.load(_env(VFS_CARD_TYPE="visa"), today=TODAY)
    assert card.card_type == "visa"


# --------------------------------------------------------------------------- #
# THE CARD MUST NOT LEAK                                                       #
#                                                                              #
# These are the tests that matter after the code is written and forgotten: the #
# default __repr__ prints every field, and the first place that bites is an    #
# exception traceback — exactly when someone is pasting output into a chat.    #
# --------------------------------------------------------------------------- #

def test_repr_does_not_contain_the_card_number():
    card = card_mod.load(_env(), today=TODAY)
    assert VISA not in repr(card)
    assert VISA not in str(card)
    assert VISA not in f"{card}"
    assert "****1111" in repr(card)


def test_the_cvn_never_appears_in_any_rendering():
    card = card_mod.load(_env(VFS_CARD_CVN="987"), today=TODAY)
    assert "987" not in repr(card)
    assert "987" not in str(card)


def test_an_error_message_never_quotes_the_number():
    """A refusal has to be actionable WITHOUT echoing the value — the operator
    knows what they typed; the log does not need a copy."""
    with pytest.raises(CardError) as caught:
        card_mod.load(_env(VFS_CARD_NUMBER="4111111111111112"), today=TODAY)
    assert "4111111111111112" not in str(caught.value)

    with pytest.raises(CardError) as caught:
        card_mod.load(_env(VFS_CARD_NUMBER="4111"), today=TODAY)
    assert "4111" not in str(caught.value)


def test_the_cvn_is_not_offered_to_the_redaction_filter():
    """Three digits would match counts, timings and HTTP statuses all over the
    log. It is never logged in the first place."""
    card = card_mod.load(_env(VFS_CARD_CVN="123"), today=TODAY)
    assert "123" not in card.secret_values()
    assert VISA in card.secret_values()


def test_installing_redaction_scrubs_the_number_from_log_lines():
    from src.waitlist import redaction

    card = card_mod.load(_env(), today=TODAY)
    card_mod.install_redaction(card)
    assert VISA not in redaction.scrub(f"oops the number is {VISA} here")


def test_installing_redaction_with_no_card_is_harmless():
    card_mod.install_redaction(None)


# --------------------------------------------------------------------------- #

def test_the_masked_form_is_what_is_safe_to_print():
    card = Card(number=VISA, expiry_month="11", expiry_year="2029",
                cvn="123", card_type="visa")
    assert card.masked == "visa ****1111"
    assert card.expiry_mm_yy == "11/29"


# --------------------------------------------------------------------------- #
# THE CARD FILE                                                                #
#                                                                              #
# A file is the source most likely to end up somewhere it should not be, so    #
# most of these tests are about the refusals rather than the happy path.       #
#                                                                              #
# tmp_path lies outside any work tree, so `git check-ignore` exits 128 and the #
# committable guard stands down. That is deliberate behaviour, and it is       #
# asserted directly rather than relied on silently.                            #
# --------------------------------------------------------------------------- #

def _card_file(tmp_path, text, name="card.local.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_card_file_loads(tmp_path):
    path = _card_file(tmp_path, "number = 4111 1111 1111 1111\n"
                                "expiry = 11/29\n"
                                "cvn = 123\n"
                                "name = A COMPANY LTD\n")
    card = card_mod.load({}, today=TODAY, card_file=path)
    assert card.number == VISA
    assert card.expiry_mm_yy == "11/29"
    assert card.cvn == "123"
    assert card.name == "A COMPANY LTD"
    assert card.card_type == "visa"


def test_comments_blank_lines_and_colons_are_accepted(tmp_path):
    path = _card_file(tmp_path, "# the company card\n"
                                "\n"
                                "number: 4111-1111-1111-1111\n"
                                "  expiry : 11/29   \n"
                                "; a semicolon comment\n"
                                'cvn = "123"\n')
    assert card_mod.load({}, today=TODAY, card_file=path).number == VISA


def test_the_full_variable_names_work_as_keys_too(tmp_path):
    """One set of documentation has to cover both sources."""
    path = _card_file(tmp_path, f"VFS_CARD_NUMBER = {VISA}\n"
                                "VFS_CARD_EXPIRY = 11/29\n"
                                "VFS_CARD_CVN = 123\n")
    assert card_mod.load({}, today=TODAY, card_file=path).number == VISA


def test_cvv_and_cardholder_aliases_work(tmp_path):
    """The operator is reading a plastic card, which says CVV, not CVN."""
    path = _card_file(tmp_path, f"pan = {VISA}\nexp = 11/29\ncvv = 123\n"
                                "cardholder = A COMPANY LTD\n")
    card = card_mod.load({}, today=TODAY, card_file=path)
    assert card.cvn == "123" and card.name == "A COMPANY LTD"


def test_a_notepad_byte_order_mark_does_not_break_the_first_key(tmp_path):
    """Windows operators will write this file in Notepad."""
    path = tmp_path / "card.local.txt"
    path.write_text(f"number = {VISA}\nexpiry = 11/29\ncvn = 123\n",
                    encoding="utf-8-sig")
    assert card_mod.load({}, today=TODAY,
                         card_file=str(path)).number == VISA


def test_the_environment_beats_the_file_field_by_field(tmp_path):
    """Exporting one value for one run must not require editing the file."""
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\nname = FROM FILE\n")
    card = card_mod.load({"VFS_CARD_CVN": "321"}, today=TODAY,
                         card_file=path)
    assert card.cvn == "321"            # from the environment
    assert card.number == VISA          # still from the file
    assert card.name == "FROM FILE"


def test_file_values_are_validated_exactly_like_environment_ones(tmp_path):
    """The validation is the point. A file must not be a way around it."""
    path = _card_file(tmp_path, "number = 4111111111111112\n"
                                "expiry = 11/29\ncvn = 123\n")
    with pytest.raises(CardError, match="Luhn"):
        card_mod.load({}, today=TODAY, card_file=path)

    expired = _card_file(tmp_path, f"number = {VISA}\nexpiry = 01/25\n"
                                   "cvn = 123\n", name="old.card.txt")
    with pytest.raises(CardError, match="expired"):
        card_mod.load({}, today=TODAY, card_file=expired)


def test_a_missing_discovered_file_is_not_an_error(tmp_path):
    """No card file on a developer machine is an ordinary state."""
    missing = str(tmp_path / "nope.txt")
    assert card_mod.read_card_file(missing, required=False) == {}


def test_a_missing_named_file_is_an_error(tmp_path):
    """A typo'd VFS_CARD_FILE must not look identical to having no card."""
    missing = str(tmp_path / "nope.txt")
    with pytest.raises(CardError, match="No card file at"):
        card_mod.load({"VFS_CARD_FILE": missing}, today=TODAY)


def test_an_unknown_key_is_refused_by_name(tmp_path):
    """Silently ignoring `cv2 = 123` means the CVN is silently missing."""
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cv2 = 123\n")
    with pytest.raises(CardError, match="line 3: unknown key 'cv2'"):
        card_mod.load({}, today=TODAY, card_file=path)


def test_a_key_set_twice_is_refused(tmp_path):
    path = _card_file(tmp_path, f"number = {VISA}\nnumber = {AMEX}\n")
    with pytest.raises(CardError, match="set twice"):
        card_mod.load({}, today=TODAY, card_file=path)


def test_a_line_with_no_separator_is_refused_without_echoing_it(tmp_path):
    path = _card_file(tmp_path, f"number = {VISA}\n{VISA}\n")
    with pytest.raises(CardError) as caught:
        card_mod.load({}, today=TODAY, card_file=path)
    assert "line 2" in str(caught.value)
    assert VISA not in str(caught.value)


def test_a_file_error_never_quotes_a_value(tmp_path):
    """Same rule as the environment: the error has to be actionable without
    a copy of the card in it. This is the message someone pastes into chat."""
    path = _card_file(tmp_path, "number = 4111111111111112\n"
                                "expiry = 11/29\ncvn = 123\n")
    with pytest.raises(CardError) as caught:
        card_mod.load({}, today=TODAY, card_file=path)
    assert "4111111111111112" not in str(caught.value)


# ----------------------------- THE REFUSALS -------------------------------- #

def test_a_card_file_git_would_let_you_commit_is_refused(tmp_path,
                                                         monkeypatch):
    """The whole reason a file is tolerable at all. A PAN in git history is
    not removed by deleting the file — the remedy is cancelling the card."""
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\n")
    monkeypatch.setattr(card_mod, "_git_ignores", lambda p: False)
    with pytest.raises(CardError, match="NOT gitignored"):
        card_mod.load({}, today=TODAY, card_file=path)


def test_a_gitignored_card_file_is_accepted(tmp_path, monkeypatch):
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\n")
    monkeypatch.setattr(card_mod, "_git_ignores", lambda p: True)
    assert card_mod.load({}, today=TODAY, card_file=path).number == VISA


def test_a_file_git_cannot_judge_is_accepted(tmp_path, monkeypatch):
    """None means git is absent or the file is outside any work tree. A card
    kept outside the repo is the BEST case and must not be refused."""
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\n")
    monkeypatch.setattr(card_mod, "_git_ignores", lambda p: None)
    assert card_mod.load({}, today=TODAY, card_file=path).number == VISA


def test_the_real_repo_gitignores_the_default_card_file():
    """Guards the .gitignore entry itself: delete it and the default path
    stops being loadable, because _check_committable refuses it."""
    assert card_mod._git_ignores(card_mod.DEFAULT_CARD_FILE) is not False


@pytest.mark.skipif(os.name != "posix", reason="st_mode is a fiction on NT")
def test_a_world_readable_card_file_is_refused(tmp_path):
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\n")
    os.chmod(path, 0o644)
    with pytest.raises(CardError, match="readable by other users"):
        card_mod.load({}, today=TODAY, card_file=path)

    os.chmod(path, 0o600)
    assert card_mod.load({}, today=TODAY, card_file=path).number == VISA


# -------------------------- DISCOVERY / PRECEDENCE ------------------------- #

def test_passing_an_env_dict_turns_off_file_discovery():
    """Otherwise every other test in this file would start depending on
    whether the machine running it happens to have a card file on disk."""
    assert card_mod.resolve_card_file({}, explicit_env=True) is None
    assert (card_mod.resolve_card_file({}, explicit_env=False)
            == card_mod.DEFAULT_CARD_FILE)


def test_an_explicitly_named_file_is_used_even_with_an_env_dict(tmp_path):
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 123\n")
    assert card_mod.resolve_card_file({"VFS_CARD_FILE": path},
                                      explicit_env=True) == path
    assert card_mod.load({"VFS_CARD_FILE": path},
                         today=TODAY).number == VISA


def test_card_file_none_means_environment_only(tmp_path, monkeypatch):
    """The escape hatch: a caller that wants nothing to do with disk."""
    monkeypatch.setenv("VFS_CARD_FILE", _card_file(
        tmp_path, f"number = {VISA}\nexpiry = 11/29\ncvn = 123\n"))
    for name in ("VFS_CARD_NUMBER", "VFS_CARD_EXPIRY", "VFS_CARD_CVN"):
        monkeypatch.delenv(name, raising=False)
    assert card_mod.load(today=TODAY, card_file=None) is None


def test_the_file_is_never_written_to(tmp_path, monkeypatch):
    """Reading a card must not rewrite, normalise or even touch the file."""
    text = f"number = {VISA}\nexpiry = 11/29\ncvn = 123\n"
    path = _card_file(tmp_path, text)
    monkeypatch.setattr(card_mod, "_git_ignores", lambda p: True)
    card_mod.load({}, today=TODAY, card_file=path)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == text


def test_the_card_file_path_is_not_a_secret_but_its_contents_are(tmp_path,
                                                                 monkeypatch):
    """The path appears in errors on purpose — it is how an operator finds
    the file to fix. Nothing inside it may follow."""
    path = _card_file(tmp_path, f"number = {VISA}\nexpiry = 11/29\n"
                                "cvn = 999\nname = SECRET HOLDER\n")
    monkeypatch.setattr(card_mod, "_git_ignores", lambda p: True)
    card = card_mod.load({}, today=TODAY, card_file=path)
    rendered = f"{card!r} {card}"
    assert VISA not in rendered
    assert "999" not in rendered
    assert "SECRET HOLDER" not in rendered
