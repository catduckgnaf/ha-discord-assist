"""Diagnostics support for Discord Assist.

The download is built to be safe to share: the bot token and every stored
id (Discord user ids, HA user ids, channel ids, room device ids, the bot
id) are redacted, and recent errors carry only a type, a source, and a
timestamp, never an exception message.
"""

from __future__ import annotations

from typing import Any

import discord
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOWLIST,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_FALLBACK_USER,
    CONF_ROOM_MAP,
    CONF_TOKEN,
    CONF_USER_MAP,
)

TO_REDACT = {
    CONF_TOKEN,
    CONF_BOT_ID,
    CONF_USER_MAP,
    CONF_ALLOWLIST,
    CONF_FALLBACK_USER,
    CONF_CHANNELS,
    CONF_ROOM_MAP,
}


def _intents_in_use(client: Any) -> list[str]:
    """Names of the gateway intents the client was created with."""
    intents = getattr(client, "intents", None)
    if intents is None:
        return []
    try:
        return sorted(name for name, enabled in intents if enabled)
    except TypeError:
        return []


def _guild_and_channel_counts(client: Any) -> tuple[int, int]:
    """(guilds the bot can see, channels across those guilds)."""
    if client is None:
        return 0, 0
    try:
        guilds = list(getattr(client, "guilds", None) or [])
    except TypeError:
        guilds = []
    try:
        channels = list(client.get_all_channels())
    except Exception:  # noqa: BLE001 - fall back to per-guild channel lists
        channels = [
            channel
            for guild in guilds
            for channel in (getattr(guild, "channels", None) or [])
        ]
    return len(guilds), len(channels)


def _cache_size(cache: Any) -> int:
    if cache is None:
        return 0
    try:
        return len(cache)
    except TypeError:
        store = getattr(cache, "_store", None)
        return len(store) if store is not None else 0


def _turn_lock_count(turns: Any) -> int:
    if turns is None:
        return 0
    try:
        return len(turns)
    except TypeError:
        return 0


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry, with secrets redacted."""
    runtime = getattr(entry, "runtime_data", None)
    client = getattr(runtime, "client", None)
    router = getattr(runtime, "router", None)
    recent_errors = getattr(runtime, "recent_errors", None)
    if recent_errors is None:
        recent_errors = getattr(client, "recent_errors", None)
    guild_count, channel_count = _guild_and_channel_counts(client)
    state = getattr(entry, "state", None)

    return {
        "entry": {
            "title": entry.title,
            "state": getattr(state, "value", state),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "connection": {
            "connected": bool(getattr(client, "connected", False)),
            "discord_py_version": discord.__version__,
            "intents": _intents_in_use(client),
            "guild_count": guild_count,
            "channel_count": channel_count,
        },
        "conversation": {
            "cache_size": _cache_size(getattr(router, "cache", None)),
            "turn_lock_count": _turn_lock_count(getattr(router, "turns", None)),
        },
        "recent_errors": recent_errors.snapshot() if recent_errors is not None else [],
    }
