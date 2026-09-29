"""The company card: loaded from the environment, never persisted, never logged.

Every test here is about a failure that would otherwise be discovered on a LIVE
payment page, with a client's booking sitting on it unpaid.

The card numbers below are the publicly published test numbers every payment
processor documents (4111..., 3714...). They are not anyone's card.
"""

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
