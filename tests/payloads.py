"""Synthetic CDL Last Mile Solutions API payloads shared by the test modules.

Shaped like the carrier's tracking response, with invented values only.
"""

from __future__ import annotations

ACTIVE_CODE = "CDL00000000042"
DELIVERED_CODE = "900000001-2"

PHOTO = "QUJD" * 64  # synthetic base64 stand-in for a proof-of-delivery photo


def event(
    wording: str,
    timestamp: str,
    *,
    vpod: str | None = None,
    city: str = "Testville",
    state: str = "NY",
) -> dict:
    """One scan of the carrier's history, in the response's own field names."""
    return {
        "id": None,
        "trackingEvent": wording,
        "trackingEventTimestamp": timestamp,
        "vpod": vpod,
        "eventCity": city,
        "eventState": state,
        "eventZip": None,
        "isException": False,
    }


def _info(code: str, latest: str, **extra) -> dict:
    info = {
        "ordertrackingid": 1.0,
        "referenceNumber": code,
        "driverName": "Test Driver",
        "latestUpdate": latest,
        "destinationCity": "Testville",
        "destinationState": "NY",
        "destinationStreet": None,
        "destinationZip": None,
        "deliveryLocationLong": 0,
        "deliveryLocationLat": 0,
        "exception": None,
        "exceptionDetails": None,
        "podName": "Front porch",
        "suppressOnCompletion": None,
        "isAgent": False,
        "isException": None,
        "deliveryDate": None,
    }
    info.update(extra)
    return info


def response(code: str, events: list[dict], latest: str, **info_extra) -> dict:
    """A full response; ``events`` are given oldest first and served newest first."""
    return {
        "trackingEvents": list(reversed(events)),
        "trackingInformation": _info(code, latest, **info_extra),
        "mapInfo": {"driverLat": None, "driverLon": None},
        "isException": False,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel: the completion row repeats once per photo."""
    return response(
        code,
        [
            event("Label has been created", "2026-03-22T09:00:00"),
            event("CDL Origin Scan", "2026-03-23T08:15:30"),
            event("Package Scanned at Local Delivery Center", "2026-03-24T07:40:12"),
            event("Package Out For Delivery", "2026-03-25T08:05:44"),
            event("Package Scanned at Delivery", "2026-03-25T11:24:51"),
            event("Delivery complete", "2026-03-25T11:25:00", vpod=PHOTO),
            event("Delivery complete", "2026-03-25T11:25:00", vpod=PHOTO),
        ],
        "Delivery complete",
        deliveryDate="2026-03-25T11:25:00",
    )


def late_door_scan_sample(code: str = DELIVERED_CODE) -> dict:
    """Delivered, but the door scan reads six seconds after the completion."""
    return response(
        code,
        [
            event("Package Out For Delivery", "2026-03-25T08:05:44"),
            event("Delivery complete", "2026-03-25T11:25:00", vpod=PHOTO),
            event("Package Scanned at Delivery", "2026-03-25T11:25:06"),
        ],
        "Package Scanned at Delivery",
        deliveryDate="2026-03-25T11:25:00",
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    return response(
        code,
        [
            event("Label has been created", "2026-03-22T09:00:00"),
            event("CDL Origin Scan", "2026-03-23T08:15:30"),
            event("Package Out For Delivery", "2026-03-25T08:05:44.250"),
        ],
        "Package Out For Delivery",
    )


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel still moving through the network."""
    return response(
        code,
        [
            event("Label has been created", "2026-03-22T09:00:00"),
            event("CDL Transfer Scan", "2026-03-23T08:15:30"),
        ],
        "CDL Transfer Scan",
    )
