"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

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
from .notes import MAX_NOTES_PER_USER, NoteLimitError, parse_notes_command
from .text_utils import chunk_message, strip_self_mention

_LOGGER = logging.getLogger(__name__)

ERROR_REPLY = "Sorry, I hit an error talking to Home Assistant."
EMPTY_REPLY = "(no response)"
TIMEOUT_REPLY = "Sorry, Home Assistant took too long to answer. Try again?"


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

        # Notes text commands are handled here and never reach the agent.
        # Reaching this point means should_respond already authorized the
        # author; _notes_reply double-checks before mutating anything.
        command = parse_notes_command(text)
        if command is not None:
            reply = ERROR_REPLY
            try:
                reply = await self._notes_reply(message.author.id, command)
            except Exception:  # noqa: BLE001 - never let the gateway die
                _LOGGER.exception("Notes command failed")
            await self._send_reply(message, reply or EMPTY_REPLY)
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

    async def _notes_reply(self, author_id: int, command: tuple[str, str]) -> str:
        """Answer a notes text command for one authorized Discord user."""
        # Fail closed, in depth: should_respond already gated the message,
        # but a note must never be stored for someone with no HA identity.
        if self.router.ha_user_for(author_id) is None:
            return "You are not authorized to use standing preferences."
        store = getattr(self.router, "notes_store", None)
        if store is None:
            return "Standing preferences are not available right now."
        action, argument = command
        if action == "add":
            try:
                added = await store.async_add_note(author_id, argument)
            except NoteLimitError:
                return (
                    f"You already have {MAX_NOTES_PER_USER} standing "
                    "preferences. Say 'notes' to see them, then "
                    "'forget note <number>' to remove one first."
                )
            except ValueError:
                return "That note was empty after cleaning, so I did not save it."
            return f"Remembered: {added}"
        if action == "list":
            notes = await store.async_notes_for(author_id)
            if not notes:
                return (
                    "You have no standing preferences saved. "
                    "Say 'remember: <note>' to add one."
                )
            lines = ["Your standing preferences:"]
            lines.extend(
                f"{index}. {note}" for index, note in enumerate(notes, start=1)
            )
            return "\n".join(lines)
        # action == "forget"
        try:
            index = int(argument)
        except ValueError:
            return "Usage: forget note <number> (say 'notes' to see the numbers)."
        removed = await store.async_remove_note(author_id, index)
        if removed is None:
            return f"You do not have a note {index}. Say 'notes' to see them."
        return f"Forgot note {index}: {removed}"

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
