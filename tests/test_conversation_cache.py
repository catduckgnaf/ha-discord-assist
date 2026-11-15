from datetime import UTC, datetime, timedelta

from custom_components.discord_assist.conversation_cache import ConversationCache


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta):
        self.now += delta


def _clock():
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def test_get_missing_returns_none():
    cache = ConversationCache(timedelta(minutes=15), now=_clock())
    assert cache.get("channel:1") is None


def test_set_then_get_returns_id():
    cache = ConversationCache(timedelta(minutes=15), now=_clock())
    cache.set("channel:1", "conv-abc")
    assert cache.get("channel:1") == "conv-abc"


def test_expired_entry_evicted():
    clock = _clock()
    cache = ConversationCache(timedelta(minutes=15), now=clock)
    cache.set("channel:1", "conv-abc")
    clock.advance(timedelta(minutes=16))
    assert cache.get("channel:1") is None


def test_within_ttl_survives():
    clock = _clock()
    cache = ConversationCache(timedelta(minutes=15), now=clock)
    cache.set("channel:1", "conv-abc")
    clock.advance(timedelta(minutes=14))
    assert cache.get("channel:1") == "conv-abc"


def test_set_none_clears_entry():
    cache = ConversationCache(timedelta(minutes=15), now=_clock())
    cache.set("channel:1", "conv-abc")
    cache.set("channel:1", None)
    assert cache.get("channel:1") is None


async def test_turn_locks_evict_idle_but_keep_held():
    from custom_components.discord_assist.conversation_cache import TurnLocks

    locks = TurnLocks(max_keys=2)
    held = locks.get("a")
    await held.acquire()
    locks.get("b")
    locks.get("c")  # evicts "b", the oldest idle lock; "a" is held
    assert len(locks) == 2
    assert locks.get("a") is held
    held.release()
