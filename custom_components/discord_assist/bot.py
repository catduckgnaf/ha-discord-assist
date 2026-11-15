"""discord.py gateway client that bridges Discord messages to the conversation agent."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import discord
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_PLACEHOLDER_DELAY,
    DOMAIN,
    EVENT_MESSAGE,
    ISSUE_DUPLICATE_TOKEN,
    issue_id,
)
from .conversation_router import (
    AgentTimeout,
    ConversationRouter,
    MessageContext,
    RateLimited,
)
from .recent_errors import RecentErrors
from .text_utils import chunk_message, strip_self_mention

_LOGGER = logging.getLogger(__name__)

ERROR_REPLY = "Sorry, I hit an error talking to Home Assistant."
EMPTY_REPLY = "(no response)"
TIMEOUT_REPLY = "Sorry, Home Assistant took too long to answer. Try again?"
RATE_LIMIT_REPLY = (
    "Slow down: you are sending requests too quickly. Please wait a bit and try again."
)
PLACEHOLDER_REPLY = "Working on it..."
UNAUTHORIZED_REPLY = "Sorry, you are not authorized to use this bot."

# Option names a text-bearing application command may use for its prompt.
_TEXT_OPTION_NAMES = ("text", "prompt", "query", "input", "message")


@asynccontextmanager
async def acknowledged(
    interaction: discord.Interaction,
    *,
    thinking: bool = False,
    ephemeral: bool = False,
) -> AsyncIterator[discord.Interaction]:
    """Acknowledge an interaction first, then let the handler work.

    Discord invalidates an interaction token if no initial response is
    sent within 3 seconds, while an Assist call routinely takes longer.
    Entering this context defers the interaction immediately, before any
    Home Assistant or agent work happens, so the token stays valid for
    its 15 minute lifetime and the handler can take as long as the agent
    timeout allows.

    Usage for future handlers (button dispatcher, slash commands)::

        async with acknowledged(interaction, thinking=True) as interaction:
            reply = await run_assist(...)
            await interaction.edit_original_response(content=reply)

    ``thinking=True`` asks Discord to show a thinking indicator and is
    the right choice for application commands; plain ``thinking=False``
    is a deferred message update for components, where the original
    message (including its buttons) stays in place until it is edited.
    ``ephemeral`` only takes effect for application commands, or when
    ``thinking`` is true.

    If the interaction was already responded to, ``defer()`` is skipped:
    ``InteractionResponse.is_done()`` guards against a second response
    raising ``InteractionResponded``.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=thinking, ephemeral=ephemeral)
    yield interaction


def _option_value(options: Any) -> str | None:
    """First text prompt in application-command options, if any.

    Looks for a string option named like a prompt first, then falls back
    to the first string-valued option. Subcommand options nest one level,
    so recurse into option lists carried on an option itself.
    """
    if not isinstance(options, (list, tuple)):
        return None
    fallback: str | None = None
    for option in options:
        if not isinstance(option, Mapping):
            continue
        nested = _option_value(option.get("options"))
        if nested is not None:
            return nested
        value = option.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        if option.get("name") in _TEXT_OPTION_NAMES:
            return value.strip()
        if fallback is None:
            fallback = value.strip()
    return fallback


