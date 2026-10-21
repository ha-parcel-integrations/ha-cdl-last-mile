"""Tests for the CDL Last Mile Solutions API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.cdl_last_mile import api as api_module
from custom_components.cdl_last_mile.api import (
    CDLLastMileSolutionsApiClient,
    CDLLastMileSolutionsApiError,
)
from custom_components.cdl_last_mile.const import MAX_RESPONSE_BYTES

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

BASE = "https://apps.cdldelivers.com/Tracking-Page-API/Tracking/details"


@pytest.fixture(autouse=True)
def _reset_mismatch_flag():
    api_module._mismatch_logged = False
    yield
    api_module._mismatch_logged = False


def _session_returning(
    status: int, body: object = None, headers: dict | None = None
) -> MagicMock:
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif body is None:
        data = b""
    elif isinstance(body, str):
        data = body.encode()
    else:
        data = json.dumps(body).encode()

    async def iter_chunked(size):
        for start in range(0, len(data), size):
            yield data[start : start + size]

    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.content.iter_chunked = iter_chunked
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_returns_the_payload():
    session = _session_returning(200, delivered_sample())
    parcel = await CDLLastMileSolutionsApiClient(session).async_get_parcel(
        DELIVERED_CODE
    )

    assert parcel["trackingInformation"]["referenceNumber"] == DELIVERED_CODE
    kwargs = session.get.call_args.kwargs
    assert kwargs["headers"] == {"Accept": "application/json"}
    assert kwargs["allow_redirects"] is False


async def test_label_number_is_sent_as_is():
    session = _session_returning(200, active_sample())
    await CDLLastMileSolutionsApiClient(session).async_get_parcel(ACTIVE_CODE)
    assert session.get.call_args.args[0] == f"{BASE}?value={ACTIVE_CODE}"


@pytest.mark.parametrize("sent", ["900000001-2", "9000000012"])
async def test_order_number_is_always_sent_with_its_hyphen(sent):
    session = _session_returning(200, delivered_sample("900000001-2"))
    parcel = await CDLLastMileSolutionsApiClient(session).async_get_parcel(sent)

    assert session.get.call_args.args[0] == f"{BASE}?value=900000001-2"
    assert parcel is not None


async def test_query_value_is_url_encoded():
    session = _session_returning(204)
    await CDLLastMileSolutionsApiClient(session).async_get_parcel("a b&c=d/é")
    assert session.get.call_args.args[0] == f"{BASE}?value=A%20B%26C%3DD%2F%C3%89"


async def test_204_is_not_found():
    client = CDLLastMileSolutionsApiClient(_session_returning(204))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


async def test_reference_comparison_is_case_insensitive():
    payload = active_sample(ACTIVE_CODE.lower())
    client = CDLLastMileSolutionsApiClient(_session_returning(200, payload))
    assert await client.async_get_parcel(ACTIVE_CODE) is not None


async def test_reference_mismatch_is_inconclusive_and_warns_once(caplog):
    client = CDLLastMileSolutionsApiClient(
        _session_returning(200, active_sample("CDL99999999999"))
    )
    assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert caplog.text.count("different reference") == 1
    assert "CDL99999999999" not in caplog.text


async def test_missing_reference_is_inconclusive():
    payload = active_sample()
    del payload["trackingInformation"]["referenceNumber"]
    client = CDLLastMileSolutionsApiClient(_session_returning(200, payload))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


@pytest.mark.parametrize("status", [404, 410, 500, 503, 302, 301, 403])
async def test_other_statuses_are_transport_errors(status):
    client = CDLLastMileSolutionsApiClient(_session_returning(status))
    with pytest.raises(CDLLastMileSolutionsApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == status


async def test_429_carries_retry_after():
    client = CDLLastMileSolutionsApiClient(
        _session_returning(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(CDLLastMileSolutionsApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("header", [None, "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_429_without_usable_retry_after(header):
    headers = {"Retry-After": header} if header else {}
    client = CDLLastMileSolutionsApiClient(_session_returning(429, headers=headers))
    with pytest.raises(CDLLastMileSolutionsApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.retry_after is None


@pytest.mark.parametrize(
    "body",
    [
        "<!doctype html><html><body>tracker</body></html>",
        "",
        "null",
        "[]",
        "{}",
        json.dumps({"trackingEvents": [], "trackingInformation": None}),
        json.dumps({"trackingEvents": None, "trackingInformation": {}}),
    ],
)
async def test_indeterminate_200_bodies_raise(body):
    client = CDLLastMileSolutionsApiClient(_session_returning(200, body))
    with pytest.raises(CDLLastMileSolutionsApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_oversized_body_is_refused():
    client = CDLLastMileSolutionsApiClient(
        _session_returning(200, b"x" * (MAX_RESPONSE_BYTES + 1))
    )
    with pytest.raises(CDLLastMileSolutionsApiError, match="too large"):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_large_delivered_response_under_the_cap_is_read():
    payload = delivered_sample()
    payload["trackingEvents"][0]["vpod"] = "A" * (1024 * 1024)
    client = CDLLastMileSolutionsApiClient(_session_returning(200, payload))
    parcel = await client.async_get_parcel(DELIVERED_CODE)
    assert len(parcel["trackingEvents"][0]["vpod"]) == 1024 * 1024


async def test_network_error_propagates():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await CDLLastMileSolutionsApiClient(session).async_get_parcel(ACTIVE_CODE)
