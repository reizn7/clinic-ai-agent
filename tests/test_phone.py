from __future__ import annotations

import pytest

from clinic_agent.phone import normalize_phone, to_e164


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Exotel / E.164 forms
        ("+919876543210", "919876543210"),
        ("+91 98765 43210", "919876543210"),
        ("+91-98765-43210", "919876543210"),
        ("+14155550123", "14155550123"),
        # trunk prefix 0 + 10 digits (common in Exotel `from`)
        ("09876543210", "919876543210"),
        ("0 98765 43210", "919876543210"),
        # bare 10-digit national number
        ("9876543210", "919876543210"),
        # already digits-with-country-code (WhatsApp style) is untouched
        ("919876543210", "919876543210"),
        ("14155550123", "14155550123"),
        # international dialling prefix
        ("00919876543210", "919876543210"),
        # whitespace
        ("  9876543210 ", "919876543210"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "+", "abc"])
def test_normalize_phone_empty(raw: str | None) -> None:
    assert normalize_phone(raw) == ""


def test_normalize_phone_custom_country_code() -> None:
    assert normalize_phone("09876543210", default_cc="44") == "449876543210"
    assert normalize_phone("9876543210", default_cc="44") == "449876543210"


def test_normalize_is_idempotent() -> None:
    for raw in ["+919876543210", "09876543210", "9876543210", "14155550123"]:
        once = normalize_phone(raw)
        assert normalize_phone(once) == once


def test_to_e164() -> None:
    assert to_e164("919876543210") == "+919876543210"
    assert to_e164("+919876543210") == "+919876543210"
    assert to_e164(" 919876543210 ") == "+919876543210"
    # no re-interpretation: a 10-digit international number keeps its digits
    assert to_e164("4791234567") == "+4791234567"


def test_round_trip() -> None:
    assert to_e164(normalize_phone("09876543210")) == "+919876543210"
