"""Tests for the in-process ha-mcp LLM API bridge (issue #41)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_HA_MCP_API_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    ISSUE_HA_MCP_API_MISSING,
    issue_id,
)
from custom_components.discord_assist.mcp_bridge import (
    HaMcpBridge,
    filter_tools,
    find_ha_mcp_api,
)

FULL_ID = "ha_mcp_tools-entry1"
SEARCH_ID = "ha_mcp_tools-entry1-toolsearch"


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration and the core homeassistant component."""
    yield


class FakeTool(llm.Tool):
    """An ha-mcp style tool; optionally bound to one entity."""

    def __init__(self, name, *, entity_id=None, result=None):
        self.name = name
        self.description = f"Fake tool {name}"
        self.parameters = vol.Schema({})
        if entity_id is not None:
            self.entity_id = entity_id
        self.result = result if result is not None else {"success": True, "tool": name}
        self.calls = []

    async def async_call(self, hass, tool_input, llm_context):
        self.calls.append((tool_input, llm_context))
        return self.result


class FakeApi(llm.API):
    """A registered LLM API with a fixed tool list."""

    def __init__(self, hass, api_id, name, tools, *, fail=False):
        super().__init__(hass=hass, id=api_id, name=name)
        self._tools = tools
        self._fail = fail
        self.instance_contexts = []

    async def async_get_api_instance(self, llm_context):
        self.instance_contexts.append(llm_context)
        if self._fail:
            raise HomeAssistantError("server is wedged")
        return llm.APIInstance(
            api=self,
            api_prompt="fake prompt",
            llm_context=llm_context,
            tools=list(self._tools),
        )


@pytest.fixture
def register_api(hass: HomeAssistant):
    """Register fake LLM APIs, unregistering them all at test end."""
    unsubs = []

    def _register(api):
        unsubs.append(llm.async_register_api(hass, api))
        return api

    yield _register
    for unsub in unsubs:
        unsub()


class StubPermissions:
    """Permissions granting control over a fixed set of entities."""

    def __init__(self, controllable=()):
        self._controllable = set(controllable)

    def check_entity(self, entity_id, key):
        return key == POLICY_CONTROL and entity_id in self._controllable


class StubUser:
    """A stand-in HA user with explicit admin flag and permissions."""

    def __init__(self, *, is_admin=False, is_active=True, controllable=()):
        self.id = "stub-user"
        self.is_admin = is_admin
        self.is_active = is_active
        self.permissions = StubPermissions(controllable)


def _expose(hass: HomeAssistant, entity_id: str, exposed: bool) -> None:
    domain, object_id = entity_id.split(".", 1)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        domain, "test", f"uid-{object_id}", suggested_object_id=object_id
    )
    hass.states.async_set(entity_id, "on")
    async_expose_entity(hass, "conversation", entity_id, exposed)


def _patch_user(hass: HomeAssistant, user: StubUser):
    return patch.object(hass.auth, "async_get_user", AsyncMock(return_value=user))


# --- Discovery -------------------------------------------------------------


async def test_find_api_prefers_full_catalog(hass: HomeAssistant, register_api):
    register_api(FakeApi(hass, SEARCH_ID, "HA-MCP Server (tool search)", []))
    full = register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", []))
    assert find_ha_mcp_api(hass) is full


async def test_find_api_falls_back_to_tool_search(hass: HomeAssistant, register_api):
    search = register_api(FakeApi(hass, SEARCH_ID, "HA-MCP Server (tool search)", []))
    assert find_ha_mcp_api(hass) is search


async def test_find_api_ignores_builtin_assist(hass: HomeAssistant):
    # Only Home Assistant's built-in "assist" API is registered.
    assert find_ha_mcp_api(hass) is None


async def test_find_api_name_fallback(hass: HomeAssistant, register_api):
    api = register_api(FakeApi(hass, "some_custom_id", "HA-MCP Server", []))
    assert find_ha_mcp_api(hass) is api


