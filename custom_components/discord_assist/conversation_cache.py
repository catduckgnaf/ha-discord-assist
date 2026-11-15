"""Per-conversation-key state: conversation_id cache and turn locks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util


class ConversationCache:
    """Maps a conversation key to (conversation_id, last_used); evicts on idle TTL."""

    def __init__(
        self,
        ttl: timedelta,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._ttl = ttl
        self._now = now
        self._store: dict[str, tuple[str, datetime]] = {}

    def get(self, key: str) -> str | None:
        # Eviction is lazy (only on access) by design: inactive keys persist until
        # next access, which is acceptable at home scale.
        item = self._store.get(key)
        if item is None:
            return None
        conversation_id, last_used = item
        if self._now() - last_used > self._ttl:
            del self._store[key]
            return None
        return conversation_id

    def set(self, key: str, conversation_id: str | None) -> None:
        if conversation_id is None:
            self._store.pop(key, None)
            return
        self._store[key] = (conversation_id, self._now())


class TurnLocks:
    """One FIFO lock per conversation key, kept in a bounded LRU.

    Two messages in the same channel must not race through the agent with the
    same conversation_id: the second waits for the first. Locks that are held
    are never evicted.
    """

    def __init__(self, max_keys: int = 256) -> None:
        self._max = max_keys
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    def __len__(self) -> int:
        return len(self._locks)

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is not None:
            self._locks.move_to_end(key)
            return lock
        lock = self._locks[key] = asyncio.Lock()
        while len(self._locks) > self._max:
            idle = next((k for k, v in self._locks.items() if not v.locked()), None)
            if idle is None or idle == key:
                break
            del self._locks[idle]
        return lock
