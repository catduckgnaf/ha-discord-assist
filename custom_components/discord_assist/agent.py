"""Bundled conversation agent: model client and tool gate.

The agent talks to any OpenAI-compatible chat completions endpoint. Tools
come from Home Assistant LLM APIs (for example the built-in Assist API, or
ha-mcp's in-process server, which registers its catalog as an LLM API), so
there is no MCP client, URL or token to manage here.

ha-mcp acts as one administrator account, so Home Assistant's per-user
permissions do not apply to calls made through it. The gate in this module
is therefore the security boundary for those tools: every tool call the
model makes passes through :func:`evaluate_tool_call` before anything runs,
and the model is only ever shown the tools that pass the same checks.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.auth.models import User
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    SystemContent,
    ToolResultContent,
    UserContent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from voluptuous_openapi import convert as vol_convert

from .const import (
    CONF_AGENT_ALLOWED_TOOLS,
    CONF_AGENT_API_KEY,
    CONF_AGENT_BASE_URL,
    CONF_AGENT_CONFIRM_TOOLS,
    CONF_AGENT_LLM_APIS,
    CONF_AGENT_MAX_TOKENS,
    CONF_AGENT_MAX_TOOL_CALLS,
    CONF_AGENT_MODEL,
    CONF_AGENT_OWNER_TOOLS,
    CONF_AGENT_OWNER_USERS,
    CONF_AGENT_TEMPERATURE,
    DEFAULT_AGENT_MAX_TOKENS,
    DEFAULT_AGENT_MAX_TOOL_CALLS,
    DEFAULT_AGENT_TEMPERATURE,
    EVENT_TOOL_CALL,
)

_LOGGER = logging.getLogger(__name__)

# Tools whose names contain any of these markers can restart or stop Home
# Assistant, write files or YAML, manage backups or add-ons, or run raw
# commands. They are never offered to the model and never executed unless
# the caller is an owner user (see AgentSettings) whose settings explicitly
# list the tool by name. Matching is deliberately broad: a false refusal is
# cheap, a missed restart tool is not.
DENIED_TOOL_MARKERS = (
    "restart",
    "shutdown",
    "reboot",
    "backup",
    "shell",
    "ws_command",
    "write_file",
    "file_write",
    "edit_file",
    "delete_file",
    "yaml_write",
    "write_yaml",
    "addon",
    "supervisor",
)

# Service calls hidden inside a generic caller (ha_call_service and friends)
# are checked too: a denied tool name must not be reachable by asking a
# generic tool to run the service instead.
DENIED_SERVICE_DOMAINS = frozenset({"backup", "shell_command", "hassio", "supervisor"})
DENIED_SERVICES = frozenset(
    {
        "homeassistant.restart",
        "homeassistant.stop",
        "homeassistant.reload_core_config",
        "homeassistant.reload_all",
    }
)

# A tool is treated as read-only only when its name says so. Anything else
# is state-changing and needs POLICY_CONTROL on every entity it names.
READ_ONLY_MARKERS = (
    "get",
    "list",
    "search",
    "read",
    "query",
    "fetch",
    "describe",
    "history",
    "state",
    "find",
    "lookup",
    "check",
)

_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


@dataclass(frozen=True)
class AgentSettings:
    """Bundled agent settings, read live from the config entry."""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    max_tokens: int = DEFAULT_AGENT_MAX_TOKENS
    temperature: float = DEFAULT_AGENT_TEMPERATURE
    max_tool_calls: int = DEFAULT_AGENT_MAX_TOOL_CALLS
    # None means the option was never saved: fall back to the built-in
    # Assist API. An explicit empty list means no tools at all.
    llm_apis: list[str] | None = None
    allowed_tools: frozenset[str] = frozenset()
    owner_tools: frozenset[str] = frozenset()
    owner_users: frozenset[str] = frozenset()
    confirm_tools: frozenset[str] = frozenset()

    @property
    def configured(self) -> bool:
        """Whether a model endpoint is set up at all."""
        return bool(self.base_url and self.model)

    @property
    def api_ids(self) -> list[str]:
        """LLM API ids whose tools the agent may use."""
        if self.llm_apis is None:
            return [llm.LLM_API_ASSIST]
        return list(self.llm_apis)

    def is_owner(self, user_id: str | None) -> bool:
        """Whether this Home Assistant user may use owner-listed tools."""
        return user_id is not None and user_id in self.owner_users

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> AgentSettings:
        """Build settings from an entry's data (secret) and options."""
        opts = entry.options
        apis = opts.get(CONF_AGENT_LLM_APIS)
        return cls(
            base_url=opts.get(CONF_AGENT_BASE_URL) or None,
            api_key=entry.data.get(CONF_AGENT_API_KEY) or None,
            model=opts.get(CONF_AGENT_MODEL) or None,
            max_tokens=int(opts.get(CONF_AGENT_MAX_TOKENS, DEFAULT_AGENT_MAX_TOKENS)),
            temperature=float(
                opts.get(CONF_AGENT_TEMPERATURE, DEFAULT_AGENT_TEMPERATURE)
            ),
            max_tool_calls=int(
                opts.get(CONF_AGENT_MAX_TOOL_CALLS, DEFAULT_AGENT_MAX_TOOL_CALLS)
            ),
            llm_apis=list(apis) if apis is not None else None,
            allowed_tools=frozenset(opts.get(CONF_AGENT_ALLOWED_TOOLS, [])),
            owner_tools=frozenset(opts.get(CONF_AGENT_OWNER_TOOLS, [])),
            owner_users=frozenset(opts.get(CONF_AGENT_OWNER_USERS, [])),
            confirm_tools=frozenset(opts.get(CONF_AGENT_CONFIRM_TOOLS, [])),
        )


