"""Tests for the scoped LLM API (permissions phase B, issue #34)."""

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_mock_service,
)

from custom_components.discord_assist.const import (
    CONF_BOT_ID,
    CONF_LLM_SCOPES,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    LLM_API_ID,
)
from custom_components.discord_assist.llm_api import (
    DiscordAssistAPI,
    ProfileScope,
    TurnOffTool,
    TurnOnTool,
    resolve_scope,
)


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so the real entry path can run."""
    yield


def _llm_context(user_id: str | None) -> llm.LLMContext:
    return llm.LLMContext(
        platform="discord_assist",
        context=Context(user_id=user_id) if user_id is not None else None,
        language="en",
        assistant="conversation",
        device_id=None,
    )


def _add_entity(
    hass: HomeAssistant,
    domain: str,
    object_id: str,
    *,
    area_id: str | None = None,
    state: str = "off",
    expose: bool = True,
) -> str:
    """Register an entity, give it a state, and expose it to conversation."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain, "test", f"uid-{object_id}", suggested_object_id=object_id
    )
    if area_id is not None:
        registry.async_update_entity(entry.entity_id, area_id=area_id)
    hass.states.async_set(
        entry.entity_id, state, {"friendly_name": object_id.replace("_", " ").title()}
    )
    # Explicit either way: this HA version exposes new entities by default.
    async_expose_entity(hass, "conversation", entry.entity_id, expose)
    return entry.entity_id


def _owner(hass: HomeAssistant) -> MockUser:
    return MockUser(name="Owner", is_owner=True).add_to_hass(hass)


def _scopes_entry(hass: HomeAssistant, scopes: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_LLM_SCOPES: scopes})
    entry.add_to_hass(hass)
    return entry


async def _instance(hass: HomeAssistant, user_id: str | None) -> llm.APIInstance:
    api = DiscordAssistAPI(hass=hass)
    return await api.async_get_api_instance(_llm_context(user_id))


def _tool_names(instance: llm.APIInstance) -> set[str]:
    return {tool.name for tool in instance.tools}


async def test_api_registered_with_entry_and_removed_on_unload(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_USER_MAP: {"7": "ha-a"}},
    )
    entry.add_to_hass(hass)
    assert LLM_API_ID not in [api.id for api in llm.async_get_apis(hass)]

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    apis = {api.id: api for api in llm.async_get_apis(hass)}
    assert LLM_API_ID in apis
    assert isinstance(apis[LLM_API_ID], DiscordAssistAPI)

    # The registered API is usable through HA's own lookup path.
    user = _owner(hass)
    instance = await llm.async_get_api(hass, LLM_API_ID, _llm_context(user.id))
    assert instance.api.id == LLM_API_ID

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert LLM_API_ID not in [api.id for api in llm.async_get_apis(hass)]


async def test_no_identity_gets_no_tools(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    for context in (None, Context()):
        instance = await DiscordAssistAPI(hass=hass).async_get_api_instance(
            llm.LLMContext(
                platform="discord_assist",
                context=context,
                language="en",
                assistant="conversation",
                device_id=None,
            )
        )
        assert instance.tools == []
        assert "light.kitchen" not in instance.api_prompt


async def test_tools_and_prompt_filtered_by_area_scope(hass: HomeAssistant):
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")
    living = area_reg.async_create("Living Room")
    bedroom = area_reg.async_create("Bedroom")
    _add_entity(hass, "light", "kitchen", area_id=kitchen.id)
    _add_entity(hass, "light", "living_room", area_id=living.id)
    _add_entity(hass, "light", "bedroom", area_id=bedroom.id)
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"areas": [kitchen.id, living.id]}})

    instance = await _instance(hass, user.id)
    assert "light.kitchen" in instance.api_prompt
    assert "light.living_room" in instance.api_prompt
    assert "light.bedroom" not in instance.api_prompt
    assert _tool_names(instance) == {"GetDateTime", "GetState", "TurnOn", "TurnOff"}

    result = await instance.async_call_tool(
        llm.ToolInput(tool_name="GetState", tool_args={"entity_id": "light.kitchen"})
    )
    assert result["state"] == "off"

    # The forbidden entity is refused even when called directly by name.
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState", tool_args={"entity_id": "light.bedroom"}
            )
        )


async def test_denied_domains_never_appear(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    _add_entity(hass, "sensor", "temperature", state="21.5")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"domains": ["light"]}})

    instance = await _instance(hass, user.id)
    assert "light.kitchen" in instance.api_prompt
    assert "sensor.temperature" not in instance.api_prompt
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState",
                tool_args={"entity_id": "sensor.temperature"},
            )
        )


async def test_deny_entities_wins_over_allow(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    _add_entity(hass, "lock", "front_door", state="locked")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"deny_entities": ["lock.front_door"]}})

    instance = await _instance(hass, user.id)
    assert "light.kitchen" in instance.api_prompt
    assert "lock.front_door" not in instance.api_prompt
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState", tool_args={"entity_id": "lock.front_door"}
            )
        )


