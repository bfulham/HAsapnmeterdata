"""Pure helpers for per-meter NEM12 channel configuration."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    CHANNEL_TYPE_CONSUMPTION,
    CHANNEL_TYPE_IGNORE,
    CHANNEL_TYPE_RETURN,
    CHANNEL_TYPES,
    CONF_CHANNEL_NAME,
    CONF_CHANNEL_TYPE,
    DEFAULT_CONSUMPTION_CHANNELS,
    DEFAULT_RETURN_CHANNELS,
)


def parse_patterns(value: str | Iterable[str]) -> tuple[str, ...]:
    """Return normalized, non-empty channel patterns."""
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    else:
        parts = [str(part) for part in value]
    return tuple(part.strip().upper() for part in parts if part.strip())


def _matches(channel: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a channel matches any configured glob."""
    normalized = channel.upper()
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def default_channel_type(
    channel: str,
    consumption_patterns: str | Iterable[str] = DEFAULT_CONSUMPTION_CHANNELS,
    return_patterns: str | Iterable[str] = DEFAULT_RETURN_CHANNELS,
) -> str:
    """Infer the safest default use for one NEM12 channel."""
    if _matches(channel, parse_patterns(return_patterns)):
        return CHANNEL_TYPE_RETURN
    if _matches(channel, parse_patterns(consumption_patterns)):
        return CHANNEL_TYPE_CONSUMPTION
    return CHANNEL_TYPE_IGNORE


def default_channel_definition(
    channel: str,
    consumption_patterns: str | Iterable[str] = DEFAULT_CONSUMPTION_CHANNELS,
    return_patterns: str | Iterable[str] = DEFAULT_RETURN_CHANNELS,
) -> dict[str, str]:
    """Return a user-editable definition for one discovered channel."""
    normalized = str(channel).strip().upper()
    return {
        CONF_CHANNEL_NAME: normalized,
        CONF_CHANNEL_TYPE: default_channel_type(
            normalized,
            consumption_patterns,
            return_patterns,
        ),
    }


def normalize_channel_config(
    value: Any,
) -> dict[str, dict[str, dict[str, str]]]:
    """Validate and normalize a complete NMI/channel configuration."""
    if not isinstance(value, Mapping):
        return {}

    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for raw_nmi, raw_channels in value.items():
        if not isinstance(raw_channels, Mapping):
            continue
        nmi = str(raw_nmi)
        meter_channels: dict[str, dict[str, str]] = {}
        for raw_channel, raw_definition in raw_channels.items():
            if not isinstance(raw_definition, Mapping):
                continue
            channel = str(raw_channel).strip().upper()
            if not channel:
                continue
            name = str(raw_definition.get(CONF_CHANNEL_NAME, channel)).strip()
            channel_type = str(
                raw_definition.get(
                    CONF_CHANNEL_TYPE,
                    default_channel_type(channel),
                )
            )
            if channel_type not in CHANNEL_TYPES:
                channel_type = CHANNEL_TYPE_IGNORE
            meter_channels[channel] = {
                CONF_CHANNEL_NAME: name or channel,
                CONF_CHANNEL_TYPE: channel_type,
            }
        normalized[nmi] = meter_channels
    return normalized


def merge_channel_config(
    nmis: Iterable[str],
    discovered: Mapping[str, Iterable[str]],
    *sources: Any,
    consumption_patterns: str | Iterable[str] = DEFAULT_CONSUMPTION_CHANNELS,
    return_patterns: str | Iterable[str] = DEFAULT_RETURN_CHANNELS,
) -> dict[str, dict[str, dict[str, str]]]:
    """Merge saved definitions and add defaults for newly found channels."""
    nmi_list = [str(nmi) for nmi in nmis]
    merged = {nmi: {} for nmi in nmi_list}
    for source in sources:
        normalized = normalize_channel_config(source)
        for nmi in nmi_list:
            merged[nmi].update(normalized.get(nmi, {}))

    for nmi in nmi_list:
        for raw_channel in discovered.get(nmi, ()):
            channel = str(raw_channel).strip().upper()
            if not channel:
                continue
            merged[nmi].setdefault(
                channel,
                default_channel_definition(
                    channel,
                    consumption_patterns,
                    return_patterns,
                ),
            )
    return merged


def enabled_channels(
    channel_config: Mapping[str, Mapping[str, str]],
) -> tuple[str, ...]:
    """Return configured channels that should become statistics."""
    return tuple(
        channel
        for channel, definition in channel_config.items()
        if definition.get(CONF_CHANNEL_TYPE) != CHANNEL_TYPE_IGNORE
    )
