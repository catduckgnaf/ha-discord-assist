"""Persistent, restart-surviving Discord buttons.

Buttons carry a stateless ``custom_id`` of the form ``da:<action>:<record_id>``
and no ``discord.ui.View`` needs to stay alive for them to work. The record
(who may press, what to do, when it expires) is persisted in a Home Assistant
``Store``, so a button posted before a restart still works after it.

A press is single-use: :meth:`ButtonStore.take` pops the record under a lock,
so a second press finds nothing and is refused as already handled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

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

# A button action handler: given the claimed record and the raw interaction,
# return the text to show for the handled press, or None for a generic ack.
ButtonHandler = Callable[["ButtonRecord", Any], Awaitable[str | None]]


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


@dataclass(frozen=True)
class ButtonRecord:
    """One pending button: who may press it and what pressing it means."""

    record_id: str
    action: str
    created_at: float
    expires_at: float
    allowed_user_ids: tuple[int, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

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
        records: dict[str, ButtonRecord] = {}
        if isinstance(data, Mapping):
            raw = data.get("records")
            if isinstance(raw, Mapping):
                for value in raw.values():
                    if isinstance(value, Mapping):
                        record = ButtonRecord.from_dict(value)
                        if record is not None:
                            records[record.record_id] = record
        self._records = records
        if self._prune_locked():
            await self._save_locked()

    def _prune_locked(self) -> bool:
        """Drop expired records; return True if anything was dropped."""
        now = self._now()
        expired = [key for key, rec in self._records.items() if rec.is_expired(now)]
        for key in expired:
            del self._records[key]
        return bool(expired)

    async def _save_locked(self) -> None:
        self._prune_locked()
        await self._store.async_save(
            {"records": {key: rec.to_dict() for key, rec in self._records.items()}}
        )

    async def record(
        self,
        action: str,
        *,
        payload: Mapping[str, Any] | None = None,
        allowed_user_ids: Iterable[int] = (),
        ttl_seconds: float | None = None,
        record_id: str | None = None,
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
        """Claim a record once: pop it under the lock and return it if fresh.

        A second take (or a take of an expired record) returns None, so each
        button press is single-use.
        """
        async with self._lock:
            await self._load_locked()
            record = self._records.pop(record_id, None)
            if record is None:
                return None
            if record.is_expired(self._now()):
                await self._save_locked()
                return None
            await self._save_locked()
            return record

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
