from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_AGENT_MAP_CHANNEL,
    CONF_AGENT_MAP_USER,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_CONVERSATION_SCOPE,
    CONF_FALLBACK_USER,
    CONF_ROOM_MAP,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
)


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so its options flow is discoverable."""
    yield


def _entry(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options=options or {CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {}},
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_menu_shown(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "settings",
        "add_user_map",
        "remove_user_map",
        "add_room",
        "remove_room",
        "add_agent_user",
        "remove_agent_user",
        "add_agent_channel",
        "remove_agent_channel",
    }


async def test_options_settings_updates(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[("555", "Home / #general")]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AGENT_ID: "conversation.ollama", CONF_CHANNELS: ["555"]},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CHANNELS] == ["555"]


async def test_options_settings_conversation_scope(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AGENT_ID: "conversation.ollama", CONF_CONVERSATION_SCOPE: "user"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CONVERSATION_SCOPE] == "user"


async def test_add_user_mapping(hass: HomeAssistant):
    entry = _entry(hass)
    fake_user = SimpleNamespace(
        id="ha-user-1", name="Alice", is_active=True, system_generated=False
    )
    with patch.object(
        hass.auth, "async_get_users", AsyncMock(return_value=[fake_user])
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_user_map"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"discord_user_id": "7", CONF_FALLBACK_USER: "ha-user-1"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_USER_MAP] == {"7": "ha-user-1"}


async def test_add_user_mapping_rejects_non_numeric_discord_id(hass: HomeAssistant):
    entry = _entry(hass)
    fake_user = SimpleNamespace(
        id="ha-user-1", name="Alice", is_active=True, system_generated=False
    )
    with patch.object(
        hass.auth, "async_get_users", AsyncMock(return_value=[fake_user])
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_user_map"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"discord_user_id": "not-a-number", CONF_FALLBACK_USER: "ha-user-1"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_user_id"}
    # No mapping was created
    assert entry.options.get(CONF_USER_MAP, {}) == {}


async def test_remove_user_mapping(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {"7": "ha-user-1", "8": "ha-user-2"},
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_user_map"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"discord_user_id": "7"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_USER_MAP] == {"8": "ha-user-2"}


async def test_add_room_channel(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[("555", "Home / #den")]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_room"}
        )
        assert result["step_id"] == "add_room"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"channel_id": "555", "device_id": "dev-den"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ROOM_MAP] == {"555": "dev-den"}


async def test_add_room_rejects_bad_channel_id(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_room"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"channel_id": "den", "device_id": "dev-den"}
        )
    assert result["errors"] == {"base": "invalid_channel_id"}


async def test_remove_room_channel(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_ROOM_MAP: {"555": "dev-den", "556": "dev-kitchen"},
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_room"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"channel_id": "555"}
    )
    assert entry.options[CONF_ROOM_MAP] == {"556": "dev-kitchen"}


async def test_add_agent_for_user(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_agent_user"}
    )
    assert result["step_id"] == "add_agent_user"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"discord_user_id": "7", CONF_AGENT_ID: "conversation.kids"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AGENT_MAP_USER] == {"7": "conversation.kids"}


async def test_add_agent_for_user_rejects_non_numeric_id(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_agent_user"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"discord_user_id": "nope", CONF_AGENT_ID: "conversation.kids"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_user_id"}
    assert entry.options.get(CONF_AGENT_MAP_USER, {}) == {}


async def test_remove_agent_for_user(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_AGENT_MAP_USER: {"7": "conversation.kids", "8": "conversation.x"},
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_agent_user"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"discord_user_id": "7"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AGENT_MAP_USER] == {"8": "conversation.x"}


async def test_remove_agent_for_user_aborts_when_none(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_agent_user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_user_agents"


async def test_add_agent_for_channel(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[("555", "Home / #kids")]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_agent_channel"}
        )
        assert result["step_id"] == "add_agent_channel"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"channel_id": "555", CONF_AGENT_ID: "conversation.kids"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AGENT_MAP_CHANNEL] == {"555": "conversation.kids"}


async def test_add_agent_for_channel_rejects_bad_channel_id(hass: HomeAssistant):
    entry = _entry(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_agent_channel"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"channel_id": "kids", CONF_AGENT_ID: "conversation.kids"},
        )
    assert result["errors"] == {"base": "invalid_channel_id"}
    assert entry.options.get(CONF_AGENT_MAP_CHANNEL, {}) == {}


async def test_remove_agent_for_channel(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_AGENT_MAP_CHANNEL: {"555": "conversation.kids"},
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_agent_channel"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"channel_id": "555"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AGENT_MAP_CHANNEL] == {}


async def test_remove_agent_for_channel_aborts_when_none(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_agent_channel"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_channel_agents"


async def test_remove_room_aborts_when_none(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_room"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_rooms"
