"""Per-conversation-key state: conversation_id cache and turn locks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
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

    def __len__(self) -> int:
        return len(self._store)

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
    same conversation_id: the second waits for the first. A key is only
    evicted when no turn holds or waits for its lock; checking ``locked()``
    alone is not enough, because a just-released lock looks idle until the
    next waiter wakes up and takes it.
    """

    def __init__(self, max_keys: int = 256) -> None:
        self._max = max_keys
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._users: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        """Run one turn for ``key``; later turns for the same key queue."""
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        else:
            self._locks.move_to_end(key)
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
            self._prune()

    def _prune(self) -> None:
        while len(self._locks) > self._max:
            idle = next((k for k in self._locks if k not in self._users), None)
            if idle is None:
                return
            del self._locks[idle]
