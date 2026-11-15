"""Bridge to the in-process ha-mcp server's Home Assistant LLM API.

The bundled agent (issue #40) gets its tools from ha-mcp. ha-mcp can run as
an app (add-on) over HTTP or in-process as the ``ha_mcp_tools`` integration;
only the in-process server registers its toolset as a Home Assistant LLM API,
so Discord Assist uses that API and needs no MCP client, URL, or token
(issue #41).

ha-mcp registers one API per server entry and exposure mode:

- full catalog: id ``ha_mcp_tools-<entry_id>``, name = the entry title
- tool search: id ``ha_mcp_tools-<entry_id>-toolsearch``

Discord Assist always picks the full-catalog API (the issue #41 setup asks
for exposure "Both" so it exists) and filters the catalog per caller itself.

ha-mcp executes tools as one administrator account, so this bridge is the
security boundary for Discord callers:

- A call with no Home Assistant user in its Context is refused outright.
  Nothing ever runs identity-less.
- Administrators get the full catalog; Home Assistant already grants them
  every entity.
- For everyone else, a tool bound to one entity (it carries an
  ``entity_id`` attribute) is only listed when that entity is exposed to
  the conversation assistant and the user may control it, and a call whose
  arguments name entities is refused unless the same holds for every
  entity it names. Generic tools are listed for any known user; their
  entity arguments are checked at call time.

Per-profile scoping (areas, domains, read-only profiles) belongs to the
permissions work in issues #12 and #34; when profiles land, their filter
plugs into :func:`filter_tools` and the argument check in
:meth:`HaMcpBridge.async_call_tool` narrows further. It can only narrow:
the checks here stay as the floor.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.core import Context, HomeAssistant, split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The ha_mcp_tools integration domain; its LLM API ids are built from it.
HA_MCP_DOMAIN = "ha_mcp_tools"
HA_MCP_API_ID_PREFIX = f"{HA_MCP_DOMAIN}-"
TOOL_SEARCH_SUFFIX = "-toolsearch"

# Exposure is curated per assistant; Discord callers arrive through the
# conversation assistant, like every other Assist surface.
ASSISTANT = "conversation"


def _is_ha_mcp_api(api: llm.API) -> bool:
    """Whether a registered LLM API belongs to the in-process ha-mcp server."""
    if api.id == HA_MCP_DOMAIN or api.id.startswith(HA_MCP_API_ID_PREFIX):
        return True
    # Fallback for a registration whose id does not follow the scheme above:
    # ha-mcp names its APIs after the server entry, whose default title is
    # "HA-MCP Server". The id match above is authoritative; this only runs
    # when no id matched at all.
    return "ha-mcp" in api.name.lower()


def find_ha_mcp_api(
    hass: HomeAssistant, preferred_id: str | None = None
) -> llm.API | None:
    """Find the full-catalog ha-mcp LLM API among the registered APIs.

    With ``preferred_id`` (the options picker), that exact API is used and
    there is no fallback: a picked API that is gone means ha-mcp is not
    available. Without one, ha-mcp APIs are matched by id, the full-catalog
    registration is preferred over the tool-search one, and ties break by
    id so the choice is deterministic.
    """
    apis = llm.async_get_apis(hass)
    if preferred_id:
        for api in apis:
            if api.id == preferred_id:
                return api
        return None

    by_id = [api for api in apis if api.id.startswith(HA_MCP_API_ID_PREFIX)]
    candidates = by_id or [api for api in apis if _is_ha_mcp_api(api)]
    full = [api for api in candidates if not api.id.endswith(TOOL_SEARCH_SUFFIX)]
    pool = full or candidates
    if not pool:
        return None
    return sorted(pool, key=lambda api: api.id)[0]


def _bound_entity_id(tool: llm.Tool) -> str | None:
    """The single entity a tool acts on, if the tool declares one."""
    entity_id = getattr(tool, "entity_id", None)
    return entity_id if isinstance(entity_id, str) else None


def _entity_allowed(hass: HomeAssistant, user: Any, entity_id: str) -> bool:
    """Whether this user may use a tool on this entity through Discord.

    Administrators may use everything. Everyone else needs the entity
    exposed to the conversation assistant and control permission on it:
    ha-mcp runs as an administrator, so a Discord caller must bring their
    own right to control what a call touches.
    """
    if user.is_admin:
        return True
    try:
        if not async_should_expose(hass, ASSISTANT, entity_id):
            return False
    except KeyError, HomeAssistantError:
        # Exposure data unavailable: fail closed.
        return False
    return bool(user.permissions.check_entity(entity_id, POLICY_CONTROL))


def filter_tools(
    hass: HomeAssistant, tools: list[llm.Tool], user: Any | None
) -> list[llm.Tool]:
    """Filter an ha-mcp catalog down to what one caller may be offered.

    No user, no tools. Administrators get the full catalog. For other
    users, tools bound to an entity they may not use are removed; generic
    tools stay and are checked per call in ``async_call_tool``.
    """
    if user is None:
        return []
    if user.is_admin:
        return list(tools)
    return [
        tool
        for tool in tools
        if (entity_id := _bound_entity_id(tool)) is None
        or _entity_allowed(hass, user, entity_id)
    ]


def _arg_entity_ids(tool_args: dict[str, Any]) -> list[str]:
    """Entity ids named by tool arguments (entity_id(s) or a target dict)."""
    found: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            try:
                split_entity_id(value)
            except ValueError:
                return
            found.append(value)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)

    add(tool_args.get("entity_id"))
    add(tool_args.get("entity_ids"))
    target = tool_args.get("target")
    if isinstance(target, dict):
        add(target.get("entity_id"))
        add(target.get("entity_ids"))
    return found


def _error(message: str) -> JsonObjectType:
    """A tool error result: the shape HA tools use instead of raising."""
    return {"success": False, "error": message}


class HaMcpBridge:
    """Discovery, per-caller filtering, and call routing for ha-mcp tools.

    The API is resolved fresh on every use, never cached: if the ha-mcp
    entry is unloaded mid-conversation, the next call returns a tool error
    instead of crashing or calling into a dead registration.
    """

    def __init__(self, hass: HomeAssistant, *, preferred_api_id: str | None = None):
        self.hass = hass
        self.preferred_api_id = preferred_api_id or None

    @property
    def api(self) -> llm.API | None:
        """The ha-mcp LLM API to use right now, or None if not registered."""
        return find_ha_mcp_api(self.hass, self.preferred_api_id)

    @property
    def available(self) -> bool:
        """Whether an ha-mcp LLM API is currently registered."""
        return self.api is not None

    def _llm_context(
        self, context: Context, language: str | None, device_id: str | None
    ) -> llm.LLMContext:
        return llm.LLMContext(
            platform=DOMAIN,
            context=context,
            language=language,
            assistant=ASSISTANT,
            device_id=device_id,
        )

    async def async_get_instance(
        self,
        context: Context,
        *,
        language: str | None = None,
        device_id: str | None = None,
    ) -> llm.APIInstance | None:
        """The ha-mcp API instance with its tools filtered for this caller.

        Raises PermissionError when the context carries no Home Assistant
        user. Returns None when no ha-mcp API is registered (or it fails to
        build an instance), meaning "no ha-mcp tools right now".
        """
        if context is None or not context.user_id:
            raise PermissionError("ha-mcp tools require a Home Assistant user")
        api = self.api
        if api is None:
            return None
        user = await self.hass.auth.async_get_user(context.user_id)
        if user is None or not user.is_active:
            return None
        try:
            instance = await api.async_get_api_instance(
                self._llm_context(context, language, device_id)
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not load the ha-mcp tool catalog: %s", err)
            return None
        return llm.APIInstance(
            api=instance.api,
            api_prompt=instance.api_prompt,
            llm_context=instance.llm_context,
            tools=filter_tools(self.hass, instance.tools, user),
            custom_serializer=instance.custom_serializer,
        )

    async def async_get_tools(
        self,
        context: Context,
        *,
        language: str | None = None,
        device_id: str | None = None,
    ) -> list[llm.Tool]:
        """The ha-mcp tools this caller may be offered ([] when none)."""
        instance = await self.async_get_instance(
            context, language=language, device_id=device_id
        )
        return instance.tools if instance is not None else []

    async def async_call_tool(
        self,
        context: Context,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        language: str | None = None,
        device_id: str | None = None,
    ) -> JsonObjectType:
        """Run one ha-mcp tool for a caller, through the gate.

        Every failure mode (no identity, unknown user, API gone, tool not
        offered to this caller, entity not allowed, the API itself erroring)
        comes back as a tool error dict, never an exception, so a
        conversation can report it and continue.
        """
        if context is None or not context.user_id:
            return _error(
                "Tool calls from Discord Assist always run as a Home "
                "Assistant user; refusing to run without one."
            )
        user = await self.hass.auth.async_get_user(context.user_id)
        if user is None or not user.is_active:
            return _error("The Home Assistant user for this caller is not available.")

        api = self.api
        if api is None:
            return _error(
                "The ha-mcp tools are not available: no ha-mcp LLM API is "
                "registered. The HA-MCP Server entry may have been unloaded."
            )
        try:
            instance = await api.async_get_api_instance(
                self._llm_context(context, language, device_id)
            )
        except HomeAssistantError as err:
            return _error(f"The ha-mcp tools could not be loaded: {err}")

        tools = filter_tools(self.hass, instance.tools, user)
        if all(tool.name != tool_name for tool in tools):
            return _error(f'Tool "{tool_name}" is not available to this caller.')

        for entity_id in _arg_entity_ids(tool_args):
            if not _entity_allowed(self.hass, user, entity_id):
                return _error(
                    f"{tool_name} may not be used on {entity_id}: the entity "
                    "is not exposed to Assist or the user may not control it."
                )

        gated = llm.APIInstance(
            api=instance.api,
            api_prompt=instance.api_prompt,
            llm_context=instance.llm_context,
            tools=tools,
            custom_serializer=instance.custom_serializer,
        )
        try:
            return await gated.async_call_tool(
                llm.ToolInput(tool_name=tool_name, tool_args=dict(tool_args))
            )
        except HomeAssistantError as err:
            return _error(str(err))
