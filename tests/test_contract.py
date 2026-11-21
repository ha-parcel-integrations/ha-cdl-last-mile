"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.cdl_last_mile.const import ParcelStatus
from custom_components.cdl_last_mile.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    in_transit_sample,
    late_door_scan_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]
ZONE = ZoneInfo("America/New_York")

SAMPLES = {
    "delivered": (delivered_sample, DELIVERED_CODE),
    "late_door_scan": (late_door_scan_sample, DELIVERED_CODE),
    "active": (active_sample, ACTIVE_CODE),
    "in_transit": (in_transit_sample, ACTIVE_CODE),
    "bare": (dict, ACTIVE_CODE),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    build, code = SAMPLES[request.param]
    return normalize_parcel(build(), code)


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    assert (parcel["delivered_at"] is not None) is parcel["delivered"]


def test_planned_is_empty_and_pickup_is_constant(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_delivered_parcel_dates_the_delivery_scan():
    parcel = normalize_parcel(delivered_sample(), DELIVERED_CODE)
    assert parcel["delivered"] is True
    assert parse_iso(parcel["delivered_at"]) is not None


def test_planned_window_shapes():
    moment = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    end = datetime.fromisoformat(moment[1])
    assert (end.hour, end.minute, end.second) == (23, 59, 59)
    assert end.date() == parse_iso(moment[0]).astimezone(ZONE).date()

    day = planned_window("2099-04-29", None, zone=ZONE)
    start, end = (datetime.fromisoformat(value) for value in day)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second) == (23, 59, 59)
    assert start.date() == end.date()


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING],
)
def test_planned_is_emptied_for_a_finished_parcel(status):
    assert planned_for_status(status, "a", "b") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def test_pickup_is_sticky_after_collection():
    arrived = [ParcelStatus.IN_TRANSIT, ParcelStatus.AT_PICKUP_POINT]
    assert is_pickup(None, ParcelStatus.DELIVERED, arrived) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
