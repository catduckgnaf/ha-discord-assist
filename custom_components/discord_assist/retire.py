"""Retire stale button messages (issue #27).

A retired message loses its buttons, its headline is struck through and an
outcome line is appended. Pressed buttons are retired by the dispatcher
itself (``bot.dispatch_button``); this module handles the rest: expired
records, handed over by the store, are retired by a periodic pass capped
per run, and automations can retire a message with the ``retire_message``
action.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord

from .buttons import ButtonRecord, ButtonStore, retired_content

_LOGGER = logging.getLogger(__name__)

# How often the periodic pass runs, and the most message edits one run may
# make. Ten edits per fifteen minutes stays far below Discord's global limit
# of 50 requests per second while still clearing stale buttons promptly.
PRUNE_INTERVAL = timedelta(minutes=15)
PRUNE_EDIT_CAP = 10

EXPIRED_OUTCOME = "Expired."


async def _fetch_message(client: Any, channel_id: int, message_id: int) -> Any | None:
    """Fetch a message, or None when it cannot be reached."""
    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        _LOGGER.warning(
            "Could not fetch Discord message %s in channel %s to retire it",
            message_id,
            channel_id,
        )
        return None


async def async_edit_retired(
    client: Any, record: ButtonRecord, outcome: str | None
) -> bool:
    """Strip one record's message of its buttons. False if unreachable."""
    if record.channel_id is None or record.message_id is None:
        return False
    message = await _fetch_message(client, record.channel_id, record.message_id)
    if message is None:
        return False
    try:
        await message.edit(
            content=retired_content(
                message.content, outcome, record.payload.get("headline")
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        _LOGGER.warning(
            "Could not edit Discord message %s to retire it", record.message_id
        )
        return False
    return True


async def async_prune_expired(
    store: ButtonStore, client: Any, *, cap: int = PRUNE_EDIT_CAP
) -> int:
    """Retire expired button messages, at most ``cap`` edits per run.

    Every message handed over is dropped from the store even when it can
    no longer be edited (for example it was deleted), so a dead message is
    not retried forever. Returns the number of messages edited.
    """
    edited = 0
    for record in await store.async_pop_expired(cap):
        try:
            if await async_edit_retired(client, record, EXPIRED_OUTCOME):
                edited += 1
        except Exception:  # noqa: BLE001 - one message must not stop the pass
            _LOGGER.exception("Failed to retire Discord message %s", record.message_id)
    return edited
