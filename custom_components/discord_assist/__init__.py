"""The Discord Assist integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiohttp
import discord
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .approvals import ApprovalManager, ApprovalPolicy, ApprovalStore
from .bot import DiscordAssistClient
from .const import (
    CONF_TOKEN,
    DOMAIN,
    ISSUE_MESSAGE_CONTENT_INTENT,
    ISSUE_NO_AUTHORIZED_USERS,
    RECONNECT_DELAY,
    SERVICE_REQUEST_APPROVAL,
    issue_id,
    signal_connection,
)
from .conversation_router import ConversationRouter

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]

# How often pending approvals are checked for expiry.
APPROVAL_SWEEP_INTERVAL = timedelta(minutes=1)


@dataclass
class RuntimeData:
    """Per-entry runtime objects."""

    client: DiscordAssistClient
    router: ConversationRouter
    approvals: ApprovalManager


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


_REQUEST_APPROVAL_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("channel_id"): vol.Coerce(int),
        vol.Required("requester_discord_id"): vol.Coerce(int),
        vol.Required("domain"): str,
        vol.Required("service"): str,
        vol.Optional("service_data", default=dict): dict,
        vol.Optional("description", default=None): vol.Any(str, None),
        vol.Optional("approvers", default=list): [vol.Coerce(int)],
        vol.Optional("self_approve", default=None): vol.Any(bool, None),
        vol.Optional("ttl_minutes", default=None): vol.Any(vol.Coerce(float), None),
    }
)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services (once, however many entries exist)."""
    if hass.services.has_service(DOMAIN, SERVICE_REQUEST_APPROVAL):
        return

    async def _request_approval(call: ServiceCall) -> None:
        try:
            data: dict[str, Any] = _REQUEST_APPROVAL_SCHEMA(dict(call.data))
        except vol.Invalid as err:
            raise ServiceValidationError(f"Invalid approval request: {err}") from err
        entry = hass.config_entries.async_get_entry(data["config_entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                f"Discord Assist entry {data['config_entry_id']} is not loaded"
            )
        kwargs: dict[str, Any] = {
            "channel_id": data["channel_id"],
            "requester_discord_id": data["requester_discord_id"],
            "domain": data["domain"],
            "service": data["service"],
            "service_data": data["service_data"],
            "description": data["description"],
            "approvers": data["approvers"],
            "self_approve": data["self_approve"],
        }
        if data["ttl_minutes"] is not None:
            kwargs["ttl_minutes"] = data["ttl_minutes"]
        await entry.runtime_data.approvals.async_request_approval(**kwargs)

    hass.services.async_register(DOMAIN, SERVICE_REQUEST_APPROVAL, _request_approval)


async def async_setup_entry(hass: HomeAssistant, entry: DiscordConfigEntry) -> bool:
    """Set up Discord Assist from a config entry."""
    router = ConversationRouter.from_entry(hass, entry)
    approval_store = ApprovalStore(hass, entry.entry_id)
    await approval_store.async_load()
    approvals = ApprovalManager(
        hass,
        entry.entry_id,
        router,
        approval_store,
        ApprovalPolicy.from_options(entry.options),
    )
    if not router.has_authorized_users:
        _LOGGER.warning(
            "No Discord users are authorized for %s; the bot will ignore everyone "
            "until you map a user or set an allowlist with a fallback user",
            entry.title,
        )
    _set_issue(hass, entry, ISSUE_NO_AUTHORIZED_USERS, not router.has_authorized_users)
    _set_issue(hass, entry, ISSUE_MESSAGE_CONTENT_INTENT, False)

    client = DiscordAssistClient(hass, router, signal_connection(entry.entry_id))
    approvals.bind_client(client)
    client.approvals = approvals
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

    entry.runtime_data = RuntimeData(client=client, router=router, approvals=approvals)
    entry.async_on_unload(
        async_track_time_interval(
            hass, approvals.async_expire_sweep, APPROVAL_SWEEP_INTERVAL
        )
    )
    _async_register_services(hass)

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
    if unloaded and not any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
    ):
        # The last loaded entry owns the domain service registration.
        hass.services.async_remove(DOMAIN, SERVICE_REQUEST_APPROVAL)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up repair issues when the entry is deleted."""
    for kind in (ISSUE_NO_AUTHORIZED_USERS, ISSUE_MESSAGE_CONTENT_INTENT):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry.entry_id))
