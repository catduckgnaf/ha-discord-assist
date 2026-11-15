"""Scoped LLM API: per-user tools and entities for Discord speakers.

Permissions phase B. Discord Assist registers its own Home Assistant LLM API
(id "discord_assist", name "Discord Assist"). When a conversation agent uses
this API, the tool list and the entity list are built from the speaker's
scope: the Home Assistant user the request runs as (from the LLM context)
decides which areas, domains and entities are visible, and whether any
state-changing tools exist at all. The model cannot call what it is not
given, and every tool call re-checks the scope server side before running,
so a tool invoked by name without being listed is still refused.

Scopes live in the ``llm_scopes`` entry option, keyed by Home Assistant user
id::

    {"llm_scopes": {"<ha_user_id>": {
        "areas": ["kitchen", "living_room"],
        "domains": ["light", "switch"],
        "entities": ["light.kitchen"],
        "deny_entities": ["lock.front_door"],
        "read_only": false,
    }}}

A missing key inside a scope means "no restriction on that dimension"; a
present list (even an empty one) means "only these". ``deny_entities`` always
wins. A malformed scope fails closed: it matches nothing. A user with no
scope at all is bounded only by entity exposure and their own Home
Assistant entity permissions, which are always enforced underneath the
scope. When the profiles from phase A land, ``resolve_scope`` is the one
place that learns to read them instead of this option.

Classification rule for tools: a tool is state-changing if it can alter
Home Assistant state or run a service that does. Each tool class declares
``state_changing``; read-only scopes never list state-changing tools, and
the base tool class refuses them again at call time.

Note on the pinned Home Assistant (2026.6.3): ``llm.Tool`` has no
``annotations`` metadata and ``llm.API`` has no ``requires_admin`` field in
this version, so the read-only classification is our own ``state_changing``
attribute rather than ``ToolAnnotations``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from operator import attrgetter
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.core import HomeAssistant, split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import CONF_LLM_SCOPES, DOMAIN, LLM_API_ID, LLM_API_NAME

if TYPE_CHECKING:
    from homeassistant.auth.models import User
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Exposure list that bounds this API when the agent does not name one.
_DEFAULT_ASSISTANT = "conversation"

_PROMPT_INTRO = (
    "You are answering a request that arrived over Discord through the "
    "Discord Assist integration. Your tools are scoped to the speaker: you "
    "can only see and control the entities listed below. Do not claim other "
    "entities exist, and do not try to control anything that is not listed."
)

_NO_ENTITIES_PROMPT = (
    "This speaker's scope does not give access to any Home Assistant "
    "entities. If they ask to control a device, tell them their Discord "
    "Assist scope does not include it."
)


@dataclass(frozen=True)
class ProfileScope:
    """What one Home Assistant user may see and do through this API.

    ``None`` for areas/domains/entities means "no restriction on that
    dimension"; an empty frozenset means "nothing matches".
    """

    areas: frozenset[str] | None = None
    domains: frozenset[str] | None = None
    entities: frozenset[str] | None = None
    deny_entities: frozenset[str] = frozenset()
    read_only: bool = False

    def allows(self, entity_id: str, area_id: str | None) -> bool:
        """Whether this scope covers an entity in a given area."""
        if entity_id in self.deny_entities:
            return False
        if self.entities is not None and entity_id not in self.entities:
            return False
        domain = split_entity_id(entity_id)[0]
        if self.domains is not None and domain not in self.domains:
            return False
        return self.areas is None or area_id in self.areas


# A malformed configured scope matches nothing and changes nothing.
_CLOSED_SCOPE = ProfileScope(areas=frozenset(), read_only=True)

_MALFORMED = object()


def _id_set(raw: dict[str, Any], key: str) -> frozenset[str] | None | object:
    """Read one scope dimension: absent -> None, valid -> frozenset."""
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return _MALFORMED
    return frozenset(value)


def _parse_scope(raw: Any, ha_user_id: str) -> ProfileScope:
    """Parse one user's scope from the option value, failing closed."""
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Ignoring malformed llm_scopes entry for user %s: not a mapping",
            ha_user_id,
        )
        return _CLOSED_SCOPE
    parsed: dict[str, Any] = {}
    for key in ("areas", "domains", "entities", "deny_entities"):
        value = _id_set(raw, key)
        if value is _MALFORMED:
            _LOGGER.warning(
                "Ignoring malformed llm_scopes entry for user %s: %s must be "
                "a list of strings",
                ha_user_id,
                key,
            )
            return _CLOSED_SCOPE
        parsed[key] = value
    return ProfileScope(
        areas=parsed["areas"],
        domains=parsed["domains"],
        entities=parsed["entities"],
        deny_entities=parsed["deny_entities"] or frozenset(),
        read_only=bool(raw.get("read_only", False)),
    )


