"""Persistent button records + custom_id helpers (stateless dispatcher substrate).

This is the smallest self-contained slice of the button dispatcher design:
buttons carry a stateless ``custom_id`` of the form ``da:<action>:<record_id>``
and a single ``on_interaction`` dispatcher routes by action, so no
``discord.ui.View`` object needs to stay alive for a button to work. The
record (who may press, what to do, when it was created) lives here, backed by
a Home Assistant ``Store`` when one is configured, and each record is
single-use: ``take()`` pops it, so a second press finds nothing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

PREFIX = "da"
ANSWER_ACTION = "answer"
MAX_CUSTOM_ID_LENGTH = 100
STORAGE_VERSION = 1
# Matches the Print Handy alert-button TTL the dispatcher design ports.
DEFAULT_BUTTON_TTL_SECONDS = 24 * 3600


def make_custom_id(action: str, record_id: str) -> str:
    """Build a ``da:<action>:<record_id>`` custom id (max 100 chars)."""
    custom_id = f"{PREFIX}:{action}:{record_id}"
    if len(custom_id) > MAX_CUSTOM_ID_LENGTH:
        raise ValueError(f"custom_id too long: {len(custom_id)} chars")
    return custom_id


def parse_custom_id(custom_id: str | None) -> tuple[str, str] | None:
    """Split a custom id into (action, record_id), or None if it is not ours."""
    if not custom_id:
        return None
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def needs_clarify_buttons(
    speech: str, continue_conversation: bool, *, heuristic: bool = False
) -> bool:
    """Whether a reply should carry Yes / No clarify buttons.

    ``continue_conversation`` (the agent saying it expects a follow-up) is
    the real signal. The "ends with ?" heuristic only applies when the
    ``clarify_buttons_heuristic`` option turns it on.
    """
    if continue_conversation:
        return True
    return heuristic and speech.strip().endswith("?")


@dataclass
class ButtonRecord:
    """One pending button: who may press it and what a press should do."""

    record_id: str
    action: str
    conversation_key: str
    answer: str
    speaker_id: int
    channel_id: int | None = None
    message_id: int | None = None
    group_id: str | None = None
    created_at: float = 0.0

    @property
    def custom_id(self) -> str:
        return make_custom_id(self.action, self.record_id)

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "action": self.action,
            "conversation_key": self.conversation_key,
            "answer": self.answer,
            "speaker_id": self.speaker_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "group_id": self.group_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ButtonRecord | None:
        try:
            return cls(
                record_id=str(data["record_id"]),
                action=str(data["action"]),
                conversation_key=str(data["conversation_key"]),
                answer=str(data["answer"]),
                speaker_id=int(data["speaker_id"]),
                channel_id=data.get("channel_id"),
                message_id=data.get("message_id"),
                group_id=data.get("group_id"),
                created_at=float(data.get("created_at") or 0.0),
            )
        except KeyError, TypeError, ValueError:
            _LOGGER.warning("Ignoring malformed button record: %r", data)
            return None


class ButtonStore:
    """Button records with a TTL, optionally persisted in a HA ``Store``.

    Without a storage key the records live in memory for the lifetime of the
    store (still outliving any single View); with one, they are written
    through to Home Assistant storage so buttons survive restarts.
    """

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        *,
        storage_key: str | None = None,
        ttl_seconds: float = DEFAULT_BUTTON_TTL_SECONDS,
        now: Callable[[], float] = time.time,
        store: Store | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._records: dict[str, ButtonRecord] = {}
        self._lock = asyncio.Lock()
        self._loaded = store is None and storage_key is None
        self._store = store or (
            Store(hass, STORAGE_VERSION, storage_key)
            if hass is not None and storage_key is not None
            else None
        )

    def __len__(self) -> int:
        return len(self._records)

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        data = await self._store.async_load()
        for row in (data or {}).get("records", []):
            record = ButtonRecord.from_dict(row)
            if record is not None:
                self._records[record.record_id] = record
        if self._prune_expired():
            await self._save()

    async def _save(self) -> None:
        if self._store is not None:
            await self._store.async_save(
                {"records": [r.to_dict() for r in self._records.values()]}
            )

    def _is_fresh(self, record: ButtonRecord) -> bool:
        return self._now() - record.created_at <= self._ttl

    def _prune_expired(self) -> bool:
        expired = [rid for rid, r in self._records.items() if not self._is_fresh(r)]
        for rid in expired:
            del self._records[rid]
        return bool(expired)

    async def record(
        self,
        *,
        action: str,
        conversation_key: str,
        answer: str,
        speaker_id: int,
        channel_id: int | None = None,
        group_id: str | None = None,
    ) -> ButtonRecord:
        """Create a fresh record and return it (its id goes in the custom_id)."""
        await self._ensure_loaded()
        record = ButtonRecord(
            record_id=secrets.token_urlsafe(8),
            action=action,
            conversation_key=conversation_key,
            answer=answer,
            speaker_id=speaker_id,
            channel_id=channel_id,
            group_id=group_id,
            created_at=self._now(),
        )
        # Raises if the id would not fit in a Discord custom_id.
        _ = record.custom_id
        async with self._lock:
            self._prune_expired()
            self._records[record.record_id] = record
            await self._save()
        return record

    async def peek(self, record_id: str) -> ButtonRecord | None:
        """The record if it exists and is fresh, without consuming it."""
        await self._ensure_loaded()
        record = self._records.get(record_id)
        if record is None:
            return None
        if not self._is_fresh(record):
            async with self._lock:
                self._records.pop(record_id, None)
                await self._save()
            return None
        return record

    async def take(self, record_id: str) -> ButtonRecord | None:
        """Pop a record for use, once. A second take of the same id gets None.

        Records sharing the taken record's group (the Yes and No of one
        question) are popped too: answering one way retires the other.
        """
        await self._ensure_loaded()
        async with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None
            if not self._is_fresh(record):
                del self._records[record_id]
                await self._save()
                return None
            del self._records[record_id]
            if record.group_id is not None:
                siblings = [
                    rid
                    for rid, r in self._records.items()
                    if r.group_id == record.group_id
                ]
                for rid in siblings:
                    del self._records[rid]
            await self._save()
            return record

    async def bind_message(
        self, record_ids: list[str], *, message_id: int, channel_id: int
    ) -> None:
        """Remember which Discord message carries these records' buttons."""
        await self._ensure_loaded()
        async with self._lock:
            changed = False
            for rid in record_ids:
                record = self._records.get(rid)
                if record is not None:
                    self._records[rid] = replace(
                        record, message_id=message_id, channel_id=channel_id
                    )
                    changed = True
            if changed:
                await self._save()

    async def invalidate_for_conversation(
        self, conversation_key: str
    ) -> list[ButtonRecord]:
        """Drop every pending record for a conversation (a newer turn happened)."""
        await self._ensure_loaded()
        async with self._lock:
            removed = [
                r
                for r in self._records.values()
                if r.conversation_key == conversation_key
            ]
            for record in removed:
                del self._records[record.record_id]
            if removed:
                await self._save()
            return removed
