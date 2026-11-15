"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import asyncio
import logging

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_PLACEHOLDER_DELAY
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
PLACEHOLDER_REPLY = "Working on it..."


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        router: ConversationRouter,
        status_signal: str,
        placeholder_delay: float = DEFAULT_PLACEHOLDER_DELAY,
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
        self.placeholder_delay = float(placeholder_delay)

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
        reply, placeholder = await self._answer(message, text, key, where)
        await self._deliver_reply(message, reply or EMPTY_REPLY, placeholder)

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

    async def _post_placeholder(
        self, message: discord.Message
    ) -> discord.Message | None:
        """Post the slow-answer placeholder as a reply, best-effort."""
        reference = message.to_reference(fail_if_not_exists=False)
        try:
            if reference is not None:
                return await message.channel.send(
                    PLACEHOLDER_REPLY, reference=reference, mention_author=False
                )
            return await message.channel.send(PLACEHOLDER_REPLY)
        except discord.HTTPException:
            _LOGGER.warning(
                "Failed to send placeholder to channel %s", message.channel.id
            )
            return None

    async def _deliver_reply(
        self,
        message: discord.Message,
        reply: str,
        placeholder: discord.Message | None,
    ) -> None:
        """Deliver a reply, editing the placeholder in place when one exists.

        The first chunk replaces the placeholder text; any remaining chunks
        are sent as follow-up messages. If the edit fails, fall back to
        sending the whole reply as new messages.
        """
        if placeholder is None:
            await self._send_reply(message, reply)
            return
        chunks = chunk_message(reply)
        try:
            await placeholder.edit(
                content=chunks[0],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            _LOGGER.warning(
                "Failed to edit placeholder in channel %s", message.channel.id
            )
            await self._send_reply(message, reply)
            return
        for part in chunks[1:]:
            try:
                await message.channel.send(part)
            except discord.HTTPException:
                _LOGGER.warning(
                    "Failed to send reply to channel %s", message.channel.id
                )
                break

    async def _answer(
        self, message: discord.Message, text: str, key: str, where: MessageContext
    ) -> tuple[str, discord.Message | None]:
        """Process the message, showing typing and a slow-answer placeholder.

        The agent runs as a task. If it has not answered after
        ``placeholder_delay`` seconds, a placeholder reply is posted and
        returned alongside the final text so the caller can edit it in
        place. Fast answers never see a placeholder. Timeout and error
        outcomes are mapped to their reply text here so those paths can
        also edit the placeholder. A typing-indicator failure must not
        block the actual reply, and the agent must never run twice.
        """
        task = asyncio.create_task(
            self.router.process(
                text=text,
                discord_user_id=message.author.id,
                conversation_key=key,
                where=where,
            )
        )
        placeholder: discord.Message | None = None

        async def wait_for_answer() -> str:
            nonlocal placeholder
            if self.placeholder_delay is not None:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task), timeout=self.placeholder_delay
                    )
                except TimeoutError:
                    placeholder = await self._post_placeholder(message)
            return await task

        started = False
        reply: str | None = None
        try:
            async with message.channel.typing():
                started = True
                reply = await wait_for_answer()
        except AgentTimeout:
            return TIMEOUT_REPLY, placeholder
        except discord.HTTPException:
            if reply is not None:
                return reply, placeholder
            if not started:
                # Typing failed before the agent answer was awaited; wait
                # for the already-running task without the indicator.
                try:
                    reply = await wait_for_answer()
                except AgentTimeout:
                    return TIMEOUT_REPLY, placeholder
                except Exception:  # noqa: BLE001 - never let the gateway die
                    _LOGGER.exception("Conversation processing failed")
                    return ERROR_REPLY, placeholder
                return reply, placeholder
            # Typing failed on the way out with no answer in hand; report
            # the generic error (editing the placeholder if there is one).
            return ERROR_REPLY, placeholder
        except Exception:  # noqa: BLE001 - never let the gateway die
            _LOGGER.exception("Conversation processing failed")
            return ERROR_REPLY, placeholder
        return (reply if reply is not None else ERROR_REPLY), placeholder
