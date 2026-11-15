"""Tests for the bundled conversation agent and its tool gate."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY
from homeassistant.auth.models import Group
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.helpers.intent import IntentResponseType
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.discord_assist.agent import AgentSettings, evaluate_tool_call
from custom_components.discord_assist.const import (
    CONF_AGENT_ALLOWED_TOOLS,
    CONF_AGENT_API_KEY,
    CONF_AGENT_BASE_URL,
    CONF_AGENT_ID,
    CONF_AGENT_LLM_APIS,
    CONF_AGENT_MODEL,
    CONF_BOT_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    EVENT_TOOL_CALL,
)

API_KEY = "sk-test-secret-key"

TOOL_PARAMS = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("domain"): str,
        vol.Optional("service"): str,
        vol.Optional("target"): dict,
        vol.Optional("tool_name"): str,
    }
)


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so entry setup runs the real path."""
    yield


class FakeTool(llm.Tool):
    """An LLM tool that records its calls instead of doing anything."""

    def __init__(self, name: str, calls: list) -> None:
        self.name = name
        self.description = f"Fake tool {name}"
        self.parameters = TOOL_PARAMS
        self._calls = calls

    async def async_call(self, hass, tool_input, llm_context):
        self._calls.append((tool_input.tool_name, dict(tool_input.tool_args)))
        return {"success": True, "tool": tool_input.tool_name}


class FakeAPI(llm.API):
    """An LLM API offering a fixed set of fake tools."""

    def __init__(self, hass: HomeAssistant, tools: list[FakeTool]) -> None:
        super().__init__(hass=hass, id="fake_api", name="Fake API")
        self._tools = tools

    async def async_get_api_instance(self, llm_context):
        return llm.APIInstance(
            api=self,
            api_prompt="Fake API prompt.",
            llm_context=llm_context,
            tools=list(self._tools),
        )


@pytest.fixture
def tool_calls() -> list:
    return []


@pytest.fixture
def fake_api(hass: HomeAssistant, tool_calls: list):
    tools = [
        FakeTool(name, tool_calls)
        for name in ("ha_get_state", "ha_turn_on", "ha_restart", "ha_call_service")
    ]
    unregister = llm.async_register_api(hass, FakeAPI(hass, tools))
    yield tools
    unregister()


class FakeResponse:
    """Minimal stand-in for an aiohttp response used as an async CM."""

    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    """Records chat completion requests and replays queued replies."""

    def __init__(self, replies: list) -> None:
        self._replies = list(replies)
        self.requests: list[dict] = []

    def post(self, url: str, json: Any = None, headers: Any = None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _completion(content: str | None = None, tool_calls: list | None = None):
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return FakeResponse({"choices": [{"message": message}]})


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _entry(options: dict | None = None) -> MockConfigEntry:
    merged = {
        CONF_AGENT_ID: "conversation.home_assistant",
        CONF_USER_MAP: {},
        CONF_AGENT_BASE_URL: "http://model.test/v1",
        CONF_AGENT_MODEL: "test-model",
        CONF_AGENT_LLM_APIS: ["fake_api"],
    }
    merged.update(options or {})
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1", CONF_AGENT_API_KEY: API_KEY},
        options=merged,
    )


async def _setup_agent(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    entity_entry = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, "botid-1_agent"
    )
    assert entity_entry is not None
    return entity_entry


def _converse(hass: HomeAssistant, entity_id: str, user_id: str | None, text: str):
    return conversation.async_converse(
        hass,
        text,
        None,
        Context(user_id=user_id),
        agent_id=entity_id,
    )


def _speech(result) -> str:
    return (result.response.speech or {}).get("plain", {}).get("speech", "")


def _capture_events(hass: HomeAssistant) -> list[dict]:
    events: list[dict] = []
    hass.bus.async_listen(EVENT_TOOL_CALL, lambda event: events.append(event.data))
    return events


def _patch_session(session: FakeSession):
    return patch(
        "custom_components.discord_assist.agent.async_get_clientsession",
        return_value=session,
    )


