from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_FALLBACK_USER,
    CONF_PROFILE_ROLE_MAP,
    CONF_PROFILE_USER_MAP,
    CONF_PROFILES,
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
        "add_profile",
        "edit_profile",
        "remove_profile",
        "add_profile_map",
        "remove_profile_map",
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


async def test_remove_room_aborts_when_none(hass: HomeAssistant):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_room"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_rooms"


# --- Access profiles (issue #12) -------------------------------------------

_FAKE_USER = SimpleNamespace(
    id="ha-kids", name="Kids", is_active=True, system_generated=False
)


def _patch_users(hass):
    return patch.object(
        hass.auth, "async_get_users", AsyncMock(return_value=[_FAKE_USER])
    )


async def _goto(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_add_profile(hass: HomeAssistant):
    entry = _entry(hass)
    with _patch_users(hass):
        result = await _goto(hass, entry, "add_profile")
        assert result["step_id"] == "add_profile"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "name": "kids",
                "ha_user": "ha-kids",
                "channels": "222, 333",
                "dms": False,
                "quiet_hours": "21:00-07:00",
                "rate_limit": 5,
                "agent": "conversation.home_assistant",
                "areas": "playroom",
                "domains": "light",
                "confirm": "cover",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    profile = entry.options[CONF_PROFILES]["kids"]
    assert profile["ha_user"] == "ha-kids"
    assert profile["channels"] == ["222", "333"]
    assert profile["dms"] is False
    assert profile["quiet_hours"] == "21:00-07:00"
    assert profile["rate_limit"] == 5
    assert profile["agent"] == "conversation.home_assistant"
    assert profile["areas"] == ["playroom"]
    assert profile["domains"] == ["light"]
    assert profile["confirm"] == ["cover"]


async def test_add_profile_rejects_bad_channel_and_duplicate(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {"kids": {"ha_user": "ha-kids"}},
        },
    )
    with _patch_users(hass):
        result = await _goto(hass, entry, "add_profile")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": "kids", "ha_user": "ha-kids"}
        )
        assert result["errors"] == {"base": "profile_exists"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"name": "teens", "ha_user": "ha-kids", "channels": "den"},
        )
        assert result["errors"] == {"base": "invalid_channel_id"}
    assert "teens" not in entry.options[CONF_PROFILES]


async def test_add_deny_profile(hass: HomeAssistant):
    entry = _entry(hass)
    with _patch_users(hass):
        result = await _goto(hass, entry, "add_profile")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": "deny"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PROFILES]["deny"]["ha_user"] is None


async def test_edit_profile(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {"kids": {"ha_user": "ha-old", "channels": ["1"]}},
        },
    )
    with _patch_users(hass):
        result = await _goto(hass, entry, "edit_profile")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"name": "kids", "ha_user": "ha-kids", "channels": "222", "dms": True},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    profile = entry.options[CONF_PROFILES]["kids"]
    assert profile["ha_user"] == "ha-kids"
    assert profile["channels"] == ["222"]


async def test_edit_profile_aborts_when_none(hass: HomeAssistant):
    entry = _entry(hass)
    result = await _goto(hass, entry, "edit_profile")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_profiles"


async def test_remove_profile_drops_its_mappings(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {
                "kids": {"ha_user": "ha-kids"},
                "family": {"ha_user": "ha-family"},
            },
            CONF_PROFILE_USER_MAP: {"1": "kids", "2": "family"},
            CONF_PROFILE_ROLE_MAP: {"50": "kids"},
        },
    )
    result = await _goto(hass, entry, "remove_profile")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "kids"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(entry.options[CONF_PROFILES]) == {"family"}
    assert entry.options[CONF_PROFILE_USER_MAP] == {"2": "family"}
    assert entry.options[CONF_PROFILE_ROLE_MAP] == {}


async def test_add_profile_map_user_and_role(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {"kids": {"ha_user": "ha-kids"}},
        },
    )
    result = await _goto(hass, entry, "add_profile_map")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"kind": "user", "target_id": "7", "profile": "kids"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PROFILE_USER_MAP] == {"7": "kids"}

    result = await _goto(hass, entry, "add_profile_map")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"kind": "role", "target_id": "50", "profile": "kids"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PROFILE_ROLE_MAP] == {"50": "kids"}


async def test_add_profile_map_rejects_bad_ids(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {"kids": {"ha_user": "ha-kids"}},
        },
    )
    result = await _goto(hass, entry, "add_profile_map")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"kind": "role", "target_id": "family-role", "profile": "kids"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_role_id"}
    # An unknown profile cannot be submitted at all: the profile field is
    # a selector limited to the defined profiles.


async def test_remove_profile_map(hass: HomeAssistant):
    entry = _entry(
        hass,
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {},
            CONF_PROFILES: {"kids": {"ha_user": "ha-kids"}},
            CONF_PROFILE_USER_MAP: {"7": "kids", "8": "kids"},
        },
    )
    result = await _goto(hass, entry, "remove_profile_map")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"kind": "user", "target_id": "7"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PROFILE_USER_MAP] == {"8": "kids"}
