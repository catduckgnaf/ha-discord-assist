"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, ISSUE_DUPLICATE_TOKEN, issue_id
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

# How many of our own sent message ids to remember when telling our own
# messages apart from a foreign process using the same bot token. Bounded
# so a long-lived client does not grow without limit; chunked replies send
# only a handful of messages per turn, so this covers many turns.
MAX_TRACKED_SENT_MESSAGES = 1000


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        router: ConversationRouter,
        status_signal: str,
        *,
        entry_id: str | None = None,
        entry_title: str | None = None,
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
        self._entry_id = entry_id
        self._entry_title = entry_title
        self._sent_message_ids: set[int] = set()
        self._sent_message_order: deque[int] = deque()
        self._duplicate_token_warned = False

    def record_sent_message(self, sent: Any) -> None:
        """Remember a message this process sent, by id.

        Every send path in this client goes through here (see
        ``_send_to_channel``), so chunked replies are all recorded. An
        edit keeps the original message id, so an edited reply stays
        recorded, and any future interaction-response or webhook send
        must record its returned message here as well. That is what
        keeps our own messages from being mistaken for a second
        process running with the same bot token.
        """
        message_id = getattr(sent, "id", sent)
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        if message_id in self._sent_message_ids:
            return
        self._sent_message_ids.add(message_id)
        self._sent_message_order.append(message_id)
        while len(self._sent_message_order) > MAX_TRACKED_SENT_MESSAGES:
            self._sent_message_ids.discard(self._sent_message_order.popleft())

    async def _send_to_channel(self, channel: Any, content: str, **kwargs: Any) -> Any:
        """Send through the one central path and record the sent message."""
        sent = await channel.send(content, **kwargs)
        self.record_sent_message(sent)
        return sent

    def _is_own_user(self, message: discord.Message) -> bool:
        if self.user is None:
            return False
        if message.author == self.user:
            return True
        return getattr(message.author, "id", None) == self.user.id

    def _serves_channel(self, message: discord.Message) -> bool:
        """Whether this entry serves the channel a message arrived in.

        Mirrors the channel part of ``ConversationRouter.should_respond``
        without its author check (our own bot user is never an authorized
        Home Assistant user): DMs when DMs are answered, a configured
        channel, or a thread whose parent channel is configured.
        """
        if isinstance(message.channel, discord.DMChannel):
            respond_dms = getattr(self.router, "respond_dms", False)
            return respond_dms is True
        parent_id = (
            message.channel.parent_id
            if isinstance(message.channel, discord.Thread)
            else None
        )
        channels = getattr(self.router, "channels", None)
        try:
            if message.channel.id in channels:
                return True
            return parent_id is not None and parent_id in channels
        except TypeError:
            return False

    def _warn_duplicate_token(self) -> None:
        """Log once and raise the duplicate-token repair issue."""
        if self._duplicate_token_warned:
            return
        self._duplicate_token_warned = True
        _LOGGER.error(
            "A message from this bot arrived that this process did not "
            "send; the bot token is probably running somewhere else (for "
            "example the Discord Conversation integration or a test bot). "
            "Both processes receive every message and actions may run "
            "twice, so stop the other process."
        )
        if self._entry_id is None or self.hass is None:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(ISSUE_DUPLICATE_TOKEN, self._entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_DUPLICATE_TOKEN,
            translation_placeholders={"entry_title": self._entry_title or ""},
        )

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
        if self._is_own_user(message):
            # Our own messages are never answered. One we did not send
            # means another process is running with the same bot token.
            if getattr(message, "id", None) in self._sent_message_ids:
                return
            if self._serves_channel(message):
                self._warn_duplicate_token()
            return
        if message.author.bot:
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
                    await self._send_to_channel(
                        message.channel,
                        part,
                        reference=reference,
                        mention_author=False,
                    )
                    reference = None
                else:
                    await self._send_to_channel(message.channel, part)
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
