"""The Discord Assist integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp
import discord
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .bot import DiscordAssistClient
from .const import (
    CONF_PLACEHOLDER_DELAY,
    CONF_TOKEN,
    DEFAULT_PLACEHOLDER_DELAY,
    DOMAIN,
    ISSUE_DUPLICATE_TOKEN,
    ISSUE_MESSAGE_CONTENT_INTENT,
    ISSUE_NO_AUTHORIZED_USERS,
    LEGACY_DOMAIN,
    RECONNECT_DELAY,
    issue_id,
    signal_connection,
)
from .conversation_router import ConversationRouter
from .notes import NotesStore
from .recent_errors import RecentErrors

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]


@dataclass
class RuntimeData:
    """Per-entry runtime objects."""

    client: DiscordAssistClient
    router: ConversationRouter
    notes_store: NotesStore | None = None
    recent_errors: RecentErrors = field(default_factory=RecentErrors)


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


def _legacy_entry_uses_same_token(hass: HomeAssistant, token: str) -> bool:
    """Whether an enabled legacy entry is loaded with the same bot token.

    Home Assistant loads every enabled config entry at startup, so an
    enabled Discord Conversation entry with this token will run the same
    bot in a second process. A disabled entry (for example the one left
    behind by the import flow in the config flow) is not running and
    does not count.
    """
    for legacy in hass.config_entries.async_entries(LEGACY_DOMAIN):
        if getattr(legacy, "disabled_by", None) is not None:
            continue
        if legacy.data.get(CONF_TOKEN) == token:
            return True
    return False


async def async_setup_entry(hass: HomeAssistant, entry: DiscordConfigEntry) -> bool:
    """Set up Discord Assist from a config entry."""
    notes_store = NotesStore(hass, entry.entry_id)
    await notes_store.async_load()
    router = ConversationRouter.from_entry(hass, entry, notes_store=notes_store)
    if not router.has_authorized_users:
        _LOGGER.warning(
            "No Discord users are authorized for %s; the bot will ignore everyone "
            "until you map a user or set an allowlist with a fallback user",
            entry.title,
        )
    _set_issue(hass, entry, ISSUE_NO_AUTHORIZED_USERS, not router.has_authorized_users)
    _set_issue(hass, entry, ISSUE_MESSAGE_CONTENT_INTENT, False)
    _set_issue(
        hass,
        entry,
        ISSUE_DUPLICATE_TOKEN,
        _legacy_entry_uses_same_token(hass, entry.data.get(CONF_TOKEN, "")),
    )

    recent_errors = RecentErrors()
    client = DiscordAssistClient(
        hass,
        router,
        signal_connection(entry.entry_id),
        placeholder_delay=float(
            entry.options.get(CONF_PLACEHOLDER_DELAY, DEFAULT_PLACEHOLDER_DELAY)
        ),
        recent_errors=recent_errors,
        entry_title=entry.title,
    )
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

    entry.runtime_data = RuntimeData(
        client=client,
        router=router,
        recent_errors=recent_errors,
        notes_store=notes_store,
    )

    async def _run() -> None:
        try:
            await client.connect(reconnect=True)
        except discord.PrivilegedIntentsRequired as err:
            recent_errors.record(err, source="gateway")
            _LOGGER.error(
                "Discord refused the Message Content intent; enable it for the "
                "bot in the Discord Developer Portal, then reload the entry"
            )
            _set_issue(hass, entry, ISSUE_MESSAGE_CONTENT_INTENT, True)
            return
        except Exception as err:  # noqa: BLE001
            recent_errors.record(err, source="gateway")
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
    """Clean up repair issues and stored notes when the entry is deleted."""
    await NotesStore(hass, entry.entry_id).async_remove()
    for kind in (
        ISSUE_NO_AUTHORIZED_USERS,
        ISSUE_MESSAGE_CONTENT_INTENT,
        ISSUE_DUPLICATE_TOKEN,
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry.entry_id))
