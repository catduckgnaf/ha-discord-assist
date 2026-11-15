"""Per-conversation-key state: conversation_id cache and turn locks."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one rate-limit check.

    ``should_notify`` is True only for the first refusal in a throttle
    window, so the caller can send one notice and then stay silent until
    the bucket refills enough to allow a request again.
    """

    allowed: bool
    should_notify: bool = False


@dataclass
class _Bucket:
    tokens: float
    updated: float
    notified: bool = False


class RateLimiter:
    """Token bucket per key, kept in a bounded LRU like ``TurnLocks``.

    Each key starts with ``burst`` tokens and refills at
    ``per_minute`` tokens per minute. A ``per_minute`` of 0 (or less)
    disables limiting entirely. Buckets are in memory only and the map
    never grows past ``max_keys`` entries.
    """

    def __init__(
        self,
        per_minute: float,
        burst: int = 3,
        max_keys: int = 256,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_minute = float(per_minute or 0)
        self._capacity = max(1, int(burst)) if self.enabled else 0
        self._max = max_keys
        self._now = now
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._per_minute > 0

    def __len__(self) -> int:
        return len(self._buckets)

    def check(self, key: str) -> RateLimitDecision:
        """Consume one token for ``key``, or report a refusal."""
        if not self.enabled:
            return RateLimitDecision(allowed=True)
        now = self._now()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(
                tokens=float(self._capacity), updated=now
            )
        else:
            self._buckets.move_to_end(key)
            elapsed = max(0.0, now - bucket.updated)
            refill = elapsed * self._per_minute / 60.0
            bucket.tokens = min(float(self._capacity), bucket.tokens + refill)
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            bucket.notified = False
            decision = RateLimitDecision(allowed=True)
        else:
            decision = RateLimitDecision(
                allowed=False, should_notify=not bucket.notified
            )
            bucket.notified = True
        self._prune()
        return decision

    def _prune(self) -> None:
        while len(self._buckets) > self._max:
            self._buckets.popitem(last=False)


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
