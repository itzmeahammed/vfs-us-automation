"""country_code must be refused before booking, not on the payment page.

A record with country_code "0000" passed precheck (it was non-empty), took a
slot, and then stopped on the CyberSource country select — which offers only
ISO alpha-2 values — leaving a booked, unpaid appointment.
"""

from __future__ import annotations

import pytest

from src.booking import requests as store

BILLING = {"first_name": "A", "last_name": "B", "address_line_1": "x",
           "address_line_2": "y", "city": "Dubai", "postcode": "00000",
           "phone_number": "501234567", "email": "a@example.com"}


def _fields(problems):
    return [p.field for p in problems]


@pytest.mark.parametrize("bad", ["0000", "971", "UAE", "A"])
def test_a_non_iso_country_code_is_refused(bad):
    assert "country_code" in _fields(store._check_billing({**BILLING, "country_code": bad}))


def test_an_iso_country_code_passes():
    assert "country_code" not in _fields(store._check_billing({**BILLING, "country_code": "AE"}))


def test_normalise_uppercases_and_defaults():
    assert store.normalise({"country_code": " ae "})["country_code"] == "AE"
    assert store.normalise({})["country_code"] == store.DEFAULT_COUNTRY_CODE
