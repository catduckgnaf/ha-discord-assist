"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .buttons import ANSWER_ACTION, ButtonStore, needs_clarify_buttons, parse_custom_id
from .conversation_router import (
    AgentTimeout,
    ConversationRouter,
    DetailedReply,
    MessageContext,
    make_conversation_key,
)
from .text_utils import chunk_message, strip_self_mention

_LOGGER = logging.getLogger(__name__)

ERROR_REPLY = "Sorry, I hit an error talking to Home Assistant."
EMPTY_REPLY = "(no response)"
TIMEOUT_REPLY = "Sorry, Home Assistant took too long to answer. Try again?"
ONLY_SPEAKER_REPLY = "Only the person who was asked can answer with these buttons."
ALREADY_HANDLED_REPLY = "That question has already been answered or has expired."

_CLARIFY_ANSWERS = ("Yes", "No")


def _clarify_view(records) -> discord.ui.View:
    """A persistent (no-timeout) Yes / No view; the dispatcher does the work."""
    view = discord.ui.View(timeout=None)
    for record in records:
        view.add_item(
            discord.ui.Button(
                label=record.answer,
                style=(
                    discord.ButtonStyle.success
                    if record.answer == "Yes"
                    else discord.ButtonStyle.secondary
                ),
                custom_id=record.custom_id,
            )
        )
    return view


