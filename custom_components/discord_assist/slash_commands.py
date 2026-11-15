"""Slash commands for Discord Assist (issue #19).

A ``discord.app_commands.CommandTree`` owned by ``DiscordAssistClient`` with
``/assist``, ``/state``, ``/reset``, ``/remember``, ``/notes``, ``/forget``
and ``/whoami``.

Every handler defers first, before any Home Assistant call, so Discord's
3 second interaction window cannot expire while the agent works. Issue #14
will provide one shared ``acknowledged`` helper for buttons and slash
commands; until that lands, the small local ``acknowledged`` context manager
below is the seam: handlers here only depend on its interface, so swapping
in the shared helper later is a one-line change per handler.

Access control fails closed, exactly like message handling: a Discord user
with no Home Assistant identity (``ConversationRouter.ha_user_for`` returns
None) gets an ephemeral refusal and nothing runs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)

from .conversation_router import AgentTimeout, MessageContext, make_conversation_key
from .text_utils import chunk_message

_LOGGER = logging.getLogger(__name__)

REFUSAL = (
    "You are not authorized to use Discord Assist. Ask the Home Assistant "
    "admin to map your Discord user first."
)
ERROR_REPLY = "Sorry, I hit an error talking to Home Assistant."
EMPTY_REPLY = "(no response)"
TIMEOUT_REPLY = "Sorry, Home Assistant took too long to answer. Try again?"

# Discord returns at most 25 autocomplete choices.
MAX_AUTOCOMPLETE_CHOICES = 25

# Notes caps, mirroring the proposal in issue #16.
MAX_NOTES_PER_USER = 10
MAX_NOTE_LENGTH = 200


def _clean_note(value: str) -> str:
    """Notes are user-controlled text: one line, no control chars, capped."""
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value).split())
    return cleaned[:MAX_NOTE_LENGTH]


class NotesStore:
    """Minimal per-Discord-user notes, in memory.

    This is the smallest self-contained substrate for ``/remember``,
    ``/notes`` and ``/forget``. Issue #16 owns the full feature: persistent
    storage via ``homeassistant.helpers.storage.Store``, editing in the
    options flow, and injecting the speaker's notes into agent turns via
    ``extra_system_prompt``. When #16 lands it can adopt this interface and
    back it with its Store; nothing here persists across restarts yet.
    """

    def __init__(self) -> None:
        self._notes: dict[int, list[str]] = {}

    def add(self, discord_user_id: int, note: str) -> str:
        """Add a cleaned note and return it; raise ValueError if not allowed."""
        cleaned = _clean_note(note)
        if not cleaned:
            raise ValueError("That note is empty once cleaned up.")
        notes = self._notes.setdefault(discord_user_id, [])
        if len(notes) >= MAX_NOTES_PER_USER:
            raise ValueError(
                f"You already have {MAX_NOTES_PER_USER} notes. "
                "Forget one with /forget before adding another."
            )
        notes.append(cleaned)
        return cleaned

    def list(self, discord_user_id: int) -> list[str]:
        """The user's notes, oldest first."""
        return list(self._notes.get(discord_user_id, []))

    def forget(self, discord_user_id: int, number: int) -> str:
        """Remove the 1-based note ``number`` and return it."""
        notes = self._notes.get(discord_user_id, [])
        if not 1 <= number <= len(notes):
            raise ValueError(f"You have no note number {number}.")
        return notes.pop(number - 1)


