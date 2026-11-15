"""Bundled conversation agent entity for Discord Assist.

The agent is a normal ConversationEntity on the Discord Assist entry, so
any pipeline (Discord, Assist chat, voice) can select it. It answers from
an OpenAI-compatible chat completions endpoint and gets its tools from
Home Assistant LLM APIs, with every tool call passing through the gate in
agent.py.
"""

from __future__ import annotations

import logging
from typing import Literal

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import AssistantContent
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DiscordConfigEntry
from .agent import (
    AgentSettings,
    async_gate_api_instance,
    async_request_completion,
    format_tools,
    messages_from_content,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DiscordConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bundled conversation agent for an entry."""
    async_add_entities([DiscordAssistConversationEntity(entry)])


class DiscordAssistConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Conversation agent backed by an OpenAI-compatible endpoint."""

    _attr_has_entity_name = True
    _attr_translation_key = "agent"
    _attr_supports_streaming = False

    def __init__(self, entry: DiscordConfigEntry) -> None:
        """Initialize the agent."""
        self.entry = entry
        self._attr_unique_id = f"{entry.unique_id}_agent"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=entry.title,
            manufacturer="Discord",
            entry_type=DeviceEntryType.SERVICE,
        )
        if AgentSettings.from_entry(entry).api_ids:
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process the user input and call the model endpoint."""
        settings = AgentSettings.from_entry(self.entry)
        if not settings.configured:
            return self._error_result(
                user_input,
                chat_log,
                "The Discord Assist agent is not configured yet. Open the "
                "integration options and set a model endpoint and model name.",
            )

        # Fail closed: without a Home Assistant user there is no identity
        # to check permissions against, so the model gets no tools at all.
        user_id = user_input.context.user_id if user_input.context else None
        api_ids = settings.api_ids if user_id else []
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                api_ids or None,
                None,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        if chat_log.llm_api is not None:
            chat_log.llm_api = await async_gate_api_instance(
                self.hass,
                chat_log.llm_api,
                settings=settings,
                user_id=user_id,
                config_entry_id=self.entry.entry_id,
                agent_id=self.entity_id,
            )

        try:
            await self._async_run_model_loop(chat_log, settings)
        except HomeAssistantError as err:
            _LOGGER.warning("Bundled agent failed: %s", err)
            return self._error_result(
                user_input, chat_log, "Sorry, I could not get an answer from the model."
            )

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_run_model_loop(
        self,
        chat_log: conversation.ChatLog,
        settings: AgentSettings,
    ) -> None:
        """Call the model, run its tool calls through the gate, repeat."""
        tool_budget = settings.max_tool_calls
        while True:
            tools: list[dict] = []
            if chat_log.llm_api is not None and tool_budget > 0:
                tools = format_tools(
                    chat_log.llm_api.tools, chat_log.llm_api.custom_serializer
                )
            reply = await async_request_completion(
                self.hass,
                settings,
                messages_from_content(chat_log.content),
                tools,
            )
            if not reply.tool_calls or tool_budget <= 0 or chat_log.llm_api is None:
                chat_log.async_add_assistant_content_without_tools(
                    AssistantContent(agent_id=self.entity_id, content=reply.content)
                )
                return
            calls = reply.tool_calls[:tool_budget]
            tool_budget -= len(calls)
            content = AssistantContent(
                agent_id=self.entity_id,
                content=reply.content,
                tool_calls=calls,
            )
            async for _tool_result in chat_log.async_add_assistant_content(content):
                pass

    def _error_result(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        message: str,
    ) -> conversation.ConversationResult:
        """Build a normal conversation error result (no crash, no speech)."""
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
        return conversation.ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
        )