def _effective_tool_name(tool_name: str, tool_args: dict[str, Any]) -> str:
    """The real tool a call reaches.

    Dispatcher tools (tool search) carry the actual tool name in their
    arguments; checks must apply to that name, not the dispatcher's.
    """
    inner = tool_args.get("tool_name") or tool_args.get("tool")
    if isinstance(inner, str) and inner and "call_tool" in tool_name.lower():
        return inner
    return tool_name


def _walk_values(node: Any) -> Iterable[Any]:
    """Yield every scalar in nested tool arguments."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_values(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk_values(value)
    else:
        yield node


def _extract_entity_ids(tool_args: dict[str, Any]) -> list[str]:
    """Every entity id named anywhere in the tool arguments."""
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "entity_id":
                    if isinstance(value, str):
                        found.append(value)
                    elif isinstance(value, (list, tuple)):
                        found.extend(v for v in value if isinstance(v, str))
                else:
                    visit(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                visit(value)

    visit(tool_args)
    return sorted(set(found))


def _extract_service_refs(tool_args: dict[str, Any]) -> list[str]:
    """Service references (``domain.service``) hidden in the arguments."""
    refs: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            service = node.get("service")
            if isinstance(service, str) and _SERVICE_RE.match(service):
                refs.append(service)
            domain = node.get("domain")
            name = node.get("service_name") or node.get("service_call")
            if isinstance(domain, str) and isinstance(name, str) and "." not in name:
                refs.append(f"{domain}.{name}")
            for value in node.values():
                visit(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                visit(value)

    visit(tool_args)
    return refs


def _is_denied_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in DENIED_TOOL_MARKERS)


def _is_read_only(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in READ_ONLY_MARKERS)


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one gate evaluation."""

    allowed: bool
    reason: str | None = None
    detail: str | None = None
    entity_ids: tuple[str, ...] = ()
    effective_name: str = ""