class DiscordAssistClient(discord.Client):
    """Listens for messages and routes them to the HA conversation agent."""

    def __init__(
        self,
        hass: HomeAssistant,
        router: ConversationRouter,
        status_signal: str,
        button_store: ButtonStore | None = None,
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
        # In-memory unless the entry setup passes a storage-backed store;
        # records outlive any View either way, and presses go through the
        # single on_interaction dispatcher below.
        self.buttons = button_store if button_store is not None else ButtonStore()

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
        # A newer turn retires any clarify buttons still pending for this
        # conversation: their records are dropped and the old message's
        # buttons are stripped best-effort.
        await self._retire_buttons(key)
        detailed = DetailedReply(speech=ERROR_REPLY)
        try:
            detailed = await self._answer(message, text, key, where)
        except AgentTimeout:
            detailed = DetailedReply(speech=TIMEOUT_REPLY)
        except Exception:  # noqa: BLE001 - never let the gateway die
            _LOGGER.exception("Conversation processing failed")
        view, record_ids = await self._clarify_buttons(
            detailed,
            conversation_key=key,
            speaker_id=message.author.id,
            channel_id=message.channel.id,
        )
        sent = await self._send_reply(
            message, detailed.speech or EMPTY_REPLY, view=view
        )
        if view is not None and sent is not None:
            await self._bind_buttons(record_ids, sent, message.channel.id)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Single dispatcher for every ``da:`` button press (no live Views)."""
        data = getattr(interaction, "data", None)
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        parsed = parse_custom_id(custom_id)
        if parsed is None:
            return
        action, record_id = parsed
        if action != ANSWER_ACTION:
            return

        record = await self.buttons.peek(record_id)
        if record is None:
            await self._refuse_interaction(interaction, ALREADY_HANDLED_REPLY)
            return
        presser = getattr(interaction, "user", None)
        if getattr(presser, "id", None) != record.speaker_id:
            # Refused presses do not consume the record: the original
            # speaker can still answer.
            await self._refuse_interaction(interaction, ONLY_SPEAKER_REPLY)
            return
        record = await self.buttons.take(record_id)
        if record is None:
            await self._refuse_interaction(interaction, ALREADY_HANDLED_REPLY)
            return

        # Acknowledge first: the agent call can take far longer than
        # Discord's 3 second interaction window.
        await self._ack_interaction(interaction)

        channel = getattr(interaction, "channel", None)
        where = MessageContext(
            display_name=getattr(presser, "display_name", None),
            channel_name=getattr(channel, "name", None),
            channel_id=record.channel_id or getattr(interaction, "channel_id", None),
        )
        detailed = DetailedReply(speech=ERROR_REPLY)
        try:
            detailed = await self._process_detailed(
                text=record.answer,
                discord_user_id=record.speaker_id,
                conversation_key=record.conversation_key,
                where=where,
            )
        except AgentTimeout:
            detailed = DetailedReply(speech=TIMEOUT_REPLY)
        except Exception:  # noqa: BLE001 - never let the gateway die
            _LOGGER.exception("Clarify button processing failed")

        await self._remove_interaction_buttons(interaction)
        view, record_ids = await self._clarify_buttons(
            detailed,
            conversation_key=record.conversation_key,
            speaker_id=record.speaker_id,
            channel_id=where.channel_id,
        )
        sent = await self._send_followup(
            interaction, detailed.speech or EMPTY_REPLY, view=view
        )
        if view is not None and sent is not None:
            await self._bind_buttons(record_ids, sent, where.channel_id)

    async def _process_detailed(
        self,
        *,
        text: str,
        discord_user_id: int,
        conversation_key: str,
        where: MessageContext,
    ) -> DetailedReply:
        """Run a turn keeping continue_conversation, via the router if it can.

        Router-like objects that only expose ``process()`` (older code, test
        doubles) fall back to speech-only with no follow-up signal.
        """
        detailed_fn = getattr(self.router, "process_detailed", None)
        if detailed_fn is not None and inspect.iscoroutinefunction(detailed_fn):
            return await detailed_fn(
                text=text,
                discord_user_id=discord_user_id,
                conversation_key=conversation_key,
                where=where,
            )
        speech = await self.router.process(
            text=text,
            discord_user_id=discord_user_id,
            conversation_key=conversation_key,
            where=where,
        )
        return DetailedReply(speech=speech)

    async def _clarify_buttons(
        self,
        detailed: DetailedReply,
        *,
        conversation_key: str,
        speaker_id: int,
        channel_id: int | None,
    ) -> tuple[discord.ui.View | None, list[str]]:
        """Build Yes / No buttons for a question reply, or (None, [])."""
        heuristic = bool(getattr(self.router, "clarify_buttons_heuristic", False))
        if not needs_clarify_buttons(
            detailed.speech, detailed.continue_conversation, heuristic=heuristic
        ):
            return None, []
        group_id = secrets.token_urlsafe(8)
        records = [
            await self.buttons.record(
                action=ANSWER_ACTION,
                conversation_key=conversation_key,
                answer=answer,
                speaker_id=speaker_id,
                channel_id=channel_id,
                group_id=group_id,
            )
            for answer in _CLARIFY_ANSWERS
        ]
        return _clarify_view(records), [r.record_id for r in records]

    async def _bind_buttons(
        self, record_ids: list[str], sent: object, channel_id: int | None
    ) -> None:
        message_id = getattr(sent, "id", None)
        if record_ids and isinstance(message_id, int) and channel_id is not None:
            await self.buttons.bind_message(
                record_ids, message_id=message_id, channel_id=channel_id
            )

    async def _retire_buttons(self, conversation_key: str) -> None:
        removed = await self.buttons.invalidate_for_conversation(conversation_key)
        seen: set[tuple[int, int]] = set()
        for record in removed:
            if record.channel_id is None or record.message_id is None:
                continue
            target = (record.channel_id, record.message_id)
            if target in seen:
                continue
            seen.add(target)
            await self._strip_buttons(record.channel_id, record.message_id)

    async def _strip_buttons(self, channel_id: int, message_id: int) -> None:
        """Best-effort removal of buttons from an older reply message."""
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                return
            fetched = channel.fetch_message(message_id)
            if asyncio.iscoroutine(fetched):
                fetched = await fetched
            edited = fetched.edit(view=None)
            if asyncio.iscoroutine(edited):
                await edited
        except Exception:  # noqa: BLE001 - the records are already retired
            _LOGGER.debug(
                "Could not strip buttons from message %s", message_id, exc_info=True
            )

    async def _ack_interaction(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            _LOGGER.warning("Failed to acknowledge interaction", exc_info=True)

    async def _refuse_interaction(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            _LOGGER.warning("Failed to refuse interaction", exc_info=True)

    async def _remove_interaction_buttons(
        self, interaction: discord.Interaction
    ) -> None:
        message = getattr(interaction, "message", None)
        if message is None:
            return
        try:
            edited = message.edit(view=None)
            if asyncio.iscoroutine(edited):
                await edited
        except Exception:  # noqa: BLE001 - the press itself already succeeded
            _LOGGER.warning("Failed to remove clarify buttons", exc_info=True)

    async def _send_followup(
        self,
        interaction: discord.Interaction,
        reply: str,
        *,
        view: discord.ui.View | None = None,
    ):
        """Post a button press's reply in the same channel; returns last sent."""
        channel = getattr(interaction, "channel", None)
        send = getattr(channel, "send", None) or interaction.followup.send
        parts = chunk_message(reply)
        sent = None
        for index, part in enumerate(parts):
            kwargs = (
                {"view": view} if view is not None and index == len(parts) - 1 else {}
            )
            try:
                sent = await send(part, **kwargs)
            except discord.HTTPException:
                _LOGGER.warning("Failed to send clarify reply")
                break
        return sent

    async def _send_reply(
        self,
        message: discord.Message,
        reply: str,
        *,
        view: discord.ui.View | None = None,
    ):
        """First piece replies to the message (no ping); the rest follow it.

        If the original message was deleted meanwhile, Discord posts the
        reply as a plain message instead of failing. A view, when given,
        rides on the last piece. Returns the last message sent, or None.
        """
        reference = message.to_reference(fail_if_not_exists=False)
        parts = chunk_message(reply)
        sent = None
        for index, part in enumerate(parts):
            kwargs = {}
            if view is not None and index == len(parts) - 1:
                kwargs["view"] = view
            try:
                if reference is not None:
                    sent = await message.channel.send(
                        part, reference=reference, mention_author=False, **kwargs
                    )
                    reference = None
                else:
                    sent = await message.channel.send(part, **kwargs)
            except discord.HTTPException:
                _LOGGER.warning(
                    "Failed to send reply to channel %s", message.channel.id
                )
                break
        return sent

    async def _answer(
        self, message: discord.Message, text: str, key: str, where: MessageContext
    ) -> DetailedReply:
        """Process the message, showing a typing indicator best-effort.

        A typing-indicator failure must not block the actual reply, so fall
        back to answering without it.
        """

        async def run() -> DetailedReply:
            return await self._process_detailed(
                text=text,
                discord_user_id=message.author.id,
                conversation_key=key,
                where=where,
            )

        started = False
        reply: DetailedReply | None = None
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