async def test_agent_entity_registers_and_is_selectable(hass: HomeAssistant, fake_api):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    assert entity_id.startswith("conversation.")
    agent = conversation.async_get_agent(hass, entity_id)
    assert agent is not None
    info = conversation.async_get_agent_info(hass, entity_id)
    assert info is not None and info.id == entity_id


async def test_unconfigured_agent_answers_with_error_not_crash(
    hass: HomeAssistant, fake_api
):
    # An entry that simply never saved agent settings.
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.home_assistant", CONF_USER_MAP: {}},
    )
    entity_id = await _setup_agent(hass, entry)
    user = await hass.auth.async_create_user("Andrew")
    result = await _converse(hass, entity_id, user.id, "hello")
    assert result.response.response_type is IntentResponseType.ERROR
    assert "not configured" in _speech(result)


async def test_agent_answers_question_without_tools(hass: HomeAssistant, fake_api):
    entry = _entry({CONF_AGENT_LLM_APIS: []})
    entity_id = await _setup_agent(hass, entry)
    user = await hass.auth.async_create_user("Andrew")
    session = FakeSession([_completion("The kitchen is 21 degrees.")])
    with _patch_session(session):
        result = await _converse(hass, entity_id, user.id, "How warm is the kitchen?")
    assert _speech(result) == "The kitchen is 21 degrees."
    assert len(session.requests) == 1
    payload = session.requests[0]["json"]
    assert payload["model"] == "test-model"
    assert "tools" not in payload
    assert session.requests[0]["url"] == "http://model.test/v1/chat/completions"
    assert session.requests[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"


async def test_model_request_contains_only_profile_tools(
    hass: HomeAssistant, fake_api, tool_calls: list
):
    entry = _entry({CONF_AGENT_ALLOWED_TOOLS: ["ha_get_state"]})
    entity_id = await _setup_agent(hass, entry)
    user = await hass.auth.async_create_user("Andrew")
    session = FakeSession([_completion("All good.")])
    with _patch_session(session):
        result = await _converse(hass, entity_id, user.id, "Anything on?")
    assert _speech(result) == "All good."
    offered = [
        tool["function"]["name"] for tool in session.requests[0]["json"]["tools"]
    ]
    # Allowlist keeps ha_get_state; the denylist would drop ha_restart anyway.
    assert offered == ["ha_get_state"]
    assert tool_calls == []


async def test_allowed_tool_call_executes_and_is_audited(
    hass: HomeAssistant, fake_api, tool_calls: list
):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    events = _capture_events(hass)
    session = FakeSession(
        [
            _completion(
                None,
                [_tool_call("ha_turn_on", {"entity_id": "light.kitchen"})],
            ),
            _completion("Turned on the kitchen light."),
        ]
    )
    with _patch_session(session):
        result = await _converse(hass, entity_id, admin.id, "Turn on the kitchen light")
    await hass.async_block_till_done()
    assert _speech(result) == "Turned on the kitchen light."
    assert tool_calls == [("ha_turn_on", {"entity_id": "light.kitchen"})]
    # ha_restart was never offered to the model.
    offered = [
        tool["function"]["name"] for tool in session.requests[0]["json"]["tools"]
    ]
    assert "ha_restart" not in offered
    # The second request carries the tool result back to the model.
    second_messages = session.requests[1]["json"]["messages"]
    assert any(
        message["role"] == "tool" and "success" in message["content"]
        for message in second_messages
    )
    assert len(events) == 1
    assert events[0]["tool_name"] == "ha_turn_on"
    assert events[0]["outcome"] == "allowed"
    assert events[0]["user_id"] == admin.id
    assert events[0]["entity_ids"] == ["light.kitchen"]


async def test_tool_call_for_unpermitted_entity_is_refused(
    hass: HomeAssistant, fake_api, tool_calls: list
):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    limited = MockUser(
        id="limited-user",
        name="Limited",
        groups=[
            Group(
                name="Kitchen only",
                policy={
                    "entities": {
                        "entity_ids": {"light.kitchen": {"read": True, "control": True}}
                    }
                },
            )
        ],
    ).add_to_hass(hass)
    events = _capture_events(hass)
    session = FakeSession(
        [
            _completion(
                None,
                [_tool_call("ha_turn_on", {"entity_id": "light.bedroom"})],
            ),
            _completion("I could not do that."),
        ]
    )
    with _patch_session(session):
        result = await _converse(
            hass, entity_id, limited.id, "Turn on the bedroom light"
        )
    await hass.async_block_till_done()
    assert _speech(result) == "I could not do that."
    assert tool_calls == []
    assert len(events) == 1
    assert events[0]["outcome"] == "refused"
    assert events[0]["reason"] == "no_permission"
    # The model is told the call was refused.
    second_messages = session.requests[1]["json"]["messages"]
    tool_messages = [m for m in second_messages if m["role"] == "tool"]
    assert tool_messages and "error" in tool_messages[0]["content"]


async def test_read_only_user_can_read_but_not_control(
    hass: HomeAssistant, fake_api, tool_calls: list
):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    # The first user created on an instance becomes its owner; make sure the
    # read-only user is not that user.
    await hass.auth.async_create_user("First")
    reader = await hass.auth.async_create_user("Reader", group_ids=[GROUP_ID_READ_ONLY])
    events = _capture_events(hass)
    session = FakeSession(
        [
            _completion(
                None, [_tool_call("ha_get_state", {"entity_id": "light.kitchen"})]
            ),
            _completion("It is on."),
            _completion(
                None, [_tool_call("ha_turn_on", {"entity_id": "light.kitchen"})]
            ),
            _completion("I cannot control that."),
        ]
    )
    with _patch_session(session):
        first = await _converse(hass, entity_id, reader.id, "Is the kitchen on?")
        second = await _converse(hass, entity_id, reader.id, "Turn it on")
    await hass.async_block_till_done()
    assert _speech(first) == "It is on."
    assert _speech(second) == "I cannot control that."
    assert tool_calls == [("ha_get_state", {"entity_id": "light.kitchen"})]
    outcomes = [(e["tool_name"], e["outcome"]) for e in events]
    assert outcomes == [("ha_get_state", "allowed"), ("ha_turn_on", "refused")]


async def test_denied_service_via_generic_caller_is_refused(
    hass: HomeAssistant, fake_api, tool_calls: list
):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    events = _capture_events(hass)
    session = FakeSession(
        [
            _completion(
                None,
                [
                    _tool_call(
                        "ha_call_service",
                        {
                            "service": "homeassistant.restart",
                            "target": {"entity_id": "light.kitchen"},
                        },
                    )
                ],
            ),
            _completion("Refused."),
        ]
    )
    with _patch_session(session):
        result = await _converse(hass, entity_id, admin.id, "Restart Home Assistant")
    await hass.async_block_till_done()
    assert _speech(result) == "Refused."
    assert tool_calls == []
    assert len(events) == 1
    assert events[0]["outcome"] == "refused"
    assert events[0]["reason"] == "denied_tool"


async def test_endpoint_error_is_a_conversation_error_and_keeps_key_secret(
    hass: HomeAssistant, fake_api, caplog: pytest.LogCaptureFixture
):
    entry = _entry({CONF_AGENT_LLM_APIS: []})
    entity_id = await _setup_agent(hass, entry)
    user = await hass.auth.async_create_user("Andrew")
    session = FakeSession([FakeResponse(status=500)])
    with caplog.at_level(logging.DEBUG), _patch_session(session):
        result = await _converse(hass, entity_id, user.id, "hello")
    assert result.response.response_type is IntentResponseType.ERROR
    assert API_KEY not in caplog.text
    assert API_KEY not in _speech(result)

    session = FakeSession([aiohttp.ClientConnectionError("down")])
    with caplog.at_level(logging.DEBUG), _patch_session(session):
        result = await _converse(hass, entity_id, user.id, "hello again")
    assert result.response.response_type is IntentResponseType.ERROR
    assert API_KEY not in caplog.text


async def test_no_user_context_gets_no_tools(hass: HomeAssistant, fake_api):
    entry = _entry()
    entity_id = await _setup_agent(hass, entry)
    session = FakeSession([_completion("Hello.")])
    with _patch_session(session):
        result = await conversation.async_converse(
            hass, "hello", None, Context(), agent_id=entity_id
        )
    assert _speech(result) == "Hello."
    assert "tools" not in session.requests[0]["json"]


def _settings(**overrides: Any) -> AgentSettings:
    base: dict[str, Any] = {"base_url": "http://x/v1", "model": "m"}
    base.update(overrides)
    return AgentSettings(**base)


async def test_gate_denied_tool_refused_even_for_admin(hass: HomeAssistant):
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    decision = evaluate_tool_call(
        tool_name="ha_restart",
        tool_args={},
        user=admin,
        settings=_settings(),
        offered_tools=frozenset({"ha_restart"}),
    )
    assert not decision.allowed
    assert decision.reason == "denied_tool"


async def test_gate_owner_listing_allows_denied_tool_for_owner_only(
    hass: HomeAssistant,
):
    owner = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_ADMIN])
    settings = _settings(
        owner_tools=frozenset({"ha_restart"}), owner_users=frozenset({owner.id})
    )
    allowed = evaluate_tool_call(
        tool_name="ha_restart",
        tool_args={},
        user=owner,
        settings=settings,
        offered_tools=frozenset({"ha_restart"}),
    )
    assert allowed.allowed
    refused = evaluate_tool_call(
        tool_name="ha_restart",
        tool_args={},
        user=other,
        settings=settings,
        offered_tools=frozenset({"ha_restart"}),
    )
    assert not refused.allowed
    assert refused.reason == "denied_tool"


