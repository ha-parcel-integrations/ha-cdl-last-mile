"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cdl_last_mile import parcels as parcels_module
from custom_components.cdl_last_mile.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.cdl_last_mile.contents import CONTENTS_STORE_MAX_CODES
from custom_components.cdl_last_mile.coordinator import CDLLastMileSolutionsCoordinator
from custom_components.cdl_last_mile.parcels import normalize_parcel

from .payloads import ACTIVE_CODE, active_sample, delivered_sample

CLIENT = "custom_components.cdl_last_mile.api.CDLLastMileSolutionsApiClient"
REFRESH = (
    f"{CDLLastMileSolutionsCoordinator.__module__}.CDLLastMileSolutionsCoordinator"
)
UNTRACKED = "CDL00000000099"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _config_entry(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, options={CONF_PARCELS: parcels}
    )


async def _setup(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = _config_entry([{CONF_TRACKING_CODE: code} for code in codes or []])
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> None:
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code, *_: active_sample(code)),
    ):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Winter socks"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "  "}
    )
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_trims_and_matches_the_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: f"  {ACTIVE_CODE.lower()} ", "contents": "Running shoes"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert entry.runtime_data.coordinator.contents_store.get(UNTRACKED) == (
        "Running shoes"
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_update_contents_never_polls_and_notifies_once_per_change(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: UNTRACKED}, blocking=True
        )
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE}, blocking=True
        )
        assert listeners.call_count == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_rejects_an_invalid_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: ""})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert _contents(entry, ACTIVE_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    track = {CONF_TRACKING_CODE: ACTIVE_CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_parcel_on_a_tracked_code_never_polls(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
        await hass.services.async_call(DOMAIN, "track_parcel", data, blocking=True)
    assert listeners.call_count == 1
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_a_newly_tracked_code_still_refreshes_via_the_options_listener(hass):
    entry = await _setup(hass)
    fetch = AsyncMock(side_effect=lambda code, *_: active_sample(code))
    with patch(f"{CLIENT}.async_get_parcel", new=fetch):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
            blocking=True,
        )
        await hass.async_block_till_done()
    fetch.assert_awaited()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_reapply_contents_covers_delivered_parcels(hass):
    entry = _config_entry([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=delivered_sample(ACTIVE_CODE)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    coordinator.delivered = [
        normalize_parcel(delivered_sample(ACTIVE_CODE), ACTIVE_CODE)
    ]

    coordinator.contents_store.async_set(ACTIVE_CODE, "Running shoes")
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] == "Running shoes"

    coordinator.contents_store.async_set(ACTIVE_CODE, None)
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] is None


async def test_a_barcode_less_parcel_keeps_its_contents_after_reapply_and_a_poll(hass):
    """The store is keyed on the requested code, not the published barcode."""
    real = parcels_module.normalize_parcel

    def without_barcode(raw, code, **kwargs):
        parcel = real(raw, code, **kwargs)
        parcel["barcode"] = None
        return parcel

    entry = _config_entry([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.cdl_last_mile.coordinator.normalize_parcel",
            new=without_barcode,
        ),
        patch(
            f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        assert coordinator.data[0]["barcode"] is None

        coordinator.contents_store.async_set(ACTIVE_CODE, "Running shoes")
        coordinator.async_reapply_contents()
        assert coordinator.data[0]["contents"] == "Running shoes"

        await coordinator.async_refresh()
        assert coordinator.data[0]["contents"] == "Running shoes"


async def test_a_code_the_response_republishes_differently_keeps_its_contents(hass):
    """A response whose barcode differs from the requested code still matches."""
    real = parcels_module.normalize_parcel

    def other_barcode(raw, code, **kwargs):
        parcel = real(raw, code, **kwargs)
        parcel["barcode"] = "CDL00000000077"
        return parcel

    entry = _config_entry([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.cdl_last_mile.coordinator.normalize_parcel",
            new=other_barcode,
        ),
        patch(
            f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        coordinator.contents_store.async_set(ACTIVE_CODE, "Running shoes")
        coordinator.async_reapply_contents()
        assert coordinator.data[0]["contents"] == "Running shoes"


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_on_an_unloaded_entry_still_untracks(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE}, blocking=True
    )
    assert entry.options[CONF_PARCELS] == []
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_store_drops_the_oldest_code_past_the_cap(hass):
    entry = await _setup(hass)
    store = entry.runtime_data.coordinator.contents_store
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"CDL{number:011d}", "Running shoes")
    assert store.get("CDL00000000000") is None
    assert store.get("CDL00000000001") == "Running shoes"
    assert store.get(f"CDL{CONTENTS_STORE_MAX_CODES:011d}") == "Running shoes"


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert hass_storage[STORE_KEY % entry.entry_id]["data"] == {
        "by_code": {ACTIVE_CODE: "Running shoes"}
    }

    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_starts_empty(hass, hass_storage):
    assert not hass_storage
    entry = await _setup(hass, [ACTIVE_CODE])
    assert _contents(entry, ACTIVE_CODE) is None


async def test_removing_the_entry_deletes_the_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert STORE_KEY % entry.entry_id in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert STORE_KEY % entry.entry_id not in hass_storage