async def test_find_api_preferred_id(hass: HomeAssistant, register_api):
    first = register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", []))
    second = register_api(FakeApi(hass, "ha_mcp_tools-entry2", "HA-MCP Server", []))
    assert find_ha_mcp_api(hass, "ha_mcp_tools-entry2") is second
    assert find_ha_mcp_api(hass, "ha_mcp_tools-gone") is None
    # Without a preference the choice is deterministic (lowest id).
    assert find_ha_mcp_api(hass) is first


# --- Catalog filtering -------------------------------------------------------


async def test_full_catalog_unfiltered_for_admin(
    hass: HomeAssistant, hass_admin_user, register_api
):
    tools = [
        FakeTool("ha_get_state"),
        FakeTool("ha_secret_tool", entity_id="light.hidden"),
    ]
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", tools))
    bridge = HaMcpBridge(hass)
    result = await bridge.async_get_tools(Context(user_id=hass_admin_user.id))
    assert [tool.name for tool in result] == ["ha_get_state", "ha_secret_tool"]


async def test_filter_tools_per_user_permissions(hass: HomeAssistant):
    _expose(hass, "light.kitchen", True)
    _expose(hass, "light.bedroom", True)
    _expose(hass, "light.secret", False)
    user = StubUser(controllable={"light.kitchen", "light.secret"})
    tools = [
        FakeTool("ha_get_state"),  # generic: kept, checked per call
        FakeTool("kitchen_tool", entity_id="light.kitchen"),  # exposed + control
        FakeTool("bedroom_tool", entity_id="light.bedroom"),  # exposed, no control
        FakeTool("secret_tool", entity_id="light.secret"),  # control, not exposed
    ]
    kept = filter_tools(hass, tools, user)
    assert [tool.name for tool in kept] == ["ha_get_state", "kitchen_tool"]
    assert filter_tools(hass, tools, None) == []


async def test_get_tools_without_identity_raises(hass: HomeAssistant, register_api):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [FakeTool("ha_get_state")]))
    bridge = HaMcpBridge(hass)
    with pytest.raises(PermissionError):
        await bridge.async_get_tools(Context())


async def test_get_tools_no_api_returns_empty(hass: HomeAssistant, hass_admin_user):
    bridge = HaMcpBridge(hass)
    assert bridge.available is False
    assert await bridge.async_get_tools(Context(user_id=hass_admin_user.id)) == []


async def test_get_tools_api_failure_returns_empty(
    hass: HomeAssistant, hass_admin_user, register_api
):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [], fail=True))
    bridge = HaMcpBridge(hass)
    assert await bridge.async_get_tools(Context(user_id=hass_admin_user.id)) == []


# --- Call routing ------------------------------------------------------------


async def test_call_tool_routes_with_caller_context(hass: HomeAssistant, register_api):
    tool = FakeTool("ha_get_state", result={"success": True, "result": {"state": "on"}})
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [tool]))
    user = StubUser(is_admin=True)
    context = Context(user_id=user.id)
    bridge = HaMcpBridge(hass)
    with _patch_user(hass, user):
        result = await bridge.async_call_tool(
            context, "ha_get_state", {"entity_id": "light.kitchen"}
        )
    assert result == {"success": True, "result": {"state": "on"}}
    assert len(tool.calls) == 1
    tool_input, llm_context = tool.calls[0]
    assert tool_input.tool_name == "ha_get_state"
    assert tool_input.tool_args == {"entity_id": "light.kitchen"}
    # The caller's own Context reaches the tool, never an identity-less one.
    assert llm_context.context is context


async def test_call_tool_without_user_refused(hass: HomeAssistant, register_api):
    tool = FakeTool("ha_get_state")
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [tool]))
    bridge = HaMcpBridge(hass)
    result = await bridge.async_call_tool(Context(), "ha_get_state", {})
    assert result["success"] is False
    assert tool.calls == []


