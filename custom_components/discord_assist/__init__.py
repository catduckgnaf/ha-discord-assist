"""The Discord Assist integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp
import discord
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .bot import DiscordAssistClient
from .button_store import ButtonStore
from .const import (
    CONF_TOKEN,
    DOMAIN,
    ISSUE_MESSAGE_CONTENT_INTENT,
    ISSUE_NO_AUTHORIZED_USERS,
    RECONNECT_DELAY,
    SERVICE_RETIRE_MESSAGE,
    issue_id,
    signal_connection,
)
from .conversation_router import ConversationRouter
from .retire import PRUNE_INTERVAL, async_prune_expired, async_retire_taken

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]

RETIRE_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("channel_id"): vol.Coerce(int),
        vol.Required("message_id"): vol.Coerce(int),
        vol.Optional("outcome"): cv.string,
    }
)


@dataclass
class RuntimeData:
    """Per-entry runtime objects."""

    client: DiscordAssistClient
    router: ConversationRouter
    button_store: ButtonStore


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


def _async_register_retire_service(hass: HomeAssistant) -> None:
    """Register the domain-wide retire_message service once."""
    if hass.services.has_service(DOMAIN, SERVICE_RETIRE_MESSAGE):
        return

    async def _retire_message(call: ServiceCall) -> None:
        entry_id = call.data["config_entry_id"]
        entry = hass.config_entries.async_get_entry(entry_id)
        runtime = getattr(entry, "runtime_data", None) if entry else None
        if entry is None or entry.domain != DOMAIN or runtime is None:
            raise ServiceValidationError(
                f"No Discord Assist config entry with id {entry_id}"
            )
        store: ButtonStore = runtime.button_store
        record = store.get(call.data["message_id"])
        if record is None or record.channel_id != call.data["channel_id"]:
            raise ServiceValidationError(
                "No button message with that channel and message id is "
                "being tracked for this entry"
            )
        retired = await async_retire_taken(
            store,
            runtime.client,
            record.message_id,
            call.data.get("outcome"),
        )
        if not retired:
            raise ServiceValidationError(
                "The tracked Discord message could not be found to retire it"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RETIRE_MESSAGE,
        _retire_message,
        schema=RETIRE_MESSAGE_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: DiscordConfigEntry) -> bool:
    """Set up Discord Assist from a config entry."""
    router = ConversationRouter.from_entry(hass, entry)
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

    button_store = ButtonStore(hass, entry.entry_id)
    await button_store.async_load()
    entry.runtime_data = RuntimeData(
        client=client, router=router, button_store=button_store
    )

    async def _prune_expired(_now) -> None:
        await async_prune_expired(button_store, client)

    entry.async_on_unload(
        async_track_time_interval(
            hass, _prune_expired, PRUNE_INTERVAL, cancel_on_shutdown=True
        )
    )
    _async_register_retire_service(hass)

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