def resolve_scope(hass: HomeAssistant, ha_user_id: str) -> ProfileScope:
    """The configured scope for an HA user, or the unrestricted default.

    The default still enforces exposure and the user's own Home Assistant
    entity permissions; a configured scope can only narrow it further.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        scopes = entry.options.get(CONF_LLM_SCOPES)
        if isinstance(scopes, dict) and ha_user_id in scopes:
            return _parse_scope(scopes[ha_user_id], ha_user_id)
    return ProfileScope()


def _entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Area an entity belongs to, directly or through its device."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry is None:
        return None
    if entity_entry.area_id is not None:
        return entity_entry.area_id
    if entity_entry.device_id is not None:
        device = dr.async_get(hass).async_get(entity_entry.device_id)
        if device is not None:
            return device.area_id
    return None


def _entity_visible(
    hass: HomeAssistant,
    scope: ProfileScope,
    user: User,
    assistant: str,
    entity_id: str,
    policy: str,
) -> bool:
    """Scope + exposure + the user's own entity permission, all required."""
    if not scope.allows(entity_id, _entity_area_id(hass, entity_id)):
        return False
    if not async_should_expose(hass, assistant, entity_id):
        return False
    return user.permissions.check_entity(entity_id, policy)


@dataclass(frozen=True)
class _VisibleEntity:
    entity_id: str
    name: str
    domain: str
    area_name: str | None


def _visible_entities(
    hass: HomeAssistant, scope: ProfileScope, user: User, assistant: str
) -> list[_VisibleEntity]:
    """Exposed entities the scope covers and the user may read."""
    area_reg = ar.async_get(hass)
    visible = []
    for state in sorted(hass.states.async_all(), key=attrgetter("entity_id")):
        if not _entity_visible(
            hass, scope, user, assistant, state.entity_id, POLICY_READ
        ):
            continue
        area_id = _entity_area_id(hass, state.entity_id)
        area = area_reg.async_get_area(area_id) if area_id is not None else None
        visible.append(
            _VisibleEntity(
                entity_id=state.entity_id,
                name=state.name,
                domain=state.domain,
                area_name=area.name if area is not None else None,
            )
        )
    return visible


def _build_prompt(entities: list[_VisibleEntity]) -> str:
    if not entities:
        return _NO_ENTITIES_PROMPT
    lines = [
        _PROMPT_INTRO,
        "",
        "Static Context: the entities this speaker may see and control:",
    ]
    for entity in entities:
        line = f"- {entity.name} ({entity.entity_id}), domain: {entity.domain}"
        if entity.area_name is not None:
            line += f", area: {entity.area_name}"
        lines.append(line)
    return "\n".join(lines)


async def _async_caller(
    hass: HomeAssistant, llm_context: llm.LLMContext
) -> tuple[User, ProfileScope]:
    """Resolve the requesting HA user and their scope, or raise.

    Fails closed: an LLM context without a user id, or naming a user that
    does not exist or is inactive, gets nothing.
    """
    context = llm_context.context
    if context is None or not context.user_id:
        raise HomeAssistantError(
            "Discord Assist API requires an identified Home Assistant user"
        )
    user = await hass.auth.async_get_user(context.user_id)
    if user is None or not user.is_active:
        raise HomeAssistantError(
            f"Discord Assist API: unknown or inactive user {context.user_id}"
        )
    return user, resolve_scope(hass, user.id)


class _ScopedTool(llm.Tool):
    """Base for Discord Assist tools: every call re-checks the scope.

    The tool list an instance hands out is already filtered, but a tool can
    still be invoked by name through a stale or merged instance, so the
    check lives here too, at execution time.
    """

    state_changing = False

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Re-check the caller's scope, then run the tool."""
        user, scope = await _async_caller(hass, llm_context)
        if self.state_changing and scope.read_only:
            raise HomeAssistantError(
                f"Tool {self.name} changes state and is not available to a "
                "read-only scope"
            )
        return await self._async_call_scoped(hass, tool_input, llm_context, user, scope)

    async def _async_call_scoped(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
        user: User,
        scope: ProfileScope,
    ) -> JsonObjectType:
        raise NotImplementedError