async def test_call_tool_unknown_tool_is_error_not_crash(
    hass: HomeAssistant, register_api
):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [FakeTool("ha_get_state")]))
    user = StubUser(is_admin=True)
    bridge = HaMcpBridge(hass)
    with _patch_user(hass, user):
        result = await bridge.async_call_tool(
            Context(user_id=user.id), "ha_does_not_exist", {}
        )
    assert result["success"] is False


async def test_call_tool_entity_gate(hass: HomeAssistant, register_api):
    _expose(hass, "light.kitchen", True)
    tool = FakeTool("ha_call_service")
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [tool]))
    bridge = HaMcpBridge(hass)

    # Exposed, but the user may not control it: refused before the tool runs.
    user = StubUser(controllable=set())
    with _patch_user(hass, user):
        result = await bridge.async_call_tool(
            Context(user_id=user.id),
            "ha_call_service",
            {"entity_id": "light.kitchen"},
        )
    assert result["success"] is False
    assert tool.calls == []

    # Same call with control over the entity goes through.
    allowed = StubUser(controllable={"light.kitchen"})
    with _patch_user(hass, allowed):
        result = await bridge.async_call_tool(
            Context(user_id=allowed.id),
            "ha_call_service",
            {"entity_id": "light.kitchen"},
        )
    assert result["success"] is True
    assert len(tool.calls) == 1


async def test_call_tool_api_vanished_is_error_not_crash(hass: HomeAssistant):
    tool = FakeTool("ha_get_state")
    api = FakeApi(hass, FULL_ID, "HA-MCP Server", [tool])
    unregister = llm.async_register_api(hass, api)
    user = StubUser(is_admin=True)
    bridge = HaMcpBridge(hass)
    with _patch_user(hass, user):
        result = await bridge.async_call_tool(
            Context(user_id=user.id), "ha_get_state", {}
        )
        assert result["success"] is True

        # The ha-mcp entry unloads mid-conversation: the API disappears.
        unregister()
        result = await bridge.async_call_tool(
            Context(user_id=user.id), "ha_get_state", {}
        )
    assert result["success"] is False
    assert "not available" in result["error"]


async def test_call_tool_api_failure_is_error_not_crash(
    hass: HomeAssistant, register_api
):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", [], fail=True))
    user = StubUser(is_admin=True)
    bridge = HaMcpBridge(hass)
    with _patch_user(hass, user):
        result = await bridge.async_call_tool(
            Context(user_id=user.id), "ha_get_state", {}
        )
    assert result["success"] is False


# --- Setup wiring --------------------------------------------------------------


def _entry(options=None):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options=options
        if options is not None
        else {CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


async def test_setup_without_ha_mcp_api_loads_and_raises_repair(
    hass: HomeAssistant,
):
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    # The integration still loads: the bot works, the agent has no tools.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.mcp_bridge.available is False
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_HA_MCP_API_MISSING, entry.entry_id)
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING


async def test_setup_with_ha_mcp_api_no_repair(hass: HomeAssistant, register_api):
    api = register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", []))
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.mcp_bridge.api is api
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_HA_MCP_API_MISSING, entry.entry_id)
    )
    assert issue is None


async def test_setup_honors_options_api_pick(hass: HomeAssistant, register_api):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", []))
    picked = register_api(FakeApi(hass, "ha_mcp_tools-entry2", "HA-MCP Server", []))
    entry = _entry(
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {"7": "ha-a"},
            CONF_HA_MCP_API_ID: "ha_mcp_tools-entry2",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.runtime_data.mcp_bridge.api is picked


# --- Options flow ----------------------------------------------------------------


async def test_options_settings_ha_mcp_api_picker(hass: HomeAssistant, register_api):
    register_api(FakeApi(hass, FULL_ID, "HA-MCP Server", []))
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        assert result["type"] is FlowResultType.FORM
        keys = {str(key) for key in result["data_schema"].schema}
        assert CONF_HA_MCP_API_ID in keys
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AGENT_ID: "conversation.ollama", CONF_HA_MCP_API_ID: FULL_ID},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HA_MCP_API_ID] == FULL_ID
