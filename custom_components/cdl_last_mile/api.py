"""CDL Last Mile Solutions public tracking API client."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from .codes import normalize_tracking_code
from .const import MAX_RESPONSE_BYTES, TRACKING_API_URL

_LOGGER = logging.getLogger(__name__)

# One-shot so a permanently mismatching code does not log on every poll.
_mismatch_logged = False


class CDLLastMileSolutionsApiError(Exception):
    """Raised when a CDL Last Mile Solutions API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"CDL Last Mile Solutions API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class CDLLastMileSolutionsApiClient:
    """Client for the public CDL Last Mile Solutions tracking endpoint.

    No authentication: the endpoint is keyed on the tracking code alone. A
    known parcel answers ``200`` with JSON, an unknown code an empty ``204``.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parcel dict, or ``None`` when the code is unknown or the
        response belongs to a different reference. Anything indeterminate
        (redirect, HTML, empty body, route gone) raises
        :class:`CDLLastMileSolutionsApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        value = normalize_tracking_code(tracking_code)
        url = TRACKING_API_URL.format(value=quote(value, safe=""))
        async with self._session.get(
            url,
            headers={"Accept": "application/json"},
            allow_redirects=False,
        ) as response:
            status = response.status
            if status == 204:
                return None
            if status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise CDLLastMileSolutionsApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if status != 200:
                raise CDLLastMileSolutionsApiError(
                    f"HTTP {status}", status_code=status
                )
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CDLLastMileSolutionsApiError("response too large")

        try:
            payload = json.loads(bytes(body))
        except ValueError as err:
            raise CDLLastMileSolutionsApiError("unparseable body") from err

        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("trackingEvents"), list)
            or not isinstance(payload.get("trackingInformation"), dict)
        ):
            raise CDLLastMileSolutionsApiError("unexpected body shape")

        reference = payload["trackingInformation"].get("referenceNumber")
        if not isinstance(reference, str) or reference.strip().upper() != value:
            global _mismatch_logged
            if not _mismatch_logged:
                _mismatch_logged = True
                _LOGGER.warning(
                    "CDL Last Mile Solutions answered with a different reference "
                    "than requested; ignoring that response"
                )
            return None
        return payload
