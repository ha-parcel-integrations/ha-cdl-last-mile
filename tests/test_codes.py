"""Tests for tracking-code shapes."""

import pytest

from custom_components.cdl_last_mile.codes import (
    normalize_tracking_code,
    valid_tracking_code,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cdl02867080305", "CDL02867080305"),
        ("  CDL02867080305 ", "CDL02867080305"),
        ("935911449-0", "935911449-0"),
        ("9359114490", "935911449-0"),
        (" 9359114490\n", "935911449-0"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_restores_the_order_number_hyphen(raw, expected):
    assert normalize_tracking_code(raw) == expected


@pytest.mark.parametrize(
    "code",
    ["CDL02867080305", "cdl02867080305", "935911449-0", "9359114490"],
)
def test_valid_codes(code):
    assert valid_tracking_code(code)


@pytest.mark.parametrize(
    "code",
    [
        "",
        None,
        "CDL0286708030",  # one digit short
        "CDL028670803055",  # one digit long
        "CDL02867080305-1",
        "93591144",  # too short for an order number
        "93591144900",  # too long
        "935911449-00",
        "93591144-90",
        "ABC123456",  # a shipper's own reference shape
        "ZZ0000000000",
        "CDL 02867080305",
    ],
)
def test_invalid_codes(code):
    assert not valid_tracking_code(code)
