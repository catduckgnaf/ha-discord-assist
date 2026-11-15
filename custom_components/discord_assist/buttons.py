"""Persistent, restart-surviving Discord buttons.

Buttons carry a stateless ``custom_id`` of the form ``da:<action>:<record_id>``
and no ``discord.ui.View`` needs to stay alive for them to work. The record
(who may press, what to do, when it expires) is persisted in a Home Assistant
``Store``, so a button posted before a restart still works after it.

A press is single-use unless the record says ``multi_use``:
:meth:`ButtonStore.take` pops the record, and every sibling posted on the
same message (same ``group_id``), under one lock, so a second press of any
of them finds nothing and is refused as already handled.

Consumers (actionable events, clarify answers, approvals) register a handler
per action on the client and post buttons through
:func:`async_send_with_buttons`, which records, binds and sends in one place.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import discord
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PREFIX = "da"
STORAGE_VERSION = 1
# Print Handy uses the same horizon for its alert buttons.
DEFAULT_TTL_SECONDS = 24 * 3600
MAX_CUSTOM_ID_LENGTH = 100

# Actions and record ids go inside a custom_id, so they must never contain
# the separator (or whitespace, which Discord rejects in practice).
_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ButtonOutcome:
    """What the dispatcher does after a handler ran.

    ``outcome`` is a line appended to the button's message, ``ephemeral``
    is shown only to the presser, and ``retire`` strips the buttons from
    the message (ignored for multi-use records, which keep theirs).
    """

    outcome: str | None = None
    ephemeral: str | None = None
    retire: bool = True


# A button action handler: given the claimed record and the raw interaction,
# return a ButtonOutcome, a plain text (shown to the presser and appended to
# the message), or None for the generic acknowledgement.
ButtonHandler = Callable[["ButtonRecord", Any], Awaitable["ButtonOutcome | str | None"]]


def make_custom_id(action: str, record_id: str) -> str:
    """Build ``da:<action>:<record_id>``, enforcing Discord's 100 char limit."""
    if not _PART_RE.match(action or ""):
        raise ValueError(f"Invalid button action: {action!r}")
    if not _PART_RE.match(record_id or ""):
        raise ValueError(f"Invalid button record id: {record_id!r}")
    custom_id = f"{PREFIX}:{action}:{record_id}"
    assert len(custom_id) <= MAX_CUSTOM_ID_LENGTH, (
        f"custom_id is {len(custom_id)} characters; Discord allows at most "
        f"{MAX_CUSTOM_ID_LENGTH}"
    )
    if len(custom_id) > MAX_CUSTOM_ID_LENGTH:  # pragma: no cover - assert guards
        raise ValueError(f"custom_id exceeds {MAX_CUSTOM_ID_LENGTH} characters")
    return custom_id