async def test_gate_unwraps_dispatcher_tool_name(hass: HomeAssistant):
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    decision = evaluate_tool_call(
        tool_name="ha_call_tool",
        tool_args={"tool_name": "ha_restart", "arguments": {}},
        user=admin,
        settings=_settings(),
        offered_tools=frozenset({"ha_call_tool"}),
    )
    assert not decision.allowed
    assert decision.reason == "denied_tool"
    assert decision.effective_name == "ha_restart"


async def test_gate_confirm_list_fails_closed(hass: HomeAssistant):
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    decision = evaluate_tool_call(
        tool_name="ha_turn_on",
        tool_args={"entity_id": "lock.front_door"},
        user=admin,
        settings=_settings(confirm_tools=frozenset({"domain:lock"})),
        offered_tools=frozenset({"ha_turn_on"}),
    )
    assert not decision.allowed
    assert decision.reason == "confirmation_required"


async def test_gate_refuses_tools_never_offered(hass: HomeAssistant):
    admin = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    decision = evaluate_tool_call(
        tool_name="ha_turn_on",
        tool_args={"entity_id": "light.kitchen"},
        user=admin,
        settings=_settings(),
        offered_tools=frozenset({"ha_get_state"}),
    )
    assert not decision.allowed
    assert decision.reason == "not_offered"


async def test_options_flow_agent_step_stores_key_in_data(
    hass: HomeAssistant, fake_api
):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "agent"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AGENT_BASE_URL: "http://model.test/v1",
            CONF_AGENT_MODEL: "test-model",
            CONF_AGENT_API_KEY: "sk-new-key",
            CONF_AGENT_LLM_APIS: ["assist"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_AGENT_API_KEY] == "sk-new-key"
    assert CONF_AGENT_API_KEY not in entry.options
    assert entry.options[CONF_AGENT_BASE_URL] == "http://model.test/v1"
    assert entry.options[CONF_AGENT_MODEL] == "test-model"
    assert entry.options[CONF_AGENT_LLM_APIS] == ["assist"]
