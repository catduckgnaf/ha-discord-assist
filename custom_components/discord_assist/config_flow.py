"""Config and options flow for Discord Assist."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
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
    CONF_AGENT_ID,
    CONF_AGENT_MAP_CHANNEL,
    CONF_AGENT_MAP_USER,
    CONF_AGENT_TIMEOUT,
    CONF_ALLOWLIST,
    CONF_BOT_ID,
    CONF_CHANNEL_IDLE_MINUTES,
    CONF_CHANNELS,
    CONF_CONVERSATION_SCOPE,
    CONF_EVENT_EXCLUDE_TEXT,
    CONF_FALLBACK_USER,
    CONF_IDLE_MINUTES,
    CONF_LANGUAGE,
    CONF_PLACEHOLDER_DELAY,
    CONF_PREFER_LOCAL,
    CONF_RATE_LIMIT_BURST,
    CONF_RATE_LIMIT_PER_MINUTE,
    CONF_RESPOND_DMS,
    CONF_RESPOND_MENTIONS,
    CONF_ROOM_MAP,
    CONF_SPEAKER_CONTEXT,
    CONF_TOKEN,
    CONF_USER_MAP,
    CONVERSATION_SCOPE_CHANNEL,
    CONVERSATION_SCOPE_USER,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_CONVERSATION_SCOPE,
    DEFAULT_EVENT_EXCLUDE_TEXT,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PLACEHOLDER_DELAY,
    DEFAULT_PREFER_LOCAL,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RESPOND_DMS,
    DEFAULT_RESPOND_MENTIONS,
    DEFAULT_SPEAKER_CONTEXT,
    DOMAIN,
    LEGACY_DOMAIN,
)
from .discord_api import CannotConnect, InvalidAuth, list_text_channels, validate_token
from .notes import NotesStore


def _agent_selector() -> EntitySelector:
    """Conversation-agent picker, the same selector as the main agent field."""
    return EntitySelector(
        EntitySelectorConfig(filter=EntityFilterSelectorConfig(domain="conversation"))
    )


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
            vol.Required(CONF_AGENT_ID): _agent_selector(),
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
                CONF_EVENT_EXCLUDE_TEXT, default=DEFAULT_EVENT_EXCLUDE_TEXT
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
            vol.Optional(
                CONF_CONVERSATION_SCOPE, default=DEFAULT_CONVERSATION_SCOPE
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=CONVERSATION_SCOPE_CHANNEL,
                            label="Channel: one shared conversation per channel",
                        ),
                        SelectOptionDict(
                            value=CONVERSATION_SCOPE_USER,
                            label="User: one conversation per person in each channel",
                        ),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_RATE_LIMIT_PER_MINUTE, default=DEFAULT_RATE_LIMIT_PER_MINUTE
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=600,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="requests/min",
                )
            ),
            vol.Optional(
                CONF_RATE_LIMIT_BURST, default=DEFAULT_RATE_LIMIT_BURST
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=100, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_PLACEHOLDER_DELAY, default=DEFAULT_PLACEHOLDER_DELAY
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=60,
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
            options.setdefault(CONF_AGENT_MAP_USER, {})
            options.setdefault(CONF_AGENT_MAP_CHANNEL, {})
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
                "add_user_map",
                "remove_user_map",
                "add_room",
                "remove_room",
                "add_channel_idle",
                "remove_channel_idle",
                "add_agent_user",
                "remove_agent_user",
                "add_agent_channel",
                "remove_agent_channel",
                "remove_note",
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

    async def async_step_add_channel_idle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Override the idle reset for one channel (see CONF_CHANNEL_IDLE_MINUTES).

        The override only controls when Discord Assist drops the cached
        conversation id for that channel; it cannot extend Home Assistant's
        own 5 minute chat session limit for core agents.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            channel_id = str(user_input["channel_id"]).strip()
            try:
                minutes = int(user_input["idle_minutes"])
            except TypeError, ValueError:
                minutes = 0
            if not channel_id.isdigit():
                errors["base"] = "invalid_channel_id"
            elif not 1 <= minutes <= 240:
                errors["base"] = "invalid_idle_minutes"
            else:
                merged = dict(self.config_entry.options)
                idle_map = dict(merged.get(CONF_CHANNEL_IDLE_MINUTES, {}))
                idle_map[channel_id] = minutes
                merged[CONF_CHANNEL_IDLE_MINUTES] = idle_map
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
                vol.Required("idle_minutes", default=DEFAULT_IDLE_MINUTES): (
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=240, step=1, mode=NumberSelectorMode.BOX
                        )
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="add_channel_idle", data_schema=schema, errors=errors
        )

    async def async_step_remove_channel_idle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        idle_map = dict(self.config_entry.options.get(CONF_CHANNEL_IDLE_MINUTES, {}))
        if user_input is not None:
            idle_map.pop(str(user_input["channel_id"]), None)
            merged = dict(self.config_entry.options)
            merged[CONF_CHANNEL_IDLE_MINUTES] = idle_map
            return self.async_create_entry(title="", data=merged)
        if not idle_map:
            return self.async_abort(reason="no_channel_idle")
        schema = vol.Schema({vol.Required("channel_id"): vol.In(sorted(idle_map))})
        return self.async_show_form(step_id="remove_channel_idle", data_schema=schema)

    async def async_step_add_agent_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use a specific conversation agent for one Discord user.

        A user override wins over a channel override and the entry default
        (see ConversationRouter.agent_for).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            if not str(user_input["discord_user_id"]).isdigit():
                errors["base"] = "invalid_user_id"
            else:
                merged = dict(self.config_entry.options)
                agent_map = dict(merged.get(CONF_AGENT_MAP_USER, {}))
                agent_map[str(user_input["discord_user_id"])] = user_input[
                    CONF_AGENT_ID
                ]
                merged[CONF_AGENT_MAP_USER] = agent_map
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Required("discord_user_id"): TextSelector(),
                vol.Required(CONF_AGENT_ID): _agent_selector(),
            }
        )
        return self.async_show_form(
            step_id="add_agent_user", data_schema=schema, errors=errors
        )

    async def async_step_remove_agent_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        agent_map = dict(self.config_entry.options.get(CONF_AGENT_MAP_USER, {}))
        if user_input is not None:
            agent_map.pop(str(user_input["discord_user_id"]), None)
            merged = dict(self.config_entry.options)
            merged[CONF_AGENT_MAP_USER] = agent_map
            return self.async_create_entry(title="", data=merged)
        if not agent_map:
            return self.async_abort(reason="no_user_agents")
        schema = vol.Schema(
            {vol.Required("discord_user_id"): vol.In(sorted(agent_map))}
        )
        return self.async_show_form(step_id="remove_agent_user", data_schema=schema)

    async def async_step_add_agent_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use a specific conversation agent in one Discord channel.

        Threads inherit their parent channel's agent.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            channel_id = str(user_input["channel_id"]).strip()
            if not channel_id.isdigit():
                errors["base"] = "invalid_channel_id"
            else:
                merged = dict(self.config_entry.options)
                agent_map = dict(merged.get(CONF_AGENT_MAP_CHANNEL, {}))
                agent_map[channel_id] = user_input[CONF_AGENT_ID]
                merged[CONF_AGENT_MAP_CHANNEL] = agent_map
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
                vol.Required(CONF_AGENT_ID): _agent_selector(),
            }
        )
        return self.async_show_form(
            step_id="add_agent_channel", data_schema=schema, errors=errors
        )

    async def async_step_remove_agent_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        agent_map = dict(self.config_entry.options.get(CONF_AGENT_MAP_CHANNEL, {}))
        if user_input is not None:
            agent_map.pop(str(user_input["channel_id"]), None)
            merged = dict(self.config_entry.options)
            merged[CONF_AGENT_MAP_CHANNEL] = agent_map
            return self.async_create_entry(title="", data=merged)
        if not agent_map:
            return self.async_abort(reason="no_channel_agents")
        schema = vol.Schema({vol.Required("channel_id"): vol.In(sorted(agent_map))})
        return self.async_show_form(step_id="remove_agent_channel", data_schema=schema)

    async def async_step_remove_note(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show every user's standing preferences and remove the picked one.

        Notes live in the per-entry NotesStore, not in entry options, so the
        form lists them as labeled options (visible) and removal writes back
        to the store; the entry itself is returned unchanged.
        """
        # Use the running entry's store when it is loaded: a second store
        # would leave the bot injecting the removed note, and the next
        # remember/forget would write it back. Removing a note does not
        # change the options, so the entry is not reloaded.
        runtime = getattr(self.config_entry, "runtime_data", None)
        store = getattr(runtime, "notes_store", None)
        if store is None or self.config_entry.state is not ConfigEntryState.LOADED:
            store = NotesStore(self.hass, self.config_entry.entry_id)
        if user_input is not None:
            user_id, _, index = str(user_input["note_key"]).partition(":")
            try:
                await store.async_remove_note(int(user_id), int(index))
            except ValueError:
                pass
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )
        all_notes = await store.async_all_notes()
        if not all_notes:
            return self.async_abort(reason="no_notes")
        options = [
            SelectOptionDict(
                value=f"{user_id}:{index}",
                label=f"{user_id} #{index}: {note}",
            )
            for user_id, notes in sorted(all_notes.items())
            for index, note in enumerate(notes, start=1)
        ]
        schema = vol.Schema(
            {
                vol.Required("note_key"): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_note", data_schema=schema)
