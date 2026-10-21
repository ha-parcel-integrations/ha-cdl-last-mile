"""Tests for the pure parcel-mapping helpers."""

from datetime import UTC, datetime, timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cdl_last_mile.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.cdl_last_mile.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    PHOTO,
    active_sample,
    delivered_sample,
    event,
    in_transit_sample,
    late_door_scan_sample,
    response,
)

ISSUE_LINK = "issues/new?template=unrecognised_status.yml"

# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("Label has been created", ParcelStatus.REGISTERED),
        ("Awaiting Package From Shipper", ParcelStatus.REGISTERED),
        ("CDL Origin Scan", ParcelStatus.IN_TRANSIT),
        ("CDL Transfer Scan", ParcelStatus.IN_TRANSIT),
        ("Package Scanned at Local Delivery Center", ParcelStatus.IN_TRANSIT),
        ("Package Out For Delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Package Scanned at Delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivery complete", ParcelStatus.DELIVERED),
        ("DELAYED- Wrong Address", ParcelStatus.PROBLEM),
        ("Order Under Investigation", ParcelStatus.PROBLEM),
        ("  delivery COMPLETE ", ParcelStatus.DELIVERED),
    ],
)
def test_map_event_status_known(wording, expected):
    assert map_event_status(wording) == expected


def test_map_event_status_missing_and_unmapped_are_none(caplog):
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("") is None
    assert map_event_status("Teleported") is None
    assert "Teleported" in caplog.text


def test_unmapped_status_warns_only_once(caplog):
    map_event_status("Abducted")
    map_event_status("Abducted")
    assert caplog.text.count("Abducted") == 1
    assert ISSUE_LINK in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


# ---------------------------------------------------------------------------
# normalize_parcel — status derivation
# ---------------------------------------------------------------------------


def test_delivered_parcel():
    parcel = normalize_parcel(delivered_sample(), DELIVERED_CODE)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "Delivery complete"
    # 11:25 Eastern (EDT on 2026-03-25) is 15:25 UTC
    assert parcel["delivered_at"] == "2026-03-25T11:25:00-04:00"
    assert parse_iso(parcel["delivered_at"]) == datetime(
        2026, 3, 25, 15, 25, tzinfo=UTC
    )


def test_door_scan_later_than_completion_still_reads_delivered():
    """The completion is stamped on its whole minute; the door scan can read
    after it, and ``latestUpdate`` follows the door scan."""
    raw = late_door_scan_sample()
    assert raw["trackingEvents"][0]["trackingEvent"] == "Package Scanned at Delivery"
    assert raw["trackingInformation"]["latestUpdate"] == "Package Scanned at Delivery"

    parcel = normalize_parcel(raw, DELIVERED_CODE, include_history=True)

    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-03-25T11:25:00-04:00"
    assert parcel["history"][-1]["raw_status"] == "Delivery complete"


def test_door_scan_tying_with_completion_still_reads_delivered():
    raw = response(
        DELIVERED_CODE,
        [
            event("Delivery complete", "2026-03-25T11:25:00"),
            event("Package Scanned at Delivery", "2026-03-25T11:25:00"),
        ],
        "Package Scanned at Delivery",
    )
    parcel = normalize_parcel(raw, DELIVERED_CODE, include_history=True)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["history"][-1]["raw_status"] == "Delivery complete"


def test_door_scan_without_completion_is_out_for_delivery():
    raw = response(
        ACTIVE_CODE,
        [
            event("Package Out For Delivery", "2026-03-25T08:05:44"),
            event("Package Scanned at Delivery", "2026-03-25T11:24:51"),
        ],
        "Package Scanned at Delivery",
    )
    parcel = normalize_parcel(raw, ACTIVE_CODE)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_in_transit_parcel():
    parcel = normalize_parcel(in_transit_sample(), ACTIVE_CODE)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "CDL Transfer Scan"


def test_reattempt_after_problem_reads_the_newest_mappable_scan():
    raw = response(
        ACTIVE_CODE,
        [
            event("Package Out For Delivery", "2026-03-24T08:00:00"),
            event("DELAYED- Wrong Address", "2026-03-24T12:00:00"),
            event("Package Out For Delivery", "2026-03-25T08:00:00"),
        ],
        "Package Out For Delivery",
    )
    assert normalize_parcel(raw, ACTIVE_CODE)["status"] == ParcelStatus.OUT_FOR_DELIVERY
    raw["trackingEvents"] = raw["trackingEvents"][1:]
    assert normalize_parcel(raw, ACTIVE_CODE)["status"] == ParcelStatus.PROBLEM


