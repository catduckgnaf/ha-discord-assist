"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import logging

import discord
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import EVENT_MESSAGE
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


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        router: ConversationRouter,
        status_signal: str,
        *,
        config_entry_id: str | None = None,
        event_exclude_text: bool | None = None,
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
        # The router is built from the same entry in production, so fall back
        # to its values when the caller does not pass them explicitly.
        self.config_entry_id = config_entry_id or getattr(
            router, "config_entry_id", None
        )
        if event_exclude_text is None:
            event_exclude_text = getattr(router, "event_exclude_text", False)
        self.event_exclude_text = bool(event_exclude_text)

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

        self._fire_message_event(message, text=text, is_dm=is_dm)

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

    def _fire_message_event(
        self, message: discord.Message, *, text: str, is_dm: bool
    ) -> None:
        """Fire ``discord_assist_message`` for one accepted message.

        Only called after ``should_respond()`` passes, so rejected messages
        (including unmapped users, which fail closed) never produce an
        event. The event carries the sender's HA identity as its context;
        without one, no event is fired at all.
        """
        ha_user_id = self.router.ha_user_for(message.author.id)
        if ha_user_id is None:
            return
        data: dict[str, object] = {
            "channel_id": message.channel.id,
            "channel_name": None if is_dm else getattr(message.channel, "name", None),
            "is_dm": is_dm,
            "discord_user_id": message.author.id,
            "ha_user_id": ha_user_id,
            "message_id": getattr(message, "id", None),
            "config_entry_id": self.config_entry_id,
        }
        # Event data is stored by the recorder; the privacy option omits
        # the message text entirely instead of blanking it.
        if not self.event_exclude_text:
            data["text"] = text
        self.hass.bus.async_fire(
            EVENT_MESSAGE, data, context=Context(user_id=ha_user_id)
        )

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
