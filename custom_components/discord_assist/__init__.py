"""The Discord Assist integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp
import discord
from homeassistant.components.intent import async_register_timer_handler
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType

from .bot import DiscordAssistClient
from .const import (
    CONF_TOKEN,
    DOMAIN,
    ISSUE_MESSAGE_CONTENT_INTENT,
    ISSUE_NO_AUTHORIZED_USERS,
    RECONNECT_DELAY,
    issue_id,
    signal_connection,
)
from .conversation_router import ConversationRouter
from .timers import TimerTracker

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]


@dataclass
class RuntimeData:
    """Per-entry runtime objects."""

    client: DiscordAssistClient
    router: ConversationRouter
    device_id: str
    timers: TimerTracker


type DiscordConfigEntry = ConfigEntry[RuntimeData]


def _set_issue(hass: HomeAssistant, entry: ConfigEntry, kind: str, active: bool):
    """Raise or clear one of this entry's repair issues."""
    if active:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(kind, entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={"entry_title": entry.title},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry.entry_id))


async def async_setup_entry(hass: HomeAssistant, entry: DiscordConfigEntry) -> bool:
    """Set up Discord Assist from a config entry."""
    # The integration's own device for this entry (the same device the
    # gateway sensor attaches to). Channels that are not mapped to a room
    # send their requests as this device, and its registered timer handler
    # is what makes Assist timers work for those channels. It is not
    # assigned to an area: an unmapped channel has no knowable room, and
    # room-mapped channels keep using their room device instead.
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(entry.unique_id))},
        name=entry.title,
        manufacturer="Discord",
        entry_type=DeviceEntryType.SERVICE,
    )

    router = ConversationRouter.from_entry(hass, entry)
    router.integration_device_id = device.id
    if not router.has_authorized_users:
        _LOGGER.warning(
            "No Discord users are authorized for %s; the bot will ignore everyone "
            "until you map a user or set an allowlist with a fallback user",
            entry.title,
        )
    _set_issue(hass, entry, ISSUE_NO_AUTHORIZED_USERS, not router.has_authorized_users)
    _set_issue(hass, entry, ISSUE_MESSAGE_CONTENT_INTENT, False)

    client = DiscordAssistClient(hass, router, signal_connection(entry.entry_id))
    # Log in over REST first so a dead network or bad token surfaces here,
    # where Home Assistant retries with backoff or starts reauth.
    try:
        await client.login(entry.data[CONF_TOKEN])
    except discord.LoginFailure as err:
        await client.close()
        raise ConfigEntryAuthFailed("Discord rejected the bot token") from err
    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as err:
        await client.close()
        raise ConfigEntryNotReady(f"Could not reach Discord: {err}") from err

    timers = TimerTracker(
        hass, client.send_timer_message, timer_mention=router.timer_mention
    )
    router.timer_tracker = timers
    try:
        entry.async_on_unload(
            async_register_timer_handler(hass, device.id, timers.handle_event)
        )
    except KeyError:
        # The intent component provides the timer manager; it is a declared
        # dependency, so this should not happen, but timers failing must not
        # take the whole entry down with them.
        _LOGGER.warning(
            "Timer manager is not available for %s; Assist timers are disabled",
            entry.title,
        )

    entry.runtime_data = RuntimeData(
        client=client, router=router, device_id=device.id, timers=timers
    )

    async def _run() -> None:
        try:
            await client.connect(reconnect=True)
        except discord.PrivilegedIntentsRequired:
            _LOGGER.error(
                "Discord refused the Message Content intent; enable it for the "
                "bot in the Discord Developer Portal, then reload the entry"
            )
            _set_issue(hass, entry, ISSUE_MESSAGE_CONTENT_INTENT, True)
            return
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Discord gateway task failed")
        if client.is_closed():
            return
        # discord.py handles routine reconnects itself; reaching here means
        # it gave up. Reload so setup retries with Home Assistant's backoff.
        _LOGGER.warning(
            "Discord gateway stopped; reloading %s in %s seconds",
            entry.title,
            RECONNECT_DELAY,
        )
        await asyncio.sleep(RECONNECT_DELAY)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_create_background_task(
        hass, _run(), name=f"{DOMAIN}-gateway-{entry.entry_id}"
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DiscordConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.client.close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up repair issues when the entry is deleted."""
    for kind in (ISSUE_NO_AUTHORIZED_USERS, ISSUE_MESSAGE_CONTENT_INTENT):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry.entry_id))