def interaction_text(interaction: discord.Interaction) -> str | None:
    """Text prompt carried by an interaction, or None if it has none.

    Application commands carry their arguments in ``interaction.data``;
    component and modal interactions carry no free text here, so they
    return None and are left for their own dispatchers.
    """
    data = getattr(interaction, "data", None)
    if not isinstance(data, Mapping):
        return None
    return _option_value(data.get("options"))


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
        placeholder_delay: float = DEFAULT_PLACEHOLDER_DELAY,
        recent_errors: RecentErrors | None = None,
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
        self.recent_errors = (
            recent_errors if recent_errors is not None else RecentErrors()
        )
        self.connected = False
        self._status_signal = status_signal
        self.placeholder_delay = float(placeholder_delay)
        self._entry_title = entry_title
        self._sent_message_ids: set[int] = set()
        self._sent_message_order: deque[int] = deque()
        self._sent_nonces: set[str] = set()
        self._sent_nonce_order: deque[str] = deque()
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

    def _remember_nonce(self, nonce: str) -> None:
        self._sent_nonces.add(nonce)
        self._sent_nonce_order.append(nonce)
        while len(self._sent_nonce_order) > MAX_TRACKED_SENT_MESSAGES:
            self._sent_nonces.discard(self._sent_nonce_order.popleft())

    async def _send_to_channel(self, channel: Any, content: str, **kwargs: Any) -> Any:
        """Send through the one central path and record the sent message.

        The gateway echo of our own message can reach ``on_message``
        before ``send()`` returns its id, so every send carries a nonce
        that is remembered first; Discord echoes it on MESSAGE_CREATE.
        """
        nonce = secrets.token_hex(8)
        self._remember_nonce(nonce)
        sent = await channel.send(content, nonce=nonce, **kwargs)
        self.record_sent_message(sent)
        return sent

    def _is_ours(self, message: discord.Message) -> bool:
        """Whether a message from our bot user came from this process.

        Interaction responses and webhook posts are authored by the bot
        user but never go through ``_send_to_channel``; they are ours too.
        """
        if getattr(message, "id", None) in self._sent_message_ids:
            return True
        nonce = getattr(message, "nonce", None)
        if nonce is not None and str(nonce) in self._sent_nonces:
            return True
        return (
            getattr(message, "interaction_metadata", None) is not None
            or getattr(message, "webhook_id", None) is not None
        )

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
        _LOGGER.warning(
            "A message from this bot arrived that this process did not "
            "send; the bot token is probably used somewhere else (the "
            "Discord Conversation integration, a test bot, or Home "
            "Assistant's Discord notify integration). A second gateway "
            "client receives every message and actions may run twice"
        )
        entry_id = getattr(self.router, "config_entry_id", None)
        if entry_id is None or self.hass is None:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(ISSUE_DUPLICATE_TOKEN, entry_id),
            is_fixable=False,
            # A REST-only sender (the notify integration) also trips this,
            # so the message signal is a warning; a loaded legacy entry
            # with the same token is raised as an error at setup.
            severity=ir.IssueSeverity.WARNING,
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

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Acknowledge an application command first, then handle it.

        Only application commands are handled here. Autocomplete cannot be
        deferred and must never reach the agent (it fires on every
        keystroke), and component and modal interactions belong to their
        own dispatchers (discord.py views), which respond themselves; a
        second defer from here would race them. Timeout and error outcomes
        edit the deferred response instead of leaving Discord showing a
        perpetual thinking indicator.
        """
        if (
            getattr(interaction, "type", None)
            != discord.InteractionType.application_command
        ):
            return
        async with acknowledged(interaction, thinking=True):
            try:
                await self.handle_interaction(interaction)
            except RateLimited as err:
                # The interaction is already deferred, so it needs an answer;
                # repeat refusals in the window get a bare one.
                await self._edit_interaction_reply(
                    interaction, RATE_LIMIT_REPLY if err.should_notify else "..."
                )
            except AgentTimeout:
                await self._edit_interaction_reply(interaction, TIMEOUT_REPLY)
            except Exception:  # noqa: BLE001 - never let the gateway die
                _LOGGER.exception("Interaction handling failed")
                await self._edit_interaction_reply(interaction, ERROR_REPLY)

    async def handle_interaction(self, interaction: discord.Interaction) -> None:
        """Handle an already-acknowledged interaction.

        Minimal real handler: a text-bearing application command is
        answered through the conversation router, exactly like a message,
        and the answer edits the original (deferred) response. Any other
        interaction is only acknowledged for now; the button dispatcher
        and the slash command tree replace or extend this dispatch when
        they land, reusing :func:`acknowledged` the same way.
        """
        text = interaction_text(interaction)
        if text is None:
            _LOGGER.debug(
                "Acknowledged interaction %s with no text prompt; "
                "leaving it to a future dispatcher",
                getattr(interaction, "id", None),
            )
            return

        user = getattr(interaction, "user", None)
        channel = getattr(interaction, "channel", None)
        if user is None or channel is None:
            return
        channel_id = getattr(channel, "id", getattr(interaction, "channel_id", None))
        if channel_id is None:
            return

        is_dm = isinstance(channel, discord.DMChannel) or (
            getattr(interaction, "guild_id", None) is None
        )
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else None
        # An explicit invocation is at least as intentional as a mention.
        if not self.router.should_respond(
            is_dm=is_dm,
            was_mentioned=True,
            channel_id=channel_id,
            author_id=user.id,
            parent_id=parent_id,
        ):
            await self._edit_interaction_reply(interaction, UNAUTHORIZED_REPLY)
            return

        key = self.router.conversation_key(
            is_dm=is_dm, channel_id=channel_id, author_id=user.id, parent_id=parent_id
        )
        where = MessageContext(
            display_name=getattr(user, "display_name", None),
            channel_name=None if is_dm else getattr(channel, "name", None),
            channel_id=channel_id,
            parent_id=parent_id,
        )
        reply = await self.router.process(
            text=text,
            discord_user_id=user.id,
            conversation_key=key,
            where=where,
        )
        await self._edit_interaction_reply(interaction, reply or EMPTY_REPLY)

    async def _edit_interaction_reply(
        self, interaction: discord.Interaction, reply: str
    ) -> None:
        """Edit the deferred response with the answer, following up if long.

        The first chunk edits the original response; any remaining chunks
        go through the followup webhook. Agent output is untrusted, so
        every send pins ``AllowedMentions.none()``.
        """
        no_mentions = discord.AllowedMentions.none()
        for index, part in enumerate(chunk_message(reply)):
            try:
                if index == 0:
                    await interaction.edit_original_response(
                        content=part, allowed_mentions=no_mentions
                    )
                else:
                    await interaction.followup.send(part, allowed_mentions=no_mentions)
            except discord.HTTPException as err:
                self.recent_errors.record(err, source="interaction_reply")
                _LOGGER.warning("Failed to edit interaction response")
                break

    async def on_message(self, message: discord.Message) -> None:
        if self._is_own_user(message):
            # Our own messages are never answered. One we did not send
            # means another process is running with the same bot token.
            if self._is_ours(message):
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

        # The key carries the resolved agent: a conversation_id belongs to
        # the agent that created it, so a different agent must not reuse it.
        key = self.router.conversation_key(
            is_dm=is_dm,
            channel_id=message.channel.id,
            author_id=message.author.id,
            parent_id=parent_id,
        )
        where = MessageContext(
            display_name=getattr(message.author, "display_name", None),
            channel_name=None if is_dm else getattr(message.channel, "name", None),
            channel_id=message.channel.id,
            parent_id=parent_id,
        )
        # Throttle before typing, the placeholder or any agent work, so a
        # refused request costs nothing: one "Slow down" notice per
        # throttle window, then silence.
        decision = self.router.check_rate_limit(message.author.id)
        if not decision.allowed:
            if decision.should_notify:
                await self._send_reply(message, RATE_LIMIT_REPLY)
            return
        self._fire_message_event(message, text=text, is_dm=is_dm)
        reply, placeholder = await self._answer(message, text, key, where)
        await self._deliver_reply(message, reply or EMPTY_REPLY, placeholder)

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
        # Discord ids are 64-bit snowflakes, beyond what the frontend can
        # hold as a JS number, so they go out as strings (as Discord's own
        # API sends them) and automations match them reliably.
        message_id = getattr(message, "id", None)
        data: dict[str, object] = {
            "channel_id": str(message.channel.id),
            "channel_name": None if is_dm else getattr(message.channel, "name", None),
            "is_dm": is_dm,
            "discord_user_id": str(message.author.id),
            "ha_user_id": ha_user_id,
            "message_id": None if message_id is None else str(message_id),
            "config_entry_id": self.router.config_entry_id,
        }
        # Event data is stored by the recorder; the privacy option omits
        # the message text entirely instead of blanking it.
        if not self.router.event_exclude_text:
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
                    await self._send_to_channel(
                        message.channel,
                        part,
                        reference=reference,
                        mention_author=False,
                    )
                    reference = None
                else:
                    await self._send_to_channel(message.channel, part)
            except discord.HTTPException as err:
                self.recent_errors.record(err, source="send_reply")
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
                return await self._send_to_channel(
                    message.channel,
                    PLACEHOLDER_REPLY,
                    reference=reference,
                    mention_author=False,
                )
            return await self._send_to_channel(message.channel, PLACEHOLDER_REPLY)
        except discord.HTTPException as err:
            self.recent_errors.record(err, source="placeholder")
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
        except discord.HTTPException as err:
            self.recent_errors.record(err, source="placeholder")
            _LOGGER.warning(
                "Failed to edit placeholder in channel %s", message.channel.id
            )
            await self._send_reply(message, reply)
            return
        for part in chunks[1:]:
            try:
                await self._send_to_channel(message.channel, part)
            except discord.HTTPException as err:
                self.recent_errors.record(err, source="send_reply")
                _LOGGER.warning(
                    "Failed to send reply to channel %s", message.channel.id
                )
                break

    async def _answer(
        self, message: discord.Message, text: str, key: str, where: MessageContext
    ) -> tuple[str, discord.Message | None]:
        """Process the message with a capped typing indicator and a placeholder.

        Typing runs in a background task bounded by TYPING_CAP_SECONDS and
        is cancelled as soon as the answer is ready; it can never raise
        into the answer path or re-run the agent. The agent runs as its own
        task: if it has not answered after ``placeholder_delay`` seconds, a
        placeholder reply is posted and returned alongside the final text
        so the caller can edit it in place. Fast answers never see one.
        Timeout and error outcomes are mapped to their reply text here so
        those paths can edit the placeholder too.
        """
        typing_task = asyncio.create_task(_bounded_typing(message.channel))
        task = asyncio.create_task(
            self.router.process(
                text=text,
                discord_user_id=message.author.id,
                conversation_key=key,
                where=where,
                rate_checked=True,
            )
        )
        placeholder: discord.Message | None = None
        try:
            done, _ = await asyncio.wait({task}, timeout=self.placeholder_delay)
            if not done:
                placeholder = await self._post_placeholder(message)
            reply = await task
        except AgentTimeout as err:
            self.recent_errors.record(err, source="on_message")
            return TIMEOUT_REPLY, placeholder
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as err:  # noqa: BLE001 - never let the gateway die
            self.recent_errors.record(err, source="on_message")
            _LOGGER.exception("Conversation processing failed")
            return ERROR_REPLY, placeholder
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
        return reply, placeholder