def evaluate_tool_call(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    user: User | None,
    settings: AgentSettings,
    offered_tools: frozenset[str],
) -> GateDecision:
    """Decide whether one tool call may run. The one gate every call passes.

    Checks, in order: the tool was actually offered to the model; dangerous
    tools stay denied unless an owner user explicitly listed them; the
    profile allowlist; argument-level service references to denied
    services; the confirmation list (fail closed until approval buttons
    land); and the caller's Home Assistant permissions on every entity the
    arguments name (POLICY_CONTROL for state-changing tools, POLICY_READ
    for read-only ones).
    """
    effective = _effective_tool_name(tool_name, tool_args)
    entity_ids = tuple(_extract_entity_ids(tool_args))

    def refused(reason: str, detail: str) -> GateDecision:
        return GateDecision(
            allowed=False,
            reason=reason,
            detail=detail,
            entity_ids=entity_ids,
            effective_name=effective,
        )

    if tool_name not in offered_tools:
        return refused("not_offered", f"Tool {tool_name} was not offered to the model")

    owner = settings.is_owner(user.id if user else None)
    owner_listed = (
        effective in settings.owner_tools or tool_name in settings.owner_tools
    )

    denied = _is_denied_name(effective) or _is_denied_name(tool_name)
    denied_services = [
        ref
        for ref in _extract_service_refs(tool_args)
        if ref in DENIED_SERVICES or ref.split(".", 1)[0] in DENIED_SERVICE_DOMAINS
    ]
    if (denied or denied_services) and not (owner and owner_listed):
        target = effective if denied else ", ".join(denied_services)
        return refused(
            "denied_tool",
            f"{target} is on the dangerous-tools denylist and is not "
            "explicitly listed for an owner profile",
        )

    if settings.allowed_tools and effective not in settings.allowed_tools:
        return refused(
            "not_in_profile", f"Tool {effective} is not in this profile's tool list"
        )

    confirm_domains = {
        entry.split(":", 1)[1]
        for entry in settings.confirm_tools
        if entry.startswith("domain:")
    }
    if effective in settings.confirm_tools or any(
        entity_id.split(".", 1)[0] in confirm_domains for entity_id in entity_ids
    ):
        return refused(
            "confirmation_required",
            f"Tool {effective} requires approval in Discord before it runs; "
            "approval is not wired up yet, so the call was refused",
        )

    if user is None:
        return refused(
            "no_user", "Tool calls need a Home Assistant user; none was provided"
        )

    policy = POLICY_READ if _is_read_only(effective) else POLICY_CONTROL
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, policy):
            return refused(
                "no_permission",
                f"The Home Assistant user may not {policy} {entity_id}",
            )

    return GateDecision(allowed=True, entity_ids=entity_ids, effective_name=effective)


def tool_visible(tool_name: str, *, user: User | None, settings: AgentSettings) -> bool:
    """Whether a tool may be shown to the model for this caller at all.

    Mirrors the name-based parts of the gate so a profile's model request
    contains only that profile's tools. Argument and permission checks
    still run at call time in the gate.
    """
    if settings.allowed_tools and tool_name not in settings.allowed_tools:
        return False
    if _is_denied_name(tool_name):
        return settings.is_owner(user.id if user else None) and (
            tool_name in settings.owner_tools
        )
    return True


@dataclass
class ToolGate:
    """Wraps an LLM API instance: audits and gates every tool call."""

    hass: HomeAssistant
    settings: AgentSettings
    user_id: str | None
    config_entry_id: str
    agent_id: str
    offered_tools: frozenset[str]

    async def async_check(self, tool_input: llm.ToolInput) -> None:
        """Gate one call; raise HomeAssistantError when it is refused."""
        user = (
            await self.hass.auth.async_get_user(self.user_id) if self.user_id else None
        )
        decision = evaluate_tool_call(
            tool_name=tool_input.tool_name,
            tool_args=tool_input.tool_args,
            user=user,
            settings=self.settings,
            offered_tools=self.offered_tools,
        )
        self.hass.bus.async_fire(
            EVENT_TOOL_CALL,
            {
                "agent_id": self.agent_id,
                "config_entry_id": self.config_entry_id,
                "user_id": self.user_id,
                "tool_name": tool_input.tool_name,
                "effective_tool_name": decision.effective_name,
                "entity_ids": list(decision.entity_ids),
                "outcome": "allowed" if decision.allowed else "refused",
                "reason": decision.reason,
            },
        )
        if decision.allowed:
            _LOGGER.info(
                "Bundled agent tool call allowed: %s by user %s (entities: %s)",
                decision.effective_name,
                self.user_id,
                ", ".join(decision.entity_ids) or "none",
            )
            return
        _LOGGER.warning(
            "Bundled agent tool call refused: %s by user %s: %s",
            decision.effective_name,
            self.user_id,
            decision.detail,
        )
        raise HomeAssistantError(f"Tool call refused: {decision.detail}")


