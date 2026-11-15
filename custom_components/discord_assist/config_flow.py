"""Config and options flow for Discord Assist."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AGENT_ALLOWED_TOOLS,
    CONF_AGENT_API_KEY,
    CONF_AGENT_BASE_URL,
    CONF_AGENT_CONFIRM_TOOLS,
    CONF_AGENT_ID,
    CONF_AGENT_LLM_APIS,
    CONF_AGENT_MAX_TOKENS,
    CONF_AGENT_MAX_TOOL_CALLS,
    CONF_AGENT_MODEL,
    CONF_AGENT_OWNER_TOOLS,
    CONF_AGENT_OWNER_USERS,
    CONF_AGENT_TEMPERATURE,
    CONF_AGENT_TIMEOUT,
    CONF_ALLOWLIST,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_FALLBACK_USER,
    CONF_IDLE_MINUTES,
    CONF_LANGUAGE,
    CONF_PREFER_LOCAL,
    CONF_RESPOND_DMS,
    CONF_RESPOND_MENTIONS,
    CONF_ROOM_MAP,
    CONF_SPEAKER_CONTEXT,
    CONF_TOKEN,
    CONF_USER_MAP,
    DEFAULT_AGENT_MAX_TOKENS,
    DEFAULT_AGENT_MAX_TOOL_CALLS,
    DEFAULT_AGENT_TEMPERATURE,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PREFER_LOCAL,
    DEFAULT_RESPOND_DMS,
    DEFAULT_RESPOND_MENTIONS,
    DEFAULT_SPEAKER_CONTEXT,
    DOMAIN,
    LEGACY_DOMAIN,
)
from .discord_api import CannotConnect, InvalidAuth, list_text_channels, validate_token

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


def build_config_schema(channel_options: list[SelectOptionDict]) -> vol.Schema:
    """Schema for the main settings form (shared by config + options flows)."""
    return vol.Schema(
        {
            vol.Required(CONF_AGENT_ID): EntitySelector(
                EntitySelectorConfig(
                    filter=EntityFilterSelectorConfig(domain="conversation")
                )
            ),
            vol.Optional(CONF_CHANNELS, default=[]): SelectSelector(
                SelectSelectorConfig(
                    options=channel_options, multiple=True, mode=SelectSelectorMode.LIST
                )
            ),
            vol.Optional(
                CONF_RESPOND_DMS, default=DEFAULT_RESPOND_DMS
            ): BooleanSelector(),
            vol.Optional(
                CONF_RESPOND_MENTIONS, default=DEFAULT_RESPOND_MENTIONS
            ): BooleanSelector(),
            vol.Optional(
                CONF_IDLE_MINUTES, default=DEFAULT_IDLE_MINUTES
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=240, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(CONF_LANGUAGE, default=""): TextSelector(),
            vol.Optional(CONF_ALLOWLIST, default=[]): SelectSelector(
                SelectSelectorConfig(options=[], multiple=True, custom_value=True)
            ),
            vol.Optional(
                CONF_SPEAKER_CONTEXT, default=DEFAULT_SPEAKER_CONTEXT
            ): BooleanSelector(),
            vol.Optional(
                CONF_PREFER_LOCAL, default=DEFAULT_PREFER_LOCAL
            ): BooleanSelector(),
            vol.Optional(
                CONF_AGENT_TIMEOUT, default=DEFAULT_AGENT_TIMEOUT
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5,
                    max=600,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


class DiscordAssistConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Discord Assist config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._token: str | None = None
        self._bot_id: str | None = None

    def _importable_legacy_entries(self) -> list[ConfigEntry]:
        """Discord Conversation entries whose bot is not set up here yet."""
        configured = {e.unique_id for e in self._async_current_entries()}
        return [
            e
            for e in self.hass.config_entries.async_entries(LEGACY_DOMAIN)
            if e.unique_id not in configured and CONF_TOKEN in e.data
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None and self._importable_legacy_entries():
            return self.async_show_menu(
                step_id="user", menu_options=["import_legacy", "token"]
            )
        return await self.async_step_token(user_input)

    async def async_step_import_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Copy token and settings from a Discord Conversation entry.

        The old entry is disabled, not deleted, so the same bot token is never
        driven by two integrations at once and the move can be undone.
        """
        legacy = self._importable_legacy_entries()
        if not legacy:
            return self.async_abort(reason="no_legacy_entry")
        old = legacy[0]
        try:
            bot_id = await validate_token(old.data[CONF_TOKEN])
        except InvalidAuth:
            return self.async_abort(reason="legacy_invalid_auth")
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(bot_id)
        self._abort_if_unique_id_configured()
        await self.hass.config_entries.async_set_disabled_by(
            old.entry_id, ConfigEntryDisabler.USER
        )
        return self.async_create_entry(
            title="Discord Assist",
            data={CONF_TOKEN: old.data[CONF_TOKEN], CONF_BOT_ID: bot_id},
            options=dict(old.options),
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                bot_id = await validate_token(user_input[CONF_TOKEN])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(bot_id)
                self._abort_if_unique_id_configured()
                self._token = user_input[CONF_TOKEN]
                self._bot_id = bot_id
                return await self.async_step_config()

        return self.async_show_form(
            step_id="token", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._token is not None
        if user_input is not None:
            options = dict(user_input)
            options[CONF_LANGUAGE] = options.get(CONF_LANGUAGE) or None
            options.setdefault(CONF_USER_MAP, {})
            options.setdefault(CONF_FALLBACK_USER, None)
            return self.async_create_entry(
                title="Discord Assist",
                data={CONF_TOKEN: self._token, CONF_BOT_ID: self._bot_id},
                options=options,
            )

        try:
            channels = await list_text_channels(self._token)
        except InvalidAuth, CannotConnect:
            channels = []
        options = [SelectOptionDict(value=cid, label=label) for cid, label in channels]
        return self.async_show_form(
            step_id="config", data_schema=build_config_schema(options)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DiscordAssistOptionsFlow()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                bot_id = await validate_token(user_input[CONF_TOKEN])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if bot_id != self._get_reauth_entry().unique_id:
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_TOKEN: user_input[CONF_TOKEN]},
                    )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA, errors=errors
        )