def test_unmapped_newest_wording_falls_back_to_the_newest_mappable_scan(caplog):
    raw = response(
        ACTIVE_CODE,
        [
            event("CDL Origin Scan", "2026-03-23T08:00:00"),
            event("Mystery Handoff", "2026-03-24T08:00:00"),
        ],
        "Mystery Handoff",
    )
    parcel = normalize_parcel(raw, ACTIVE_CODE)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Mystery Handoff"
    assert "Mystery Handoff" in caplog.text
    assert ISSUE_LINK in caplog.text


def test_only_unmapped_wording_is_unknown(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("Mystery Handoff", "2026-03-24T08:00:00")],
        "Mystery Handoff",
    )
    assert normalize_parcel(raw, ACTIVE_CODE)["status"] == ParcelStatus.UNKNOWN
    assert caplog.text.count("Mystery Handoff") == 1


def test_warnings_never_carry_payload_values(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("Mystery Handoff", "2026-03-24T08:00:00", city="Secretville")],
        "Mystery Handoff",
    )
    normalize_parcel(raw, ACTIVE_CODE)
    assert "Secretville" not in caplog.text
    assert ACTIVE_CODE not in caplog.text


def test_eastern_clock_assumption_warns_once(caplog):
    normalize_parcel(active_sample(), ACTIVE_CODE)
    normalize_parcel(active_sample(), ACTIVE_CODE)
    assert caplog.text.count("America/New_York") == 1
    assert ISSUE_LINK in caplog.text


def test_timestamps_with_an_offset_are_kept_and_do_not_warn(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("CDL Origin Scan", "2026-03-23T08:00:00+00:00")],
        "CDL Origin Scan",
    )
    parcel = normalize_parcel(raw, ACTIVE_CODE, include_history=True)
    assert parcel["history"][0]["timestamp"] == "2026-03-23T08:00:00+00:00"
    assert "America/New_York" not in caplog.text


def test_empty_history_is_unknown_without_warning(caplog):
    raw = response(ACTIVE_CODE, [], "")
    parcel = normalize_parcel(raw, ACTIVE_CODE, include_history=True)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] is None
    assert parcel["history"] == []
    assert caplog.text == ""


def test_missing_events_list_warns_once_with_structure_only(caplog):
    raw = {"trackingInformation": {"referenceNumber": ACTIVE_CODE}}
    normalize_parcel(raw, ACTIVE_CODE)
    normalize_parcel(raw, ACTIVE_CODE)
    assert caplog.text.count("trackingEvents") == 1
    assert ACTIVE_CODE not in caplog.text


def test_malformed_scans_are_skipped_with_one_warning(caplog):
    raw = response(ACTIVE_CODE, [event("CDL Origin Scan", "2026-03-23T08:00:00")], "x")
    raw["trackingEvents"] += [
        "not-a-dict",
        event("CDL Transfer Scan", "not-a-date"),
        event("", "2026-03-23T09:00:00"),
        event("CDL Transfer Scan", None),
    ]
    parcel = normalize_parcel(raw, ACTIVE_CODE, include_history=True)
    assert [h["raw_status"] for h in parcel["history"]] == ["CDL Origin Scan"]
    assert caplog.text.count("lacks trackingEvent") == 1


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_is_oldest_first_and_deduplicated():
    history = build_history(delivered_sample())
    assert [h["raw_status"] for h in history] == [
        "Label has been created",
        "CDL Origin Scan",
        "Package Scanned at Local Delivery Center",
        "Package Out For Delivery",
        "Package Scanned at Delivery",
        "Delivery complete",
    ]
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED
    assert set(history[0]) == {"timestamp", "status", "raw_status"}
    assert history[0]["timestamp"] == "2026-03-22T09:00:00-04:00"


def test_history_keeps_fractional_seconds():
    history = build_history(active_sample())
    assert history[-1]["timestamp"] == "2026-03-25T08:05:44.250000-04:00"


