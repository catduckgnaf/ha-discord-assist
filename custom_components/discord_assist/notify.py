"""Notify entities for Discord Assist: one per configured channel."""

from __future__ import annotations

import logging
from typing import Any

import discord
from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DiscordConfigEntry
from .const import DOMAIN, signal_connection
from .text_utils import chunk_message

_LOGGER = logging.getLogger(__name__)


def _resolve_channel(client: Any, channel_id: int) -> Any | None:
    """Find a channel in the running client's cache, or None.

    discord.py keeps every guild channel the bot can see in its cache once
    the gateway is connected (``Client.get_channel``); guild objects expose
    the same lookup, so scan them too for clients whose top-level cache is
    not populated yet.
    """
    get_channel = getattr(client, "get_channel", None)
    if callable(get_channel):
        try:
            channel = get_channel(channel_id)
        except Exception:  # noqa: BLE001 - a bad cache lookup means unknown
            channel = None
        if channel is not None:
            return channel
    for guild in getattr(client, "guilds", None) or []:
        guild_get_channel = getattr(guild, "get_channel", None)
        if callable(guild_get_channel):
            try:
                channel = guild_get_channel(channel_id)
            except Exception:  # noqa: BLE001
                channel = None
            if channel is not None:
                return channel
        for channel in getattr(guild, "channels", None) or []:
            if getattr(channel, "id", None) == channel_id:
                return channel
    return None


def _channel_name(client: Any, channel_id: int) -> str:
    """Best known name for a channel, falling back to its id.

    The router only stores channel ids, so the display name has to come
    from the client's channel cache. Before the gateway connects (or for a
    channel the bot can no longer see) the id itself is the sensible name.
    """
    channel = _resolve_channel(client, channel_id)
    name = getattr(channel, "name", None) if channel is not None else None
    if isinstance(name, str) and name:
        return name
    return str(channel_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DiscordConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one notify entity per configured channel.

    Options changes reload the entry (the options flow is
    ``OptionsFlowWithReload``), so this runs again with the new channel set:
    entities for added channels are created and entities for removed
    channels disappear with the unload.
    """
    client = entry.runtime_data.client
    channels = sorted(entry.runtime_data.router.channels)
    _remove_stale_entities(hass, entry, set(channels))
    names = {
        channel_id: await _fetch_channel_name(client, channel_id)
        for channel_id in channels
    }
    async_add_entities(
        [
            DiscordChannelNotifyEntity(entry, channel_id, names[channel_id])
            for channel_id in channels
        ]
    )


def _unique_id(entry: DiscordConfigEntry, channel_id: int) -> str:
    return f"{entry.unique_id}_notify_{channel_id}"


def _remove_stale_entities(
    hass: HomeAssistant, entry: DiscordConfigEntry, channels: set[int]
) -> None:
    """Drop registry entries for channels no longer in the options."""
    registry = er.async_get(hass)
    wanted = {_unique_id(entry, channel_id) for channel_id in channels}
    prefix = f"{entry.unique_id}_notify_"
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "notify"
            and reg_entry.unique_id.startswith(prefix)
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)


async def _fetch_channel_name(client: Any, channel_id: int) -> str:
    """Channel name for the first registration, over REST if needed.

    Platforms are set up before the gateway has filled the channel cache,
    and the first name decides the entity_id, so ask Discord directly (the
    client is already logged in) instead of registering the numeric id.
    """
    name = _channel_name(client, channel_id)
    if name != str(channel_id):
        return name
    fetch = getattr(client, "fetch_channel", None)
    if callable(fetch):
        try:
            channel = await fetch(channel_id)
        except Exception:  # noqa: BLE001 - unknown channel: fall back to the id
            _LOGGER.debug("Could not fetch Discord channel %s", channel_id)
        else:
            fetched = getattr(channel, "name", None)
            if isinstance(fetched, str) and fetched:
                return fetched
    return name


class DiscordChannelNotifyEntity(NotifyEntity):
    """Sends ``notify.send_message`` calls to one Discord channel."""

    _attr_has_entity_name = True
    _attr_supported_features = NotifyEntityFeature.TITLE
    _attr_should_poll = False

    def __init__(
        self, entry: DiscordConfigEntry, channel_id: int, name: str | None = None
    ) -> None:
        self._client = entry.runtime_data.client
        self._channel_id = channel_id
        self._signal = signal_connection(entry.entry_id)
        self._attr_unique_id = _unique_id(entry, channel_id)
        self._attr_name = name or _channel_name(self._client, channel_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=entry.title,
            manufacturer="Discord",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def channel_id(self) -> int:
        """Discord channel id this entity posts to."""
        return self._channel_id

    @property
    def available(self) -> bool:
        """Available only while the bot holds a gateway connection."""
        return bool(getattr(self._client, "connected", False))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._signal, self._connection_changed)
        )

    @callback
    def _connection_changed(self) -> None:
        # The channel cache fills in once the gateway connects, so a name
        # that fell back to the channel id at setup can resolve lazily here.
        name = _channel_name(self._client, self._channel_id)
        if name != str(self._channel_id):
            self._attr_name = name
        self.async_write_ha_state()

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Post the message to this entity's channel, chunked for Discord.

        The title, when given, becomes a bold first line. Mentions are
        disabled on every piece: notification text is caller supplied and
        must never ping @everyone, roles, or users.
        """
        text = f"**{title}**\n{message}" if title else message
        channel = _resolve_channel(self._client, self._channel_id)
        send = getattr(channel, "send", None) if channel is not None else None
        if not callable(send):
            _LOGGER.error(
                "Cannot send notification to Discord channel %s: the channel "
                "is not known to the bot (not connected, or the bot is not "
                "in that channel)",
                self._channel_id,
            )
            raise HomeAssistantError(
                f"Discord channel {self._channel_id} is not available; "
                "the bot is disconnected or cannot see that channel"
            )
        # Through the client's recording send path, so the duplicate-token
        # check knows these bot-authored messages are ours.
        send_recorded = getattr(self._client, "send_to_channel", None)
        for part in chunk_message(text):
            try:
                if callable(send_recorded):
                    await send_recorded(
                        channel, part, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    await send(part, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as err:
                _LOGGER.warning(
                    "Failed to send notification to channel %s: %s",
                    self._channel_id,
                    err,
                )
                raise HomeAssistantError(
                    f"Failed to send notification to Discord channel "
                    f"{self._channel_id}: {err}"
                ) from err
