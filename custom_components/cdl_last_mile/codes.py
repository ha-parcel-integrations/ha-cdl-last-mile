"""Tracking-code shapes and the request value derived from them.

Kept free of Home Assistant imports so the API client and the config flow
share one definition.
"""

from __future__ import annotations

import re

_LABEL_RE = re.compile(r"^CDL\d{11}$")
_ORDER_RE = re.compile(r"^\d{9}-\d$")
_ORDER_WITHOUT_HYPHEN_RE = re.compile(r"^(\d{9})(\d)$")


def normalize_tracking_code(value: str | None) -> str:
    """Trim, upper-case and restore the hyphen of an order number.

    The service finds an order number only with its hyphen, so a ten-digit
    code typed (or pasted) without it must get the hyphen back before it is
    stored or sent.
    """
    code = (value or "").strip().upper()
    match = _ORDER_WITHOUT_HYPHEN_RE.match(code)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return code


def valid_tracking_code(value: str | None) -> bool:
    """Accept exactly a ``CDL`` label number or a hyphenated order number.

    The lookup field also holds shippers' own references, so any other shape
    could resolve to a stranger's parcel.
    """
    code = normalize_tracking_code(value)
    return bool(_LABEL_RE.match(code) or _ORDER_RE.match(code))