def parse_custom_id(custom_id: str | None) -> tuple[str, str] | None:
    """Split a custom_id into ``(action, record_id)``, or None if not ours.

    Anything without our exact ``da:`` prefix (other bots, other features,
    malformed ids) returns None so the dispatcher can ignore it.
    """
    if not custom_id or not isinstance(custom_id, str):
        return None
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    action, record_id = parts[1], parts[2]
    if not _PART_RE.match(action) or not _PART_RE.match(record_id):
        return None
    return action, record_id


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class ButtonRecord:
    """One pending button: who may press it and what pressing it means."""

    record_id: str
    action: str
    created_at: float
    expires_at: float
    allowed_user_ids: tuple[int, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    # Buttons posted on one message share a group: pressing one claims them
    # all, so a Yes/No pair can never both run.
    group_id: str | None = None
    multi_use: bool = False
    channel_id: int | None = None
    message_id: int | None = None

    @property
    def custom_id(self) -> str:
        return make_custom_id(self.action, self.record_id)

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def allows(self, discord_user_id: int) -> bool:
        """Whether this record names ``discord_user_id`` as a presser.

        An empty allowlist names nobody in particular; the dispatcher still
        requires the presser to be an authorized bot user (fail closed at
        that layer). A non-empty list is an exact restriction.
        """
        return not self.allowed_user_ids or discord_user_id in self.allowed_user_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "action": self.action,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "allowed_user_ids": list(self.allowed_user_ids),
            "payload": dict(self.payload),
            "group_id": self.group_id,
            "multi_use": self.multi_use,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ButtonRecord | None:
        try:
            return cls(
                record_id=str(data["record_id"]),
                action=str(data["action"]),
                created_at=float(data["created_at"]),
                expires_at=float(data["expires_at"]),
                allowed_user_ids=tuple(
                    int(value) for value in data.get("allowed_user_ids") or ()
                ),
                payload=dict(data.get("payload") or {}),
                group_id=(
                    str(data["group_id"]) if data.get("group_id") is not None else None
                ),
                multi_use=bool(data.get("multi_use", False)),
                channel_id=_optional_int(data.get("channel_id")),
                message_id=_optional_int(data.get("message_id")),
            )
        except KeyError, TypeError, ValueError:
            _LOGGER.warning("Dropping malformed button record: %r", data)
            return None


class ButtonStore:
    """Button records persisted in a Home Assistant ``Store`` (per entry).

    ``store`` is injectable so tests can pass a simple fake with
    ``async_load`` / ``async_save`` instead of a real HA storage backend;
    that fake is also what proves a record survives a "restart" (a new
    ButtonStore over the same persisted data).
    """

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        entry_id: str = "",
        *,
        store: Any | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        if store is None:
            if hass is None:
                raise ValueError("ButtonStore needs a hass or an explicit store")
            store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_buttons_{entry_id}")
        self._store: Store | Any = store
        self._ttl = float(ttl_seconds)
        self._now = now
        self._records: dict[str, ButtonRecord] = {}
        # Expired records whose message still shows buttons, waiting for the
        # retire pass (retire.py) to strip them.
        self._expired: dict[str, ButtonRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load persisted records, pruning expired or malformed rows."""
        async with self._lock:
            await self._load_locked()

    async def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        data = await self._store.async_load()
        self._records = self._parse(data, "records")
        self._expired = self._parse(data, "expired")
        if self._prune_locked():
            await self._save_locked()

    @staticmethod
    def _parse(data: Any, key: str) -> dict[str, ButtonRecord]:
        records: dict[str, ButtonRecord] = {}
        raw = data.get(key) if isinstance(data, Mapping) else None
        if isinstance(raw, Mapping):
            for value in raw.values():
                if isinstance(value, Mapping):
                    record = ButtonRecord.from_dict(value)
                    if record is not None:
                        records[record.record_id] = record
        return records

    def _prune_locked(self) -> bool:
        """Move expired records out; return True if anything changed.

        An expired record whose message is known is handed to the retire
        pass (its buttons are still on screen); one with no message is
        simply dropped.
        """
        now = self._now()
        expired = [key for key, rec in self._records.items() if rec.is_expired(now)]
        for key in expired:
            record = self._records.pop(key)
            if record.message_id is not None and record.channel_id is not None:
                self._expired[key] = record
        return bool(expired)

    async def _save_locked(self) -> None:
        self._prune_locked()
        await self._store.async_save(
            {
                "records": {key: rec.to_dict() for key, rec in self._records.items()},
                "expired": {key: rec.to_dict() for key, rec in self._expired.items()},
            }
        )

    async def async_pop_expired(self, limit: int) -> list[ButtonRecord]:
        """Up to ``limit`` expired messages to retire, one record per message.

        Every record of a returned message leaves the store, so a message
        is retired once even when it carried several buttons.
        """
        async with self._lock:
            await self._load_locked()
            changed = self._prune_locked()
            picked: dict[tuple[int | None, int | None], ButtonRecord] = {}
            for record in self._expired.values():
                where = (record.channel_id, record.message_id)
                if where not in picked and len(picked) < limit:
                    picked[where] = record
            if picked:
                for key in [
                    key
                    for key, rec in self._expired.items()
                    if (rec.channel_id, rec.message_id) in picked
                ]:
                    del self._expired[key]
                changed = True
            if changed:
                await self._save_locked()
            return list(picked.values())

    async def record(
        self,
        action: str,
        *,
        payload: Mapping[str, Any] | None = None,
        allowed_user_ids: Iterable[int] = (),
        ttl_seconds: float | None = None,
        record_id: str | None = None,
        group_id: str | None = None,
        multi_use: bool = False,
    ) -> ButtonRecord:
        """Create and persist a button record; returns it (use its custom_id).

        Ids are short random tokens (never entity ids or text), and the
        resulting custom_id is asserted to fit Discord's 100 character limit.
        """
        record_id = record_id or secrets.token_urlsafe(8)
        now = self._now()
        ttl = self._ttl if ttl_seconds is None else float(ttl_seconds)
        record = ButtonRecord(
            record_id=record_id,
            action=action,
            created_at=now,
            expires_at=now + ttl,
            allowed_user_ids=tuple(int(value) for value in allowed_user_ids),
            payload=dict(payload or {}),
            group_id=group_id,
            multi_use=bool(multi_use),
        )
        # Raises (assert / ValueError) before anything is persisted when the
        # action or id would push the custom_id past Discord's limit.
        _ = record.custom_id
        async with self._lock:
            await self._load_locked()
            self._records[record.record_id] = record
            await self._save_locked()
        return record

    async def peek(self, record_id: str) -> ButtonRecord | None:
        """Return a fresh record without claiming it, or None.

        Expired records are refused and pruned (and the prune is saved).
        """
        async with self._lock:
            await self._load_locked()
            record = self._records.get(record_id)
            if record is None:
                return None
            if record.is_expired(self._now()):
                del self._records[record_id]
                await self._save_locked()
                return None
            return record

    async def take(self, record_id: str) -> ButtonRecord | None:
        """Claim a record: pop it and its siblings under the lock.

        A second take (or a take of an expired record) returns None, so each
        button press is single-use, and claiming one button of a group
        claims the whole group. A ``multi_use`` record is returned without
        being removed.
        """
        async with self._lock:
            await self._load_locked()
            record = self._records.get(record_id)
            if record is None:
                return None
            if record.is_expired(self._now()):
                del self._records[record_id]
                await self._save_locked()
                return None
            if record.multi_use:
                return record
            del self._records[record_id]
            if record.group_id is not None:
                for key in [
                    key
                    for key, rec in self._records.items()
                    if rec.group_id == record.group_id
                ]:
                    del self._records[key]
            await self._save_locked()
            return record

    async def bind_message(
        self, record_ids: Iterable[str], *, channel_id: int, message_id: int
    ) -> None:
        """Remember which Discord message carries these records' buttons."""
        ids = set(record_ids)
        async with self._lock:
            await self._load_locked()
            changed = False
            for key in ids:
                record = self._records.get(key)
                if record is not None:
                    self._records[key] = replace(
                        record, channel_id=channel_id, message_id=message_id
                    )
                    changed = True
            if changed:
                await self._save_locked()

    async def async_remove_where(
        self, predicate: Callable[[ButtonRecord], bool]
    ) -> list[ButtonRecord]:
        """Remove and return every record matching ``predicate``."""
        async with self._lock:
            await self._load_locked()
            removed = [rec for rec in self._records.values() if predicate(rec)]
            for rec in removed:
                del self._records[rec.record_id]
            stale = [rec for rec in self._expired.values() if predicate(rec)]
            for rec in stale:
                del self._expired[rec.record_id]
            removed.extend(stale)
            if removed:
                await self._save_locked()
            return removed

    async def async_remove(self) -> None:
        """Delete the persisted records (the config entry was removed)."""
        async with self._lock:
            self._records = {}
            self._expired = {}
            remove = getattr(self._store, "async_remove", None)
            if callable(remove):
                await remove()

    async def prune(self) -> int:
        """Prune expired records now; returns how many were dropped."""
        async with self._lock:
            await self._load_locked()
            before = len(self._records)
            if self._prune_locked():
                await self._save_locked()
            return before - len(self._records)

    def __len__(self) -> int:
        return len(self._records)


@dataclass(frozen=True)
class ButtonSpec:
    """One button to post: its label, action and what the handler needs."""

    label: str
    action: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    allowed_user_ids: tuple[int, ...] = ()
    multi_use: bool = False


def headline_of(text: str) -> str | None:
    """The first non-empty line of a message, struck through when retired."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:200]
    return None


def retired_content(
    current: str | None, outcome: str | None, headline: str | None = None
) -> str:
    """The message text once its buttons are retired.

    The headline is struck through where it appears (or prepended, struck,
    when the live text no longer contains it) and the outcome line is
    appended, for example "Andrew pressed Unlock front door".
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


async def async_send_with_buttons(
    store: ButtonStore,
    channel: Any,
    text: str,
    specs: Iterable[ButtonSpec],
    *,
    send: Callable[..., Awaitable[Any]] | None = None,
    ttl_seconds: float | None = None,
    **send_kwargs: Any,
) -> list[Any]:
    """Record, send and bind one message's buttons; return the sent messages.

    All buttons on the message share one group, so pressing one claims
    them all. Long text is chunked and the buttons ride on the last chunk.
    The view is stopped before sending: dispatch goes through the client's
    ``on_interaction`` by custom_id, and a running ``View(timeout=None)``
    would otherwise sit in discord.py's view store for the life of the
    client. ``send`` defaults to ``channel.send``; pass the client's
    recording ``send_to_channel`` (bound to the channel) to have the
    messages recorded as ours.
    """
    from .text_utils import chunk_message

    group_id = secrets.token_urlsafe(6)
    headline = headline_of(text)
    records = [
        await store.record(
            spec.action,
            payload={**spec.payload, "headline": headline},
            allowed_user_ids=spec.allowed_user_ids,
            ttl_seconds=ttl_seconds,
            group_id=group_id,
            multi_use=spec.multi_use,
        )
        for spec in specs
    ]
    view = discord.ui.View(timeout=None)
    for spec, record in zip(specs, records, strict=True):
        view.add_item(
            discord.ui.Button(
                label=spec.label[:80], style=spec.style, custom_id=record.custom_id
            )
        )
    view.stop()
    send = send or channel.send
    parts = chunk_message(text)
    sent: list[Any] = []
    for index, part in enumerate(parts):
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if index == len(parts) - 1:
            kwargs.update(send_kwargs)
            if records:
                kwargs["view"] = view
        sent.append(await send(part, **kwargs))
    last = sent[-1] if sent else None
    message_id = getattr(last, "id", None)
    channel_id = getattr(channel, "id", None)
    if records and isinstance(message_id, int) and isinstance(channel_id, int):
        await store.bind_message(
            [record.record_id for record in records],
            channel_id=channel_id,
            message_id=message_id,
        )
    return sent
