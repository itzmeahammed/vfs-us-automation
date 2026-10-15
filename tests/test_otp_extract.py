"""The SMS code extractor. PURE, so every case here is a real SMS shape.

Every test is about the same failure: typing a number that is not the code.
That costs one of the two attempts the bank allows before it locks the card,
on a booking that is already paid for and cannot be re-paid. Refusing is
always the cheaper answer — it falls back to a human.

The codes below are invented. No real OTP is in this file.
"""

import pytest

from src.otp_relay import extract
from src.otp_relay.extract import find_code, looks_like_sender


# --------------------------------------------------------------------------- #
# The shapes banks actually send                                               #
# --------------------------------------------------------------------------- #

def test_a_plain_otp_message():
    assert find_code("Your OTP is 483920") == "483920"


def test_the_code_word_wins_over_an_amount_and_a_reference():
    """THE CASE THIS MODULE EXISTS FOR. Four digit runs, one code."""
    body = ("AED 350.00 at VFS GLOBAL on 15/10/26. OTP 483920. "
            "Valid 5 min. Ref 778101.")
    assert find_code(body) == "483920"


def test_code_before_the_word():
    assert find_code("483920 is your verification code") == "483920"


def test_real_world_wordings():
    for body, expected in [
        ("Mashreq: AED 350 at VFSGLOBAL. Your OTP 112233", "112233"),
        ("Do not share. Your secure code is 445566 for AED 1,234", "445566"),
        ("778899 is your one time password for your Visa card", "778899"),
        ("Use passcode 334455 to authorise AED 99.00", "334455"),
    ]:
        assert find_code(body) == expected, body


def test_a_four_digit_code_when_asked_for_four():
    assert find_code("Your PIN is 1234", digits=4) == "1234"


def test_the_expected_length_is_exact():
    """A 6-digit search must not pull 6 digits out of an 8-digit number."""
    assert find_code("Reference 12345678") is None
    assert find_code("Your OTP is 48392") is None        # 5 digits
    assert find_code("Your OTP is 4839201") is None      # 7 digits


# --------------------------------------------------------------------------- #
# WHAT IT MUST REFUSE                                                          #
# --------------------------------------------------------------------------- #

def test_an_amount_is_never_a_code():
    assert find_code("Transaction of AED 350.000 completed") is None


def test_a_decimal_is_not_a_code_on_either_side():
    assert find_code("Charged 123456.78 to your card") is None
    assert find_code("Charged 99.123456 to your card") is None


def test_a_date_is_not_a_code():
    assert find_code("Transaction on 15/10/2026 at 14:30") is None


def test_a_thousands_group_is_not_a_code():
    assert find_code("Amount AED 123,456 approved") is None


def test_ambiguity_without_a_code_word_is_refused():
    """TWO candidates and nothing to choose between them. Guessing here is
    how the reference number gets typed into the bank's form."""
    assert find_code("112233 445566") is None


def test_ambiguity_with_a_code_word_is_resolved():
    assert find_code("Ref 112233. Your OTP is 445566") == "445566"


def test_a_code_word_too_far_away_does_not_claim_a_candidate():
    """A code word at the start of a long message must not adopt a number
    sixty characters later that belongs to something else."""
    body = ("Your code has been sent separately. This transaction of AED 20 "
            "was made at MERCHANT on a date and carries reference 998877 and "
            "another number 112233")
    assert find_code(body) is None


def test_an_empty_or_codeless_body_is_none():
    assert find_code("") is None
    assert find_code(None) is None
    assert find_code("Your payment was approved. Thank you.") is None


def test_a_trailing_full_stop_does_not_disqualify_a_code():
    """'OTP 483920.' is the normal shape — the stop ends the sentence, it is
    not a decimal point. An earlier cut of this module rejected it."""
    assert find_code("Your OTP 483920.") == "483920"
    assert find_code("OTP 483920. Do not share.") == "483920"


def test_custom_code_words_override_the_defaults():
    body = "Ref 112233. Kode 445566"
    assert find_code(body) is None
    assert find_code(body, code_words=["kode"]) == "445566"


def test_code_words_match_whole_words_only():
    """'encoded' contains 'code' and must not count as a code word."""
    assert "code" in extract.CODE_WORDS
    assert find_code("Encoded 112233 and 445566") is None


# --------------------------------------------------------------------------- #
# THE REAL THING: RAKBANK, the issuer of the card this system pays with        #
#                                                                              #
# Supplied by the operator 2026-10-15 as the exact wording their 3-D Secure    #
# SMS uses. This is the one test here that is not a constructed example, so if #
# any test in this file is allowed to fail, it is not this one.                #
#                                                                              #
# The code below is spent — single-use, five-minute validity, long expired.    #
# --------------------------------------------------------------------------- #

RAKBANK_SMS = (
    "Use OTP 632007 to complete your 83.3 AED txn at RAKBANK on card ending "
    "with 9340 (Ref: XWE). Valid for 5 mins. Don't share it. Not You? Call "
    "600 500 946."
)


def test_the_rakbank_otp_is_extracted():
    assert find_code(RAKBANK_SMS) == "632007"


def test_the_rakbank_sms_has_exactly_one_six_digit_candidate():
    """WHY IT IS SAFE, not just that it works. The digit runs in this message
    are 632007, 83, 3, 9340, 5, 600, 500, 946 — the amount splits at its
    decimal point, so at six digits there is nothing to confuse the code
    with and no reliance on the 'OTP' keyword at all."""
    candidates = [c[1] for c in extract._candidates(RAKBANK_SMS.lower(), 6)]
    assert candidates == ["632007"]


def test_the_rakbank_amount_and_helpline_are_never_the_code():
    for wrong in ("83", "9340", "600", "500", "946", "5", "3"):
        assert find_code(RAKBANK_SMS) != wrong


def test_four_digits_would_pick_the_cards_last_four_which_is_the_trap():
    """*** A STANDING WARNING, ASSERTED SO IT CANNOT BE FORGOTTEN. ***

    This SMS names the card by its last four digits — 9340, which is this
    card's real tail. Set `otp_digits` to 4 for this issuer and the bot would
    confidently type the card number's own tail into the bank's form, burn an
    attempt, and look like a parsing success in the log.

    Six is correct for RAKBANK and for 3-D Secure generally. This test exists
    so that anyone tempted to 'make it more flexible' sees the cost first.
    """
    assert find_code(RAKBANK_SMS, digits=4) == "9340"
    assert find_code(RAKBANK_SMS, digits=6) == "632007"


def test_the_helpline_is_refused_at_three_digits_rather_than_guessed():
    """'Call 600 500 946' is three equally-good candidates and no code word
    close to any of them. Refusing is the whole design."""
    assert find_code(RAKBANK_SMS, digits=3) is None


# --------------------------------------------------------------------------- #
# Sender filtering                                                             #
# --------------------------------------------------------------------------- #

def test_an_empty_allowlist_trusts_everyone():
    """The only honest default: a bank's sender ID is not knowable before the
    first live run."""
    assert looks_like_sender("MashreqBK", [])
    assert looks_like_sender("", [])


def test_a_sender_is_matched_case_insensitively_as_a_substring():
    """An operator writes 'mashreq' having seen 'MashreqBK'."""
    assert looks_like_sender("MashreqBK", ["mashreq"])
    assert looks_like_sender("VFS-GLOBAL", ["vfs"])
    assert not looks_like_sender("SCAMMER", ["mashreq"])


def test_a_blank_entry_in_the_allowlist_is_ignored_not_a_wildcard():
    assert not looks_like_sender("SCAMMER", ["  ", "mashreq"])
