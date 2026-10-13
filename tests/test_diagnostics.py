"""Tests for CDL Last Mile Solutions diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.cdl_last_mile.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.cdl_last_mile.parcels import normalize_parcel

from .payloads import DELIVERED_CODE, PHOTO, delivered_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "900000001-2"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    raw = delivered_sample(DELIVERED_CODE)
    entry.runtime_data.coordinator.data = [
        normalize_parcel(raw, DELIVERED_CODE, include_history=True)
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    parcel = result["incoming"][0]
    assert parcel["barcode"] == "**REDACTED**"
    assert parcel["url"] == "**REDACTED**"
    assert parcel["raw"] == "**REDACTED**"
    assert parcel["history"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert parcel["status"] == "delivered"
    assert parcel["raw_status"] == "Delivery complete"
    dumped = str(result)
    for value in (DELIVERED_CODE, PHOTO, "Test Driver", "Testville", "Front porch"):
        assert value not in dumped


def test_every_sensitive_payload_field_is_listed():
    raw = delivered_sample()
    fields = set(raw["trackingInformation"]) | set(raw["trackingEvents"][0])
    fields |= {"mapInfo"}
    harmless = {"id", "isException", "isAgent", "suppressOnCompletion"}
    assert fields - harmless <= TO_REDACT


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