@asynccontextmanager
async def acknowledged(
    interaction: discord.Interaction, *, ephemeral: bool = False
) -> AsyncIterator[None]:
    """Defer the interaction first, then run the handler body.

    Local stand-in for the shared helper proposed in issue #14: no handler
    can forget to acknowledge, and ``is_done()`` guards against a double
    response if something already replied.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    yield


def interaction_conversation_key(interaction: discord.Interaction) -> str:
    """The conversation key a slash command in this context shares.

    Matches ``bot.on_message``: one conversation per DM (by user) or per
    channel, so ``/assist`` and ``/reset`` act on the same conversation as
    plain messages in the channel.
    """
    channel = interaction.channel
    is_dm = interaction.guild is None or isinstance(channel, discord.DMChannel)
    channel_id = interaction.channel_id or (channel.id if channel else 0)
    return make_conversation_key(
        is_dm=is_dm, channel_id=channel_id, author_id=interaction.user.id
    )


def interaction_where(interaction: discord.Interaction) -> MessageContext:
    """Speaker/channel context for a slash command, mirroring on_message."""
    channel = interaction.channel
    is_dm = interaction.guild is None or isinstance(channel, discord.DMChannel)
    parent_id = channel.parent_id if isinstance(channel, discord.Thread) else None
    return MessageContext(
        display_name=getattr(interaction.user, "display_name", None),
        channel_name=None if is_dm else getattr(channel, "name", None),
        channel_id=interaction.channel_id or (channel.id if channel else None),
        parent_id=parent_id,
    )


def _matches_prefix(entity_id: str, needle: str) -> bool:
    """Typed-prefix match on the full id or on the object id after the dot."""
    lowered = entity_id.lower()
    if lowered.startswith(needle):
        return True
    object_id = lowered.partition(".")[2]
    return object_id.startswith(needle)


def exposed_entity_choices(hass, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete choices: exposed entities only, prefix filtered, max 25.

    An entity is offered only when
    ``async_should_expose(hass, "conversation", entity_id)`` is true. Any
    error while checking exposure fails closed (the entity is not offered).
    """
    try:
        entity_ids = list(hass.states.async_entity_ids())
    except Exception:  # noqa: BLE001 - no state machine, no suggestions
        return []
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[str]] = []
    for entity_id in sorted(entity_ids):
        if needle and not _matches_prefix(entity_id, needle):
            continue
        try:
            if not async_should_expose(hass, "conversation", entity_id):
                continue
        except Exception:  # noqa: BLE001 - fail closed on exposure checks
            continue
        choices.append(app_commands.Choice(name=entity_id, value=entity_id))
        if len(choices) >= MAX_AUTOCOMPLETE_CHOICES:
            break
    return choices


def format_entity_state(hass, entity_id: str) -> str:
    """One-line human summary of an entity's current state."""
    state = hass.states.get(entity_id)
    if state is None:
        return f"{entity_id} was not found. It may be unavailable or removed."
    name = state.attributes.get("friendly_name") or entity_id
    text = f"{name} ({entity_id}) is {state.state}."
    unit = state.attributes.get("unit_of_measurement")
    if unit and state.state not in ("unknown", "unavailable"):
        text = f"{name} ({entity_id}) is {state.state} {unit}."
    return text


async def _ha_user(hass, ha_user_id: str):
    """The HA user object for an id, or None if it cannot be loaded."""
    auth = getattr(hass, "auth", None)
    if auth is None:
        return None
    try:
        return await auth.async_get_user(ha_user_id)
    except Exception:  # noqa: BLE001 - lookup failure is treated as no user
        return None


async def _edit_reply(interaction: discord.Interaction, text: str) -> None:
    """Fill in the deferred response, chunking long text into follow-ups."""
    parts = chunk_message(text or EMPTY_REPLY)
    await interaction.edit_original_response(
        content=parts[0], allowed_mentions=discord.AllowedMentions.none()
    )
    for part in parts[1:]:
        await interaction.followup.send(
            part, allowed_mentions=discord.AllowedMentions.none()
        )


async def _refuse(interaction: discord.Interaction) -> None:
    """Ephemeral refusal for a user with no HA identity; nothing has run."""
    await interaction.followup.send(REFUSAL, ephemeral=True)