async def _ha_user_options(hass: HomeAssistant) -> list[SelectOptionDict]:
    """Build SelectOptionDict list from active, non-system HA users."""
    users = await hass.auth.async_get_users()
    return [
        SelectOptionDict(value=user.id, label=user.name or user.id)
        for user in users
        if user.is_active and not user.system_generated
    ]


class DiscordAssistOptionsFlow(OptionsFlowWithReload):
    """Options: general settings, user mappings, and room channels."""

    async def _channel_options(self) -> list[SelectOptionDict]:
        try:
            channels = await list_text_channels(self.config_entry.data[CONF_TOKEN])
        except InvalidAuth, CannotConnect:
            channels = []
        return [SelectOptionDict(value=cid, label=label) for cid, label in channels]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "settings",
                "agent",
                "add_user_map",
                "remove_user_map",
                "add_room",
                "remove_room",
            ],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            merged = dict(self.config_entry.options)
            merged.update(user_input)
            merged[CONF_LANGUAGE] = merged.get(CONF_LANGUAGE) or None
            return self.async_create_entry(title="", data=merged)

        channel_options = await self._channel_options()
        user_options = await _ha_user_options(self.hass)
        schema = build_config_schema(channel_options).extend(
            {
                vol.Optional(CONF_FALLBACK_USER): SelectSelector(
                    SelectSelectorConfig(
                        options=user_options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )

    async def async_step_agent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bundled agent: model endpoint, tool sources, and the tool gate.

        The API key is a secret: it is stored in the entry's data, never in
        its options, and the form never echoes it back. Leaving it blank
        keeps the stored key.
        """
        if user_input is not None:
            merged = dict(self.config_entry.options)
            api_key = user_input.pop(CONF_AGENT_API_KEY, None)
            if api_key:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_AGENT_API_KEY: api_key},
                )
            for key, value in user_input.items():
                # An empty API list is meaningful (no tools at all), unlike
                # the other fields where empty just clears the setting.
                if key != CONF_AGENT_LLM_APIS and value in ("", []):
                    merged.pop(key, None)
                else:
                    merged[key] = value
            return self.async_create_entry(title="", data=merged)

        api_options = [
            SelectOptionDict(value=api.id, label=api.name)
            for api in llm.async_get_apis(self.hass)
        ]
        user_options = await _ha_user_options(self.hass)

        def _multi_custom() -> SelectSelector:
            return SelectSelector(
                SelectSelectorConfig(options=[], multiple=True, custom_value=True)
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_AGENT_BASE_URL, default=""): TextSelector(),
                vol.Optional(CONF_AGENT_MODEL, default=""): TextSelector(),
                vol.Optional(CONF_AGENT_API_KEY, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_AGENT_MAX_TOKENS, default=DEFAULT_AGENT_MAX_TOKENS
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=32768, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_AGENT_TEMPERATURE, default=DEFAULT_AGENT_TEMPERATURE
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=2, step=0.1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_AGENT_MAX_TOOL_CALLS, default=DEFAULT_AGENT_MAX_TOOL_CALLS
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=25, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_AGENT_LLM_APIS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=api_options,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(CONF_AGENT_ALLOWED_TOOLS, default=[]): _multi_custom(),
                vol.Optional(CONF_AGENT_OWNER_TOOLS, default=[]): _multi_custom(),
                vol.Optional(CONF_AGENT_CONFIRM_TOOLS, default=[]): _multi_custom(),
                vol.Optional(CONF_AGENT_OWNER_USERS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=user_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        suggested = dict(self.config_entry.options)
        # Never suggest the stored API key back into the form.
        suggested[CONF_AGENT_API_KEY] = ""
        if CONF_AGENT_LLM_APIS not in suggested:
            suggested[CONF_AGENT_LLM_APIS] = [llm.LLM_API_ASSIST]
        return self.async_show_form(
            step_id="agent",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
        )

    async def async_step_add_user_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not str(user_input["discord_user_id"]).isdigit():
                errors["base"] = "invalid_user_id"
            else:
                merged = dict(self.config_entry.options)
                user_map = dict(merged.get(CONF_USER_MAP, {}))
                discord_uid = str(user_input["discord_user_id"])
                user_map[discord_uid] = user_input[CONF_FALLBACK_USER]
                merged[CONF_USER_MAP] = user_map
                return self.async_create_entry(title="", data=merged)

        user_options = await _ha_user_options(self.hass)
        schema = vol.Schema(
            {
                vol.Required("discord_user_id"): TextSelector(),
                vol.Required(CONF_FALLBACK_USER): SelectSelector(
                    SelectSelectorConfig(
                        options=user_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="add_user_map", data_schema=schema, errors=errors
        )

    async def async_step_remove_user_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        user_map = dict(self.config_entry.options.get(CONF_USER_MAP, {}))
        if user_input is not None:
            user_map.pop(str(user_input["discord_user_id"]), None)
            merged = dict(self.config_entry.options)
            merged[CONF_USER_MAP] = user_map
            return self.async_create_entry(title="", data=merged)

        schema = vol.Schema({vol.Required("discord_user_id"): vol.In(sorted(user_map))})
        return self.async_show_form(step_id="remove_user_map", data_schema=schema)

    async def async_step_add_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Treat a Discord channel as a room: requests there act like a satellite.

        The device's area becomes the default room, the same way a voice
        satellite in that area works. Threads inherit their channel's room.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            channel_id = str(user_input["channel_id"]).strip()
            if not channel_id.isdigit():
                errors["base"] = "invalid_channel_id"
            else:
                merged = dict(self.config_entry.options)
                room_map = dict(merged.get(CONF_ROOM_MAP, {}))
                room_map[channel_id] = user_input["device_id"]
                merged[CONF_ROOM_MAP] = room_map
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Required("channel_id"): SelectSelector(
                    SelectSelectorConfig(
                        options=await self._channel_options(),
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required("device_id"): DeviceSelector(DeviceSelectorConfig()),
            }
        )
        return self.async_show_form(
            step_id="add_room", data_schema=schema, errors=errors
        )

    async def async_step_remove_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        room_map = dict(self.config_entry.options.get(CONF_ROOM_MAP, {}))
        if user_input is not None:
            room_map.pop(str(user_input["channel_id"]), None)
            merged = dict(self.config_entry.options)
            merged[CONF_ROOM_MAP] = room_map
            return self.async_create_entry(title="", data=merged)
        if not room_map:
            return self.async_abort(reason="no_rooms")
        schema = vol.Schema({vol.Required("channel_id"): vol.In(sorted(room_map))})
        return self.async_show_form(step_id="remove_room", data_schema=schema)
