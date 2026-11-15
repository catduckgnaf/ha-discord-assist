"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import asyncio
import logging

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

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

# Discord typing indicators expire after about 10 seconds, so there is no
# point refreshing one for longer than that, however long the agent runs.
TYPING_CAP_SECONDS = 10.0
# discord.py's own typing loop re-sends every 5 seconds; mirror that.
TYPING_REFRESH_SECONDS = 5.0


async def _bounded_typing(channel: discord.abc.Messageable) -> None:
    """Trigger typing on ``channel``, refreshing it until the cap.

    Fire-and-forget helper: every error is logged at debug and ignored, so
    a typing failure can never affect the reply. Cancellation (when the
    answer is ready) stops the loop early.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TYPING_CAP_SECONDS
    while True:
        try:
            # The awaitable form of typing() sends a single trigger.
            await channel.typing()
        except Exception:  # noqa: BLE001 - typing is strictly best-effort
            _LOGGER.debug("Typing indicator failed", exc_info=True)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(TYPING_REFRESH_SECONDS, remaining))
        if loop.time() >= deadline:
            return


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self, hass: HomeAssistant, router: ConversationRouter, status_signal: str
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
        """Process the message, showing a capped typing indicator.

        Typing runs in a background task bounded by TYPING_CAP_SECONDS and
        is cancelled as soon as the answer is ready; it can never raise
        into the answer path or re-run the agent.
        """
        typing_task = asyncio.create_task(_bounded_typing(message.channel))
        try:
            return await self.router.process(
                text=text,
                discord_user_id=message.author.id,
                conversation_key=key,
                where=where,
            )
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
