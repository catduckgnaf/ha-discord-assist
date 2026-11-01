from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryDisabler,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_CHANNELS,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    LEGACY_DOMAIN,
)

_CHANNELS = [("555", "Home / #general")]


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so its config flow is discoverable."""
    yield


async def test_full_user_flow(hass: HomeAssistant):
    with (
        patch(
            "custom_components.discord_assist.config_flow.validate_token",
            new=AsyncMock(return_value="botid-1"),
        ),
        patch(
            "custom_components.discord_assist.config_flow.list_text_channels",
            new=AsyncMock(return_value=_CHANNELS),
        ),
        patch(
            "custom_components.discord_assist.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "token"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "good-token"}
        )
        assert result["step_id"] == "config"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_AGENT_ID: "conversation.ollama", CONF_CHANNELS: ["555"]},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == "good-token"
    assert result["options"][CONF_CHANNELS] == ["555"]


async def test_invalid_token_shows_error(hass: HomeAssistant):
    from custom_components.discord_assist.discord_api import InvalidAuth

    with patch(
        "custom_components.discord_assist.config_flow.validate_token",
        new=AsyncMock(side_effect=InvalidAuth),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "bad"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_single_entry_per_bot(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, unique_id="botid-1").add_to_hass(hass)
    with patch(
        "custom_components.discord_assist.config_flow.validate_token",
        new=AsyncMock(return_value="botid-1"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "good"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "old", "bot_id": "botid-1"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discord_assist.config_flow.validate_token",
        new=AsyncMock(return_value="botid-1"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new"


async def test_reauth_rejects_different_bot(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "old", "bot_id": "botid-1"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discord_assist.config_flow.validate_token",
        new=AsyncMock(return_value="botid-DIFFERENT"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "other-bot-token"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    # Token must not have been updated
    assert entry.data[CONF_TOKEN] == "old"


async def test_import_from_legacy_entry(hass: HomeAssistant):
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "legacy-token", "bot_id": "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )
    legacy.add_to_hass(hass)
    with (
        patch(
            "custom_components.discord_assist.config_flow.validate_token",
            new=AsyncMock(return_value="botid-1"),
        ),
        patch(
            "custom_components.discord_assist.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "import_legacy"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == "legacy-token"
    assert result["options"][CONF_USER_MAP] == {"7": "ha-a"}
    assert legacy.disabled_by is ConfigEntryDisabler.USER
