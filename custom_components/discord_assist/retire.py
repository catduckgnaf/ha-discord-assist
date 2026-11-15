"""Retire stale button messages: strip buttons, strike the headline.

A retired message is edited with ``view=None`` (which removes its buttons),
its stored headline is struck through, and an outcome line is appended, for
example "Unlocked by Andrew" for a handled alert or the expiry line for one
the prune pass retired.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol

import discord

from .button_store import ButtonRecord, ButtonStore

_LOGGER = logging.getLogger(__name__)

# How often the periodic prune runs, and the most message edits one run may
# make. Ten edits per fifteen minutes stays far below Discord's global limit
# of 50 requests per second while still clearing stale buttons promptly.
PRUNE_INTERVAL = timedelta(minutes=15)
PRUNE_EDIT_CAP = 10

EXPIRED_OUTCOME = "Expired."


class _Message(Protocol):
    content: str | None

    async def edit(self, **kwargs) -> object: ...


class _Channel(Protocol):
    async def fetch_message(self, message_id: int) -> _Message: ...


class _Client(Protocol):
    def get_channel(self, channel_id: int) -> _Channel | None: ...

    async def fetch_channel(self, channel_id: int) -> _Channel: ...


def retired_content(
    current: str | None, headline: str | None, outcome: str | None
) -> str:
    """Build the replacement content for a retired message.

    The headline is struck through where it appears (or prepended, struck
    through, when the live content no longer contains it), and the outcome
    line is appended when one is supplied.
    """
    content = current or ""
    if headline:
        struck = f"~~{headline}~~"
        if headline in content and struck not in content:
            content = content.replace(headline, struck, 1)
        elif struck not in content:
            content = f"{struck}\n{content}" if content else struck
    if outcome and outcome not in content:
        content = f"{content}\n{outcome}" if content else outcome
    return content


async def _fetch_message(client: _Client, record: ButtonRecord) -> _Message | None:
    """Fetch the record's message, or None when it cannot be reached."""
    try:
        channel = client.get_channel(record.channel_id)
        if channel is None:
            channel = await client.fetch_channel(record.channel_id)
        return await channel.fetch_message(record.message_id)
    except discord.HTTPException:
        _LOGGER.warning(
            "Could not fetch Discord message %s in channel %s to retire it",
            record.message_id,
            record.channel_id,
        )
        return None


async def async_edit_retired(
    client: _Client, record: ButtonRecord, outcome: str | None
) -> bool:
    """Edit one record's message to remove its buttons. False if unreachable."""
    message = await _fetch_message(client, record)
    if message is None:
        return False
    try:
        await message.edit(
            content=retired_content(message.content, record.headline, outcome),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        _LOGGER.warning(
            "Could not edit Discord message %s to retire it", record.message_id
        )
        return False
    return True


async def async_retire_taken(
    store: ButtonStore, client: _Client, message_id: int, outcome: str | None
) -> bool:
    """Retire a message whose record was just handled (single-use take).

    Returns False, editing nothing, when no record is held for the message:
    an unknown or already-handled message fails cleanly.
    """
    record = await store.async_take(message_id)
    if record is None:
        return False
    return await async_edit_retired(client, record, outcome)


async def async_prune_expired(
    store: ButtonStore,
    client: _Client,
    *,
    cap: int = PRUNE_EDIT_CAP,
) -> int:
    """Retire expired button messages, at most ``cap`` edits per run.

    Every expired record considered is pruned from the store, even when its
    message can no longer be edited (for example it was deleted), so a dead
    message is not retried forever. Returns the number of messages edited.
    """
    edited = 0
    for record in store.expired()[:cap]:
        if await async_edit_retired(client, record, EXPIRED_OUTCOME):
            edited += 1
        await store.async_remove(record.message_id)
    return edited