def build_command_tree(client) -> app_commands.CommandTree:
    """Create the client's CommandTree and register every slash command.

    ``client`` is the DiscordAssistClient; handlers reach Home Assistant
    through ``client.hass`` and ``client.router`` and the notes through
    ``client.notes``.
    """
    tree = app_commands.CommandTree(client)

    @tree.command(name="assist", description="Ask Home Assistant Assist")
    @app_commands.describe(text="What to ask Home Assistant")
    async def assist(interaction: discord.Interaction, text: str) -> None:
        async with acknowledged(interaction, ephemeral=False):
            if client.router.ha_user_for(interaction.user.id) is None:
                await _refuse(interaction)
                return
            reply = ERROR_REPLY
            try:
                # Shares the channel conversation key (and, inside
                # router.process, its turn lock) with plain messages.
                reply = await client.router.process(
                    text=text,
                    discord_user_id=interaction.user.id,
                    conversation_key=interaction_conversation_key(interaction),
                    where=interaction_where(interaction),
                )
            except AgentTimeout:
                reply = TIMEOUT_REPLY
            except PermissionError:
                await _refuse(interaction)
                return
            except Exception:  # noqa: BLE001 - never let the gateway die
                _LOGGER.exception("Slash /assist processing failed")
            await _edit_reply(interaction, reply or EMPTY_REPLY)

    @tree.command(name="state", description="Read one exposed entity's state")
    @app_commands.describe(entity="Entity to read")
    async def state(interaction: discord.Interaction, entity: str) -> None:
        async with acknowledged(interaction, ephemeral=True):
            ha_user_id = client.router.ha_user_for(interaction.user.id)
            if ha_user_id is None:
                await _refuse(interaction)
                return
            entity_id = entity.strip()
            user = await _ha_user(client.hass, ha_user_id)
            if user is None:
                await interaction.followup.send(
                    "Your mapped Home Assistant user could not be loaded, "
                    "so nothing was read.",
                    ephemeral=True,
                )
                return
            # Read as the mapped user: their entity policy decides.
            try:
                allowed = user.permissions.check_entity(entity_id, POLICY_READ)
            except Exception:  # noqa: BLE001 - unverifiable means no read
                allowed = False
            if not allowed:
                await interaction.followup.send(
                    f"You do not have permission to read {entity_id}.",
                    ephemeral=True,
                )
                return
            await _edit_reply(interaction, format_entity_state(client.hass, entity_id))

    @state.autocomplete("entity")
    async def state_entity_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return exposed_entity_choices(client.hass, current)

    @tree.command(name="reset", description="Start a fresh conversation here")
    async def reset(interaction: discord.Interaction) -> None:
        async with acknowledged(interaction, ephemeral=False):
            if client.router.ha_user_for(interaction.user.id) is None:
                await _refuse(interaction)
                return
            # Against the cache directly: drop the cached conversation id
            # for this channel or DM, so the next turn starts fresh.
            key = interaction_conversation_key(interaction)
            client.router.cache.clear(key)
            await _edit_reply(
                interaction,
                "Conversation reset. The next message starts a new conversation.",
            )

    @tree.command(name="remember", description="Save a standing preference note")
    @app_commands.describe(note="The note to remember")
    async def remember(interaction: discord.Interaction, note: str) -> None:
        async with acknowledged(interaction, ephemeral=True):
            if client.router.ha_user_for(interaction.user.id) is None:
                await _refuse(interaction)
                return
            try:
                saved = client.notes.add(interaction.user.id, note)
            except ValueError as err:
                await interaction.followup.send(str(err), ephemeral=True)
                return
            await _edit_reply(interaction, f"Remembered: {saved}")

    @tree.command(name="notes", description="List your saved notes")
    async def notes(interaction: discord.Interaction) -> None:
        async with acknowledged(interaction, ephemeral=True):
            if client.router.ha_user_for(interaction.user.id) is None:
                await _refuse(interaction)
                return
            mine = client.notes.list(interaction.user.id)
            if not mine:
                await _edit_reply(interaction, "You have no saved notes.")
                return
            listing = "\n".join(
                f"{index}. {note}" for index, note in enumerate(mine, start=1)
            )
            await _edit_reply(interaction, f"Your notes:\n{listing}")

    @tree.command(name="forget", description="Forget one of your saved notes")
    @app_commands.describe(number="Note number, as shown by /notes")
    async def forget(interaction: discord.Interaction, number: int) -> None:
        async with acknowledged(interaction, ephemeral=True):
            if client.router.ha_user_for(interaction.user.id) is None:
                await _refuse(interaction)
                return
            try:
                removed = client.notes.forget(interaction.user.id, number)
            except ValueError as err:
                await interaction.followup.send(str(err), ephemeral=True)
                return
            await _edit_reply(interaction, f"Forgot: {removed}")

    @tree.command(
        name="whoami", description="Show which Home Assistant user you act as"
    )
    async def whoami(interaction: discord.Interaction) -> None:
        async with acknowledged(interaction, ephemeral=True):
            ha_user_id = client.router.ha_user_for(interaction.user.id)
            if ha_user_id is None:
                await _refuse(interaction)
                return
            user = await _ha_user(client.hass, ha_user_id)
            name = getattr(user, "name", None) if user is not None else None
            if name:
                text = f"You are mapped to Home Assistant user {name} ({ha_user_id})."
            else:
                text = f"You are mapped to Home Assistant user {ha_user_id}."
            await _edit_reply(interaction, text)

    return tree
