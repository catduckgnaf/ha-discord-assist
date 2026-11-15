"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import logging

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .buttons import ButtonHandler, ButtonStore, parse_custom_id
from .conversation_router import (
    AgentTimeout,
    ConversationRouter,
    MessageContext,
    make_conversation_key,
)
from .text_utils import chunk_message, strip_self_mention

_LOGGER = logging.getLogger(__name__)

ERROR_REPLY = "Sorry, I hit an error talking to Home Assistant."
EMPTY_REPLY = "(no response)"
TIMEOUT_REPLY = "Sorry, Home Assistant took too long to answer. Try again?"
ALREADY_HANDLED_REPLY = "Already handled."
NOT_ALLOWED_REPLY = "You are not allowed to use this button."
HANDLED_REPLY = "Handled."
BUTTON_ERROR_REPLY = "Sorry, that button hit an error."


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        router: ConversationRouter,
        status_signal: str,
        *,
        button_store: ButtonStore | None = None,
        button_handlers: dict[str, ButtonHandler] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # Agent output is untrusted text: never let it ping @everyone, roles,
        # or users.
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self.hass = hass
        self.router = router
        self.connected = False
        self._status_signal = status_signal
        self.button_store = button_store
        self._button_handlers: dict[str, ButtonHandler] = dict(button_handlers or {})

    def register_button_handler(self, action: str, handler: ButtonHandler) -> None:
        """Register the coroutine run when a button with ``action`` is pressed."""
        self._button_handlers[action] = handler

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            async_dispatcher_send(self.hass, self._status_signal)

    async def on_ready(self) -> None:
        self._set_connected(True)

    async def on_resumed(self) -> None:
        self._set_connected(True)

    async def on_disconnect(self) -> None:
        self._set_connected(False)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        parent_id = (
            message.channel.parent_id
            if isinstance(message.channel, discord.Thread)
            else None
        )
        was_mentioned = self.user in message.mentions
        if not self.router.should_respond(
            is_dm=is_dm,
            was_mentioned=was_mentioned,
            channel_id=message.channel.id,
            author_id=message.author.id,
            parent_id=parent_id,
        ):
            return

        text = strip_self_mention(message.content, self.user.id)
        if not text:
            return

        key = make_conversation_key(
            is_dm=is_dm, channel_id=message.channel.id, author_id=message.author.id
        )
        where = MessageContext(
            display_name=getattr(message.author, "display_name", None),
            channel_name=None if is_dm else getattr(message.channel, "name", None),
            channel_id=message.channel.id,
            parent_id=parent_id,
        )
        reply = ERROR_REPLY
        try:
            reply = await self._answer(message, text, key, where)
        except AgentTimeout:
            reply = TIMEOUT_REPLY
        except Exception:  # noqa: BLE001 - never let the gateway die
            _LOGGER.exception("Conversation processing failed")
        await self._send_reply(message, reply or EMPTY_REPLY)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Dispatch a button press by its stateless custom_id.

        No View needs to be alive, so buttons keep working across restarts.
        Anything without our ``da:`` prefix belongs to another bot or feature
        and is ignored without acknowledging it.
        """
        data = getattr(interaction, "data", None) or {}
        custom_id = (
            data.get("custom_id")
            if isinstance(data, dict)
            else getattr(data, "custom_id", None)
        )
        parsed = parse_custom_id(custom_id)
        if parsed is None:
            return
        action, record_id = parsed

        # Acknowledge promptly (Discord allows about 3 seconds); the real
        # work and the visible result follow as a followup / message edit.
        await self._ack_interaction(interaction)

        if self.button_store is None:
            await self._respond_interaction(interaction, ALREADY_HANDLED_REPLY)
            return

        record = await self.button_store.peek(record_id)
        if record is None or record.action != action:
            await self._respond_interaction(interaction, ALREADY_HANDLED_REPLY)
            return

        # Authorization fails closed, and is checked before claiming so an
        # unauthorized press never consumes someone else's button.
        presser_id = getattr(getattr(interaction, "user", None), "id", None)
        authorized = (
            presser_id is not None
            and self.router.ha_user_for(presser_id) is not None
            and record.allows(presser_id)
        )
        if not authorized:
            await self._respond_interaction(interaction, NOT_ALLOWED_REPLY)
            return

        # Single-use: whoever takes the record runs the action; everyone
        # else (second press, double-click race) gets "Already handled".
        taken = await self.button_store.take(record_id)
        if taken is None or taken.action != action:
            await self._respond_interaction(interaction, ALREADY_HANDLED_REPLY)
            return

        result: str | None = None
        handler = self._button_handlers.get(action)
        if handler is not None:
            try:
                result = await handler(taken, interaction)
            except Exception:  # noqa: BLE001 - never let the gateway die
                _LOGGER.exception("Button action %s failed", action)
                result = BUTTON_ERROR_REPLY
        if result is None:
            payload_text = taken.payload.get("response_text")
            result = payload_text if isinstance(payload_text, str) else HANDLED_REPLY
        await self._edit_interaction_message(interaction, result)
        await self._respond_interaction(interaction, result)

    async def _ack_interaction(self, interaction: discord.Interaction) -> None:
        try:
            response = getattr(interaction, "response", None)
            if response is None or response.is_done():
                return
            await response.defer(ephemeral=True)
        except Exception:  # noqa: BLE001 - ack is best-effort
            _LOGGER.debug("Failed to acknowledge interaction", exc_info=True)

    async def _respond_interaction(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """Send an ephemeral reply, via followup when already acknowledged."""
        try:
            response = getattr(interaction, "response", None)
            if response is not None and not response.is_done():
                await response.send_message(
                    text,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            followup = getattr(interaction, "followup", None)
            if followup is not None:
                await followup.send(
                    text,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:  # noqa: BLE001 - a failed reply must not kill the bot
            _LOGGER.warning("Failed to reply to interaction", exc_info=True)

    async def _edit_interaction_message(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """Edit the button's message to show the outcome and drop its buttons."""
        try:
            message = getattr(interaction, "message", None)
            if message is not None and hasattr(message, "edit"):
                await message.edit(
                    content=text,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:  # noqa: BLE001 - editing is best-effort
            _LOGGER.debug("Failed to edit interaction message", exc_info=True)

    async def _send_reply(self, message: discord.Message, reply: str) -> None:
        """First piece replies to the message (no ping); the rest follow it.

        If the original message was deleted meanwhile, Discord posts the
        reply as a plain message instead of failing.
        """
        reference = message.to_reference(fail_if_not_exists=False)
        for part in chunk_message(reply):
            try:
                if reference is not None:
                    await message.channel.send(
                        part, reference=reference, mention_author=False
                    )
                    reference = None
                else:
                    await message.channel.send(part)
            except discord.HTTPException:
                _LOGGER.warning(
                    "Failed to send reply to channel %s", message.channel.id
                )
                break

    async def _answer(
        self, message: discord.Message, text: str, key: str, where: MessageContext
    ) -> str:
        """Process the message, showing a typing indicator best-effort.

        A typing-indicator failure must not block the actual reply, so fall
        back to answering without it.
        """

        async def run() -> str:
            return await self.router.process(
                text=text,
                discord_user_id=message.author.id,
                conversation_key=key,
                where=where,
            )

        started = False
        reply: str | None = None
        try:
            async with message.channel.typing():
                started = True
                reply = await run()
        except discord.HTTPException:
            # Retry only when the typing call failed before the agent ran;
            # never run the agent (and its actions) twice.
            if reply is not None:
                return reply
            if started:
                raise
            return await run()
        return reply
