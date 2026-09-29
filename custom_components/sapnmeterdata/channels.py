"""Per-meter NEM12 channel settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CHANNEL_CONSUMPTION,
    CHANNEL_IGNORE,
    CHANNEL_RETURN,
    CHANNEL_TYPES,
    CONF_CHANNEL_NAME,
    CONF_CHANNEL_TYPE,
    CONF_CHANNELS,
)


def default_channel_type(channel: str) -> str:
    """Return the default use of a NEM12 channel suffix.

    E channels are energy imported from the grid and B channels are energy
    exported to it. Other suffixes (such as reactive energy) are ignored.
    """
    if channel.startswith("E"):
        return CHANNEL_CONSUMPTION
    if channel.startswith("B"):
        return CHANNEL_RETURN
    return CHANNEL_IGNORE


def channel_settings(
    options: Mapping[str, Any],
    nmi: str,
    channel: str,
) -> tuple[str, str]:
    """Return the configured ``(name, type)`` of a channel, or its defaults."""
    configured = options.get(CONF_CHANNELS, {}).get(nmi, {}).get(channel, {})
    name = str(configured.get(CONF_CHANNEL_NAME) or channel).strip() or channel
    channel_type = configured.get(CONF_CHANNEL_TYPE)
    if channel_type not in CHANNEL_TYPES:
        channel_type = default_channel_type(channel)
    return name, channel_type


def enabled_channels(
    options: Mapping[str, Any],
    nmi: str,
    seen: set[str] | frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Return ``{channel: name}`` for every channel that should be imported.

    Channels configured by the user are always considered. Channels that
    appear in SAPN's data later (``seen``) are included using their defaults.
    """
    candidates = set(options.get(CONF_CHANNELS, {}).get(nmi, {})) | set(seen)
    enabled: dict[str, str] = {}
    for channel in sorted(candidates):
        name, channel_type = channel_settings(options, nmi, channel)
        if channel_type != CHANNEL_IGNORE:
            enabled[channel] = name
    return enabled
