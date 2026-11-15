"""Persistent store for messages that carry buttons (issue #27 substrate).

Dependency note: issue #13 (persistent button store) is a separate lane that
is not on main. This module is the smallest self-contained substrate the
stale-button retirement in issue #27 needs: records carry the channel and
message id plus an optional headline, a take is single-use, and records
expire after a TTL. When #13 lands, this store is the likely rebase point:
the retirement helpers in :mod:`.retire` only rely on ``async_add``,
``get``, ``async_take``, ``expired`` and ``async_remove``, so they can be
rebased onto #13's store by adapting those methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1

# Buttons that nobody acts on should not stay live forever.
DEFAULT_BUTTON_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class ButtonRecord:
    """One message with buttons that may later need retiring."""

    channel_id: int
    message_id: int
    headline: str | None
    expires_at: datetime

    def to_dict(self) -> dict:
        """Serialize for the HA storage file."""
        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "headline": self.headline,
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ButtonRecord:
        """Deserialize one record loaded from storage."""
        return cls(
            channel_id=int(data["channel_id"]),
            message_id=int(data["message_id"]),
            headline=data.get("headline"),
            expires_at=dt_util.parse_datetime(data["expires_at"]),
        )


class ButtonStore:
    """Button records persisted with Home Assistant's storage helper."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_buttons_{entry_id}"
        )
        self._records: dict[int, ButtonRecord] = {}

    async def async_load(self) -> None:
        """Load persisted records, dropping any that fail to parse."""
        data = await self._store.async_load()
        if not data:
            return
        for item in data.get("records", []):
            try:
                record = ButtonRecord.from_dict(item)
            except KeyError, TypeError, ValueError:
                continue
            self._records[record.message_id] = record

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"records": [r.to_dict() for r in self._records.values()]}
        )

    def __len__(self) -> int:
        return len(self._records)

    def get(self, message_id: int) -> ButtonRecord | None:
        """Return the record for a message, if one is still held."""
        return self._records.get(message_id)

    async def async_add(
        self,
        *,
        channel_id: int,
        message_id: int,
        headline: str | None = None,
        ttl: timedelta = DEFAULT_BUTTON_TTL,
        now: datetime | None = None,
    ) -> ButtonRecord:
        """Register a button message and persist it."""
        record = ButtonRecord(
            channel_id=channel_id,
            message_id=message_id,
            headline=headline,
            expires_at=(now or dt_util.utcnow()) + ttl,
        )
        self._records[message_id] = record
        await self._async_save()
        return record

    async def async_take(self, message_id: int) -> ButtonRecord | None:
        """Remove and return a record exactly once; later takes return None."""
        record = self._records.pop(message_id, None)
        if record is None:
            return None
        await self._async_save()
        return record

    async def async_remove(self, message_id: int) -> ButtonRecord | None:
        """Drop a record without treating it as handled (prune path)."""
        return await self.async_take(message_id)

    def expired(self, now: datetime | None = None) -> list[ButtonRecord]:
        """All records past their TTL, oldest first."""
        moment = now or dt_util.utcnow()
        return sorted(
            (r for r in self._records.values() if r.expires_at <= moment),
            key=lambda r: r.expires_at,
        )
