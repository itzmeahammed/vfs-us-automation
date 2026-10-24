"""The payment page's billing country is ours, not the caller's.

A record with country_code "0000" passed precheck (it was non-empty), took a
slot, and then stopped on the CyberSource country select — which offers only
ISO alpha-2 values — leaving a booked, unpaid appointment. Validating the
caller's value only moved that failure earlier; every applicant is resident in
the UAE, so the value is now fixed and the field is not asked for at all.
"""

from __future__ import annotations

import pytest

from src.booking import requests as store

BILLING = {"first_name": "A", "last_name": "B", "address_line_1": "x",
           "address_line_2": "y", "city": "Dubai", "postcode": "00000",
           "phone_number": "501234567", "email": "a@example.com"}


def _fields(problems):
    return [p.field for p in problems]


# --------------------------------------------------------------------------- #
# The billing country is FIXED, not supplied                                   #
# --------------------------------------------------------------------------- #
#
# Callers sent "United Arab Emirates", a name their field cut to "United A",
# and the dialling code "971". Each matched no option on the gateway's country
# select and failed there, with the slot already taken. So normalise() now
# overwrites whatever arrives.

@pytest.mark.parametrize("sent", [
    "United Arab Emirates", "United A", "971", "uae", "0000", "NO", "", None])
def test_whatever_the_caller_sends_the_billing_country_is_ours(sent):
    assert store.normalise({"country_code": sent})["country_code"] ==         store.BILLING_COUNTRY


def test_an_absent_country_is_no_longer_a_problem_to_report():
    """There is nothing an agent could fix, so asking them to is noise."""
    assert "country_code" not in _fields(
        store._check_billing({**BILLING, "route": "AE-NOR"}))
    assert "country_code" not in _fields(
        store._check_billing({**BILLING, "country_code": "United A"}))


def test_the_payment_page_reads_the_fixed_country_from_an_old_record():
    """as_registrant is what fills the gateway's select, and records written
    before this still hold a country name."""
    request = store.BookingRequest("trav-nook-ae-nor-1234ab", {
        "route": "AE-NOR", "combo": "x", "country_code": "United Arab Emirates",
        "first_name": "A", "last_name": "B"})
    assert request.as_registrant().data["country_code"] == store.BILLING_COUNTRY


def test_the_web_app_is_not_asked_for_it():
    """GET /v1/routes/{route}/readiness drives the agent's form."""
    from src.waitlist import validate

    names = [f["name"] for f in validate.required_fields_live("AE-NOR")]
    assert "country_code" not in names
    assert "postcode" in names          # the other billing fields still are
