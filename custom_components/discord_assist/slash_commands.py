"""Slash commands for Discord Assist (issue #19).

A ``discord.app_commands.CommandTree`` owned by ``DiscordAssistClient`` with
``/assist``, ``/state``, ``/reset``, ``/remember``, ``/notes``, ``/forget``
and ``/whoami``.

Every command goes through the same gate as a message
(``DiscordAssistClient.interaction_allowed``: the router's
``should_respond`` rules plus an HA identity) before anything is
acknowledged. A refused user gets an ephemeral refusal as the initial
response, so nobody else sees it; an allowed command is deferred with the
shared ``acknowledged`` helper before any Home Assistant call, so Discord's
3 second window cannot expire while the agent works.

The commands reuse the message path's own pieces: ``/assist`` is the
client's ``answer_command``, ``/reset`` is the router's locked
``reset_conversation`` on the same conversation key, and the notes commands
are the persistent notes from #16 (``_notes_reply``), so text and slash
forms always agree.

Commands are synced per guild once per client, so they appear immediately
in the servers the bot is in; they are not registered globally, so they are
not offered in DMs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)

from .text_utils import chunk_message

if TYPE_CHECKING:
    from .bot import DiscordAssistClient

_LOGGER = logging.getLogger(__name__)

EMPTY_REPLY = "(no response)"

# Discord returns at most 25 autocomplete choices.
MAX_AUTOCOMPLETE_CHOICES = 25


def _matches_prefix(entity_id: str, needle: str) -> bool:
    """Typed-prefix match on the full id or on the object id after the dot."""
    lowered = entity_id.lower()
    if lowered.startswith(needle):
        return True
    object_id = lowered.partition(".")[2]
    return object_id.startswith(needle)


def _readable_exposed(hass, user, entity_id: str) -> bool:
    """Exposed to Assist and readable by the mapped user; fails closed."""
    try:
        if not async_should_expose(hass, "conversation", entity_id):
            return False
        return bool(user.permissions.check_entity(entity_id, POLICY_READ))
    except Exception:  # noqa: BLE001 - unverifiable means no
        return False


def exposed_entity_choices(hass, user, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete choices: exposed and readable entities, prefix filtered.

    Only entities exposed to the conversation assistant that ``user`` may
    read are offered, at most 25. Any error fails closed.
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
        if not _readable_exposed(hass, user, entity_id):
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


def build_command_tree(client: DiscordAssistClient) -> app_commands.CommandTree:
    """Create the client's CommandTree and register every slash command."""
    # Late import: bot imports this module.
    from .bot import acknowledged

    tree = app_commands.CommandTree(client)

    async def gate(interaction: discord.Interaction) -> bool:
        """Refuse ephemerally, before any acknowledgement, if not allowed."""
        if client.interaction_allowed(interaction):
            return True
        await client.refuse_interaction(interaction)
        return False

    @tree.command(name="assist", description="Ask Home Assistant Assist")
    @app_commands.describe(text="What to ask Home Assistant")
    async def assist(interaction: discord.Interaction, text: str) -> None:
        # The same path a plain command interaction takes: gate, defer,
        # router.process on the shared conversation key, edit the answer.
        await client.answer_command(interaction, text)

    @tree.command(name="state", description="Read one exposed entity's state")
    @app_commands.describe(entity="Entity to read")
    async def state(interaction: discord.Interaction, entity: str) -> None:
        if not await gate(interaction):
            return
        async with acknowledged(interaction, thinking=True, ephemeral=True):
            ha_user_id = client.router.ha_user_for(interaction.user.id)
            user = await _ha_user(client.hass, ha_user_id) if ha_user_id else None
            entity_id = entity.strip()
            # Read as the mapped user, and only what Assist may see: an
            # entity that is not exposed is refused like an unreadable one.
            if user is None or not _readable_exposed(client.hass, user, entity_id):
                await _edit_reply(
                    interaction, f"You do not have permission to read {entity_id}."
                )
                return
            await _edit_reply(interaction, format_entity_state(client.hass, entity_id))

    @state.autocomplete("entity")
    async def state_entity_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # Autocomplete cannot be refused with a message; an unauthorized
        # user simply gets no suggestions, never a list of entity ids.
        if not client.interaction_allowed(interaction):
            return []
        ha_user_id = client.router.ha_user_for(interaction.user.id)
        user = await _ha_user(client.hass, ha_user_id) if ha_user_id else None
        if user is None:
            return []
        return exposed_entity_choices(client.hass, user, current)

    @tree.command(name="reset", description="Start a fresh conversation here")
    async def reset(interaction: discord.Interaction) -> None:
        if not await gate(interaction):
            return
        async with acknowledged(interaction, thinking=True):
            key = client.interaction_conversation_key(interaction)
            if key is not None:
                await client.router.reset_conversation(key)
            await _edit_reply(
                interaction,
                "Conversation reset. The next message starts a new conversation.",
            )

    async def notes_command(
        interaction: discord.Interaction, command: tuple[str, str]
    ) -> None:
        if not await gate(interaction):
            return
        async with acknowledged(interaction, thinking=True, ephemeral=True):
            reply = await client._notes_reply(interaction.user.id, command)
            await _edit_reply(interaction, reply)

    @tree.command(name="remember", description="Save a standing preference note")
    @app_commands.describe(note="The note to remember")
    async def remember(interaction: discord.Interaction, note: str) -> None:
        await notes_command(interaction, ("add", note))

    @tree.command(name="notes", description="List your saved notes")
    async def notes(interaction: discord.Interaction) -> None:
        await notes_command(interaction, ("list", ""))

    @tree.command(name="forget", description="Forget one of your saved notes")
    @app_commands.describe(number="Note number, as shown by /notes")
    async def forget(interaction: discord.Interaction, number: int) -> None:
        await notes_command(interaction, ("forget", str(number)))

    @tree.command(
        name="whoami", description="Show which Home Assistant user you act as"
    )
    async def whoami(interaction: discord.Interaction) -> None:
        if not await gate(interaction):
            return
        async with acknowledged(interaction, thinking=True, ephemeral=True):
            ha_user_id = client.router.ha_user_for(interaction.user.id)
            user = await _ha_user(client.hass, ha_user_id) if ha_user_id else None
            name = getattr(user, "name", None) if user is not None else None
            if name:
                text = f"You are mapped to Home Assistant user {name} ({ha_user_id})."
            else:
                text = f"You are mapped to Home Assistant user {ha_user_id}."
            await _edit_reply(interaction, text)

    return tree
