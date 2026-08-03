"""Helpers for classifying SAPN meter assignments."""

from __future__ import annotations

from typing import Any

_INTERVAL_METER_MARKERS = ("interval", "smart", "advanced")
_NON_INTERVAL_METER_MARKERS = ("basic", "manual", "accumulation")


def supports_interval_data(assignment: Any) -> bool | None:
    """Return whether an SAPN assignment can provide interval NEM12 data.

    ``None`` preserves compatibility when SAPN omits the type description.
    Explicit basic/manual assignments are excluded, while interval, smart, and
    advanced descriptions are treated as interval-capable.
    """
    description = str(getattr(assignment, "meter_type_description", "") or "").strip()
    normalized = description.casefold()
    if any(marker in normalized for marker in _NON_INTERVAL_METER_MARKERS):
        return False
    if any(marker in normalized for marker in _INTERVAL_METER_MARKERS):
        return True
    return None


def meter_type_label(assignment: Any) -> str:
    """Return the best available portal meter-type label."""
    return (
        str(getattr(assignment, "meter_type_description", "") or "").strip()
        or str(getattr(assignment, "meter_type", "") or "").strip()
        or "Non-interval meter"
    )