async def test_tool_list_filtered_by_user_permissions(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    _add_entity(hass, "light", "bedroom")
    user = MockUser(name="Limited").add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {"light.kitchen": True}}})

    instance = await _instance(hass, user.id)
    assert "light.kitchen" in instance.api_prompt
    assert "light.bedroom" not in instance.api_prompt

    result = await instance.async_call_tool(
        llm.ToolInput(tool_name="GetState", tool_args={"entity_id": "light.kitchen"})
    )
    assert result["entity_id"] == "light.kitchen"
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState", tool_args={"entity_id": "light.bedroom"}
            )
        )


async def test_allowed_turn_on_executes(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    user = _owner(hass)
    turn_on_calls = async_mock_service(hass, "homeassistant", "turn_on")
    turn_off_calls = async_mock_service(hass, "homeassistant", "turn_off")

    instance = await _instance(hass, user.id)
    result = await instance.async_call_tool(
        llm.ToolInput(tool_name="TurnOn", tool_args={"entity_id": "light.kitchen"})
    )
    assert result == {"success": True}
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].data["entity_id"] == "light.kitchen"
    # The service runs as the requesting user, so HA's own checks also apply.
    assert turn_on_calls[0].context.user_id == user.id

    result = await instance.async_call_tool(
        llm.ToolInput(tool_name="TurnOff", tool_args={"entity_id": "light.kitchen"})
    )
    assert result == {"success": True}
    assert len(turn_off_calls) == 1


async def test_read_only_scope_gets_no_state_changing_tools(
    hass: HomeAssistant,
):
    _add_entity(hass, "light", "kitchen")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"read_only": True}})

    instance = await _instance(hass, user.id)
    assert _tool_names(instance) == {"GetDateTime", "GetState"}
    assert not any(getattr(tool, "state_changing", False) for tool in instance.tools)

    # Reading still works.
    result = await instance.async_call_tool(
        llm.ToolInput(tool_name="GetState", tool_args={"entity_id": "light.kitchen"})
    )
    assert result["state"] == "off"


async def test_direct_call_to_unlisted_state_changing_tool_refused(
    hass: HomeAssistant,
):
    """A state-changing tool invoked by name under a read-only scope fails."""
    _add_entity(hass, "light", "kitchen")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"read_only": True}})
    turn_on_calls = async_mock_service(hass, "homeassistant", "turn_on")

    instance = await _instance(hass, user.id)
    # Not in the instance list: HA core refuses the call itself.
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(tool_name="TurnOn", tool_args={"entity_id": "light.kitchen"})
        )
    # Invoked directly, bypassing the list: the tool re-checks and refuses.
    with pytest.raises(HomeAssistantError):
        await TurnOnTool().async_call(
            hass,
            llm.ToolInput(tool_name="TurnOn", tool_args={"entity_id": "light.kitchen"}),
            _llm_context(user.id),
        )
    assert turn_on_calls == []


async def test_direct_call_outside_scope_refused(hass: HomeAssistant):
    """Tools re-resolve the scope at call time, not from a stale list."""
    _add_entity(hass, "light", "kitchen")
    _add_entity(hass, "light", "bedroom")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"entities": ["light.kitchen"]}})
    turn_on_calls = async_mock_service(hass, "homeassistant", "turn_on")

    with pytest.raises(HomeAssistantError):
        await TurnOffTool().async_call(
            hass,
            llm.ToolInput(
                tool_name="TurnOff", tool_args={"entity_id": "light.bedroom"}
            ),
            _llm_context(user.id),
        )
    assert turn_on_calls == []


async def test_unexposed_entity_is_not_visible(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen", expose=False)
    user = _owner(hass)

    instance = await _instance(hass, user.id)
    assert "light.kitchen" not in instance.api_prompt
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState", tool_args={"entity_id": "light.kitchen"}
            )
        )


async def test_malformed_scope_fails_closed(hass: HomeAssistant):
    _add_entity(hass, "light", "kitchen")
    user = _owner(hass)
    _scopes_entry(hass, {user.id: {"areas": "kitchen"}})

    instance = await _instance(hass, user.id)
    assert "light.kitchen" not in instance.api_prompt
    # A closed scope is read-only too: no state-changing tools at all.
    assert _tool_names(instance) == {"GetDateTime", "GetState"}
    with pytest.raises(HomeAssistantError):
        await instance.async_call_tool(
            llm.ToolInput(
                tool_name="GetState", tool_args={"entity_id": "light.kitchen"}
            )
        )


def test_resolve_scope_defaults_and_parsing(hass: HomeAssistant):
    user = _owner(hass)
    # No scope configured: unrestricted default (exposure + permissions
    # are still enforced on top of it).
    assert resolve_scope(hass, user.id) == ProfileScope()

    _scopes_entry(
        hass,
        {
            user.id: {
                "areas": ["kitchen"],
                "domains": ["light"],
                "entities": ["light.kitchen"],
                "deny_entities": ["light.kitchen_main"],
                "read_only": True,
            }
        },
    )
    scope = resolve_scope(hass, user.id)
    assert scope.areas == frozenset({"kitchen"})
    assert scope.domains == frozenset({"light"})
    assert scope.entities == frozenset({"light.kitchen"})
    assert scope.deny_entities == frozenset({"light.kitchen_main"})
    assert scope.read_only is True
    assert scope.allows("light.kitchen", "kitchen")
    assert not scope.allows("light.kitchen", "bedroom")
    assert not scope.allows("switch.kitchen", "kitchen")
    assert not scope.allows("light.kitchen_main", "kitchen")