@dataclass
class GatedAPIInstance(llm.APIInstance):
    """An LLM API instance whose tool calls all pass through the gate."""

    gate: ToolGate | None = field(default=None)

    async def async_call_tool(self, tool_input: llm.ToolInput) -> Any:
        """Gate the call, then forward it to the real API instance."""
        if self.gate is not None:
            await self.gate.async_check(tool_input)
        return await super().async_call_tool(tool_input)


async def async_gate_api_instance(
    hass: HomeAssistant,
    instance: llm.APIInstance,
    *,
    settings: AgentSettings,
    user_id: str | None,
    config_entry_id: str,
    agent_id: str,
) -> GatedAPIInstance:
    """Filter an API instance's tools for this caller and gate its calls."""
    user = await hass.auth.async_get_user(user_id) if user_id else None
    tools = [
        tool
        for tool in instance.tools
        if tool_visible(tool.name, user=user, settings=settings)
    ]
    return GatedAPIInstance(
        api=instance.api,
        api_prompt=instance.api_prompt,
        llm_context=instance.llm_context,
        tools=tools,
        custom_serializer=instance.custom_serializer,
        gate=ToolGate(
            hass=hass,
            settings=settings,
            user_id=user_id,
            config_entry_id=config_entry_id,
            agent_id=agent_id,
            offered_tools=frozenset(tool.name for tool in tools),
        ),
    )


@dataclass(frozen=True)
class ModelReply:
    """One chat completions response, normalized."""

    content: str | None
    tool_calls: list[llm.ToolInput]


def format_tools(
    tools: list[llm.Tool], custom_serializer: Any | None
) -> list[dict[str, Any]]:
    """Format LLM tools as chat completions function definitions."""
    formatted = []
    for tool in tools:
        schema = vol_convert(tool.parameters, custom_serializer=custom_serializer)
        unsupported = {"oneOf", "anyOf", "allOf", "enum", "not"}
        if unsupported.intersection(schema):
            schema = {k: v for k, v in schema.items() if k not in unsupported}
        formatted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return formatted


async def async_request_completion(
    hass: HomeAssistant,
    settings: AgentSettings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> ModelReply:
    """POST one chat completions request and normalize the reply.

    Errors raise HomeAssistantError with a message that never contains the
    API key or the request headers.
    """
    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    url = f"{(settings.base_url or '').rstrip('/')}/chat/completions"
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status != 200:
                raise HomeAssistantError(
                    f"The model endpoint answered with HTTP {response.status}"
                )
            data = await response.json()
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(
            f"Could not reach the model endpoint: {type(err).__name__}"
        ) from err
    except ValueError as err:
        raise HomeAssistantError("The model endpoint returned invalid JSON") from err

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as err:
        raise HomeAssistantError(
            "The model endpoint returned an unexpected response"
        ) from err

    tool_calls: list[llm.ToolInput] = []
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            _LOGGER.warning("Model returned unparseable arguments for %s", name)
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        kwargs: dict[str, Any] = {"tool_name": name, "tool_args": args}
        if raw_call.get("id"):
            kwargs["id"] = raw_call["id"]
        tool_calls.append(llm.ToolInput(**kwargs))
    content = message.get("content")
    return ModelReply(
        content=content if isinstance(content, str) else None,
        tool_calls=tool_calls,
    )


def messages_from_content(content: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert ChatLog content to chat completions messages.

    Attachments are not forwarded: the endpoint gets text only.
    """
    messages: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, SystemContent):
            messages.append({"role": "system", "content": item.content})
        elif isinstance(item, UserContent):
            messages.append({"role": "user", "content": item.content})
        elif isinstance(item, AssistantContent):
            message: dict[str, Any] = {
                "role": "assistant",
                "content": item.content or "",
            }
            if item.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": json.dumps(call.tool_args),
                        },
                    }
                    for call in item.tool_calls
                ]
            messages.append(message)
        elif isinstance(item, ToolResultContent):
            result = item.tool_result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": result
                    if isinstance(result, str)
                    else json.dumps(result, default=str),
                }
            )
    return messages


__all__ = [
    "AgentSettings",
    "GateDecision",
    "GatedAPIInstance",
    "ModelReply",
    "ToolGate",
    "async_gate_api_instance",
    "async_request_completion",
    "evaluate_tool_call",
    "format_tools",
    "messages_from_content",
    "tool_visible",
]