def test_history_is_capped_to_the_newest_twenty():
    events = [
        event("CDL Transfer Scan", f"2026-03-{day:02d}T10:00:00")
        for day in range(1, 26)
    ]
    history = build_history(response(ACTIVE_CODE, events, "CDL Transfer Scan"))
    assert len(history) == 20
    assert history[0]["timestamp"].startswith("2026-03-06")
    assert history[-1]["timestamp"].startswith("2026-03-25")


def test_history_unmapped_wording_keeps_null_status():
    raw = response(ACTIVE_CODE, [event("Mystery Handoff", "2026-03-24T08:00:00")], "x")
    assert build_history(raw)[0]["status"] is None


def test_history_of_a_placeholder_is_empty():
    assert build_history({}) == []


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample(), DELIVERED_CODE)) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    parcels = [
        normalize_parcel(delivered_sample(), DELIVERED_CODE, include_history=True),
        normalize_parcel(active_sample(), ACTIVE_CODE, include_history=True),
    ]
    assert {"url", "history"} == CAPABILITIES
    assert all(p["url"] and p["history"] for p in parcels)
    for key in ("weight", "dimensions", "pickup_point", "planned_from", "planned_to"):
        assert all(p[key] is None for p in parcels)


def test_normalize_fields_the_payload_does_not_carry_are_none():
    parcel = normalize_parcel(active_sample(), ACTIVE_CODE)
    assert parcel["carrier"] == "CDL Last Mile Solutions"
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup"] is False
    assert parcel["history"] is None  # opt-in, default off
    assert (
        parcel["url"]
        == "https://apps.cdldelivers.com/Tracking-Page/track?id=CDL00000000042"
    )


def test_normalize_pending_placeholder():
    """A tracked-but-unknown code still yields a full parcel dict."""
    parcel = normalize_parcel({}, ACTIVE_CODE, include_history=True)
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] == []


def test_normalize_keeps_raw_payload_untouched():
    raw = delivered_sample()
    assert normalize_parcel(raw, DELIVERED_CODE)["raw"] is raw


def test_normalize_does_not_strip_photos_or_depend_on_them():
    with_photo = delivered_sample()
    assert with_photo["trackingEvents"][0]["vpod"] == PHOTO
    without_photo = delivered_sample()
    for scan in without_photo["trackingEvents"]:
        scan.pop("vpod")

    with_result = normalize_parcel(with_photo, DELIVERED_CODE, include_history=True)
    without_result = normalize_parcel(
        without_photo, DELIVERED_CODE, include_history=True
    )

    assert with_result["raw"]["trackingEvents"][0]["vpod"] == PHOTO
    assert {k: v for k, v in with_result.items() if k != "raw"} == {
        k: v for k, v in without_result.items() if k != "raw"
    }


def test_normalize_tolerates_null_driver_and_photo_fields():
    raw = delivered_sample()
    raw["trackingInformation"]["driverName"] = None
    raw["trackingInformation"]["podName"] = None
    assert normalize_parcel(raw, DELIVERED_CODE)["delivered"] is True


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_unconfirmed_wording_warns_once_without_parcel_data(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("CDL Transfer Scan", "2026-03-24T08:00:00", city="Secretville")],
        "CDL Transfer Scan",
    )
    parcel = normalize_parcel(raw, ACTIVE_CODE)
    normalize_parcel(
        response(
            ACTIVE_CODE,
            [event("CDL Transfer Scan", "2026-03-24T09:00:00")],
            "CDL Transfer Scan",
        ),
        ACTIVE_CODE,
    )

    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert caplog.text.count("CDL Transfer Scan") == 1
    assert ISSUE_LINK in caplog.text
    assert "Secretville" not in caplog.text
    assert ACTIVE_CODE not in caplog.text


def test_order_under_investigation_also_warns(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("Order Under Investigation", "2026-03-24T08:00:00")],
        "Order Under Investigation",
    )
    parcel = normalize_parcel(raw, ACTIVE_CODE)

    assert parcel["status"] == ParcelStatus.PROBLEM
    assert caplog.text.count("Order Under Investigation") == 1


def test_confirmed_wording_does_not_warn(caplog):
    raw = response(
        ACTIVE_CODE,
        [event("CDL Origin Scan", "2026-03-24T08:00:00+00:00")],
        "CDL Origin Scan",
    )
    normalize_parcel(raw, ACTIVE_CODE)

    assert caplog.text == ""