class _EntityTool(_ScopedTool):
    """A scoped tool that targets one entity, re-checked before it runs."""

    parameters = vol.Schema({vol.Required("entity_id"): cv.entity_id})
    _policy = POLICY_READ

    async def _async_call_scoped(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
        user: User,
        scope: ProfileScope,
    ) -> JsonObjectType:
        try:
            data = self.parameters(tool_input.tool_args)
        except vol.Invalid as err:
            raise HomeAssistantError(
                f"Invalid arguments for {self.name}: {err}"
            ) from err
        entity_id = data["entity_id"]
        assistant = llm_context.assistant or _DEFAULT_ASSISTANT
        if not _entity_visible(hass, scope, user, assistant, entity_id, self._policy):
            raise HomeAssistantError(
                f"Entity {entity_id} is not available to this user"
            )
        return await self._async_call_entity(hass, entity_id, llm_context)

    async def _async_call_entity(
        self, hass: HomeAssistant, entity_id: str, llm_context: llm.LLMContext
    ) -> JsonObjectType:
        raise NotImplementedError


def _json_safe(value: Any) -> Any:
    """Coerce a state attribute value into something JSON can carry."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


class GetStateTool(_EntityTool):
    """Read the current state of one in-scope entity."""

    name = "GetState"
    description = (
        "Get the current state and attributes of one Home Assistant entity "
        "by entity id. Only entities in the speaker's scope can be read."
    )
    _policy = POLICY_READ

    async def _async_call_entity(
        self, hass: HomeAssistant, entity_id: str, llm_context: llm.LLMContext
    ) -> JsonObjectType:
        state = hass.states.get(entity_id)
        if state is None:
            raise HomeAssistantError(f"Entity {entity_id} has no state")
        return {
            "entity_id": entity_id,
            "state": state.state,
            "attributes": _json_safe(dict(state.attributes)),
        }


class _ControlTool(_EntityTool):
    """Turn an in-scope entity on or off through the homeassistant domain."""

    state_changing = True
    _policy = POLICY_CONTROL
    _service: str

    async def _async_call_entity(
        self, hass: HomeAssistant, entity_id: str, llm_context: llm.LLMContext
    ) -> JsonObjectType:
        await hass.services.async_call(
            "homeassistant",
            self._service,
            {"entity_id": entity_id},
            context=llm_context.context,
            blocking=True,
        )
        return {"success": True}


class TurnOnTool(_ControlTool):
    """Turn one in-scope entity on."""

    name = "TurnOn"
    description = "Turn on one Home Assistant entity by entity id."
    _service = "turn_on"


class TurnOffTool(_ControlTool):
    """Turn one in-scope entity off."""

    name = "TurnOff"
    description = "Turn off one Home Assistant entity by entity id."
    _service = "turn_off"


class DiscordAssistAPI(llm.API):
    """LLM API that scopes tools and entities to the requesting user."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init the class."""
        super().__init__(hass=hass, id=LLM_API_ID, name=LLM_API_NAME)

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Build the instance for one request: scoped tools and prompt."""
        try:
            user, scope = await _async_caller(self.hass, llm_context)
        except HomeAssistantError:
            # No identified user: fail closed with an empty tool list.
            return llm.APIInstance(
                api=self,
                api_prompt=_NO_ENTITIES_PROMPT,
                llm_context=llm_context,
                tools=[],
            )
        assistant = llm_context.assistant or _DEFAULT_ASSISTANT
        entities = _visible_entities(self.hass, scope, user, assistant)
        tools: list[llm.Tool] = [llm.GetDateTimeTool(), GetStateTool()]
        if not scope.read_only:
            tools.extend([TurnOnTool(), TurnOffTool()])
        return llm.APIInstance(
            api=self,
            api_prompt=_build_prompt(entities),
            llm_context=llm_context,
            tools=tools,
            custom_serializer=llm.selector_serializer,
        )


def async_setup_llm_api(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the scoped LLM API for as long as any entry is loaded.

    The API is registered once, on the first entry to set up, and removed
    when the last entry unloads (via ``entry.async_on_unload``).
    """
    data = hass.data.setdefault(DOMAIN, {})
    state = data.setdefault("llm_api", {"refs": 0, "unregister": None})
    if state["refs"] == 0:
        state["unregister"] = llm.async_register_api(hass, DiscordAssistAPI(hass=hass))
        _LOGGER.debug("Registered the Discord Assist scoped LLM API")
    state["refs"] += 1
    entry.async_on_unload(lambda: _release_llm_api(hass))


def _release_llm_api(hass: HomeAssistant) -> None:
    """Drop one entry's hold on the API; unregister when none remain."""
    data = hass.data.get(DOMAIN, {})
    state = data.get("llm_api")
    if not state:
        return
    state["refs"] -= 1
    if state["refs"] <= 0:
        data.pop("llm_api", None)
        if state["unregister"] is not None:
            state["unregister"]()
            _LOGGER.debug("Unregistered the Discord Assist scoped LLM API")
