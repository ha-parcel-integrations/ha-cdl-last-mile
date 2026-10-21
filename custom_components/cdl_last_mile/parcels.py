"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That keeps the carrier-specific
mapping apart from the coordinator and makes it unit-testable without
spinning up HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-cdl-last-mile/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the casefolded scan wording; the service sends English text and no
# machine code. ``Package Scanned at Delivery`` is the door scan: on its own it
# reads out for delivery, but a ``Delivery complete`` row overrides it for the
# parcel as a whole (see :func:`normalize_parcel`).
_STATUS_MAP: dict[str, ParcelStatus] = {
    "label has been created": ParcelStatus.REGISTERED,
    "awaiting package from shipper": ParcelStatus.REGISTERED,
    "cdl origin scan": ParcelStatus.IN_TRANSIT,
    "cdl transfer scan": ParcelStatus.IN_TRANSIT,
    "package scanned at local delivery center": ParcelStatus.IN_TRANSIT,
    "package out for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "package scanned at delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "delivery complete": ParcelStatus.DELIVERED,
    "delayed- wrong address": ParcelStatus.PROBLEM,
    "order under investigation": ParcelStatus.PROBLEM,
}
_COMPLETION = "delivery complete"

# Mapped from the carrier's own tracker wording, but never seen on a real
# parcel, so the mapping is a guess until a user reports one.
_UNCONFIRMED_WORDINGS = frozenset({"cdl transfer scan", "order under investigation"})

# The service stamps scans with a wall clock and no offset. Eastern is an
# assumption about where the clock comes from, not something the payload
# proves; a one-shot WARNING asks users to confirm it.
_CLOCK_ZONE = ZoneInfo("America/New_York")

# Warnings already issued this HA session, so each fires once, not per poll.
_unmapped_statuses_logged: set[str] = set()
_shape_warnings_logged: set[str] = set()


def _warn_unmapped_status(wording: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if wording in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(wording)
    _LOGGER.warning(
        "Unrecognised CDL Last Mile Solutions status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown' unless an "
        "earlier scan can be mapped",
        NEW_ISSUE_URL,
        wording,
    )


def _warn_shape(key: str, message: str) -> None:
    """Log a payload-shape or assumption warning once per HA session."""
    if key in _shape_warnings_logged:
        return
    _shape_warnings_logged.add(key)
    _LOGGER.warning(
        "CDL Last Mile Solutions: %s. Open an issue and paste this line: %s",
        message,
        NEW_ISSUE_URL,
    )


def map_event_status(wording: str | None) -> ParcelStatus | None:
    """Map one scan's wording to a canonical status, or ``None``.

    Unmapped wording keeps ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warns once.
    """
    if not wording:
        return None
    key = wording.strip().casefold()
    mapped = _STATUS_MAP.get(key)
    if mapped is not None:
        if key in _UNCONFIRMED_WORDINGS:
            _warn_shape(
                f"unconfirmed:{key}",
                f"the scan wording '{wording.strip()}' is mapped to "
                f"'{mapped}' from the carrier's tracker, not from a parcel we have "
                "seen — confirm the mapping looks right",
            )
        return mapped
    _warn_unmapped_status(wording)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _scan_time(value: Any) -> datetime | None:
    """Parse a scan timestamp to an aware datetime, or ``None``.

    The service omits the UTC offset; those values are read as Eastern time.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        _warn_shape(
            "clock",
            "scan timestamps carry no UTC offset and are read as America/New_York, "
            "which is unconfirmed — report it if times look off by hours",
        )
        parsed = parsed.replace(tzinfo=_CLOCK_ZONE)
    return parsed


def _ordered_events(raw: dict) -> list[dict]:
    """Return the scans oldest to newest, de-duplicated.

    A delivery is listed once per proof-of-delivery photo, so identical
    wording and clock collapse into one. The completed delivery is stamped on
    its whole minute while the door scan carries real seconds and can read
    after it; within one minute the completion therefore sorts last.
    """
    events = raw.get("trackingEvents")
    if not isinstance(events, list):
        _warn_shape("events", "the response has no trackingEvents list")
        return []
    seen: set[tuple[str, str]] = set()
    ordered: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        wording = event.get("trackingEvent")
        stamp = event.get("trackingEventTimestamp")
        moment = _scan_time(stamp)
        if (
            not isinstance(wording, str)
            or not wording.strip()
            or not isinstance(stamp, str)
            or moment is None
        ):
            _warn_shape(
                "event",
                "a scan lacks trackingEvent or a readable trackingEventTimestamp",
            )
            continue
        wording = wording.strip()
        if (wording, stamp) in seen:
            continue
        seen.add((wording, stamp))
        ordered.append(
            {
                "moment": moment,
                "wording": wording,
                "status": map_event_status(wording),
            }
        )
    ordered.sort(
        key=lambda item: (
            item["moment"].replace(second=0, microsecond=0),
            item["wording"].casefold() == _COMPLETION,
            item["moment"],
        )
    )
    return ordered


def build_history(raw: dict, *, max_events: int = HISTORY_MAX_EVENTS) -> list[dict]:
    """Build the canonical ``history`` list from the response.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Oldest to newest, capped to the most recent
    ``max_events``.
    """
    if not raw:
        return []
    return [
        {
            "timestamp": event["moment"].isoformat(),
            "status": event["status"],
            "raw_status": event["wording"],
        }
        for event in _ordered_events(raw)
    ][-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(
    raw: dict, tracking_code: str, *, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    Status is derived from the whole history, never the newest row: any
    ``Delivery complete`` scan means delivered, otherwise the newest scan with
    a mapped wording decides. ``raw`` may be empty (a code the service does not
    know yet), which yields an ``unknown`` placeholder.
    """
    events = _ordered_events(raw) if raw else []
    completion = next(
        (e for e in reversed(events) if e["wording"].casefold() == _COMPLETION), None
    )
    delivered = completion is not None

    if completion is not None:
        status = ParcelStatus.DELIVERED
        raw_status = completion["wording"]
    else:
        status = next(
            (e["status"] for e in reversed(events) if e["status"] is not None),
            ParcelStatus.UNKNOWN,
        )
        raw_status = events[-1]["wording"] if events else None

    return {
        "carrier": "CDL Last Mile Solutions",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": completion["moment"].isoformat() if completion else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
