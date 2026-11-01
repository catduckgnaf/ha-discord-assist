from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    ISSUE_MESSAGE_CONTENT_INTENT,
    ISSUE_NO_AUTHORIZED_USERS,
    issue_id,
)

CLIENT = "custom_components.discord_assist.DiscordAssistClient"


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so setup/unload run the real entry path."""
    yield


def _entry(options=None):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options=options
        if options is not None
        else {CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


def _fake_client(**overrides):
    client = MagicMock()
    client.login = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=True)
    client.connected = False
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


async def test_setup_and_unload(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    fake_client = _fake_client()

    with patch(CLIENT, return_value=fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        assert entry.runtime_data.client is fake_client
        fake_client.login.assert_awaited_once_with("tok")
        fake_client.connect.assert_awaited_once()
        assert hass.states.get("binary_sensor.mock_title_discord_gateway")

        assert await hass.config_entries.async_unload(entry.entry_id) is True
        fake_client.close.assert_awaited()


async def test_login_failure_triggers_reauth(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    fake_client = _fake_client(login=AsyncMock(side_effect=discord.LoginFailure("x")))

    with patch(CLIENT, return_value=fake_client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )
    fake_client.connect.assert_not_awaited()


async def test_network_failure_retries_setup(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    fake_client = _fake_client(
        login=AsyncMock(side_effect=aiohttp.ClientConnectionError("down"))
    )

    with patch(CLIENT, return_value=fake_client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    fake_client.close.assert_awaited()


async def test_missing_intent_raises_repair(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    fake_client = _fake_client(
        connect=AsyncMock(side_effect=discord.PrivilegedIntentsRequired(None))
    )

    with patch(CLIENT, return_value=fake_client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_MESSAGE_CONTENT_INTENT, entry.entry_id)
    )
    assert issue is not None


async def test_gateway_death_schedules_reload(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    fake_client = _fake_client(
        connect=AsyncMock(side_effect=RuntimeError("boom")),
        is_closed=MagicMock(return_value=False),
    )

    with (
        patch(CLIENT, return_value=fake_client),
        patch("custom_components.discord_assist.RECONNECT_DELAY", 0),
        patch.object(hass.config_entries, "async_schedule_reload") as mock_reload,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    mock_reload.assert_called_once_with(entry.entry_id)


async def test_no_authorized_users_raises_repair(hass: HomeAssistant):
    entry = _entry(options={CONF_AGENT_ID: "conversation.ollama"})
    entry.add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_NO_AUTHORIZED_USERS, entry.entry_id)
    )
    assert issue is not None
