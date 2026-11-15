import asyncio
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


def test_reset_clears_entry():
    cache = ConversationCache(timedelta(minutes=15), now=_clock())
    cache.set("channel:1", "conv-abc")
    cache.reset("channel:1")
    assert cache.get("channel:1") is None
    # Resetting a key with nothing cached is not an error.
    cache.reset("channel:1")


def test_ttl_override_extends_one_lookup():
    clock = _clock()
    cache = ConversationCache(timedelta(minutes=15), now=clock)
    cache.set("channel:1", "conv-long")
    cache.set("channel:2", "conv-default")
    clock.advance(timedelta(minutes=16))
    assert cache.get("channel:1", ttl=timedelta(minutes=60)) == "conv-long"
    assert cache.get("channel:2") is None
    clock.advance(timedelta(minutes=45))
    assert cache.get("channel:1", ttl=timedelta(minutes=60)) is None


def test_ttl_override_shortens_one_lookup():
    clock = _clock()
    cache = ConversationCache(timedelta(minutes=15), now=clock)
    cache.set("channel:1", "conv-short")
    clock.advance(timedelta(minutes=3))
    assert cache.get("channel:1", ttl=timedelta(minutes=2)) is None


async def test_turn_locks_evict_only_unused_keys():
    from custom_components.discord_assist.conversation_cache import TurnLocks

    locks = TurnLocks(max_keys=2)
    async with locks.hold("a"):
        async with locks.hold("b"):
            pass
        async with locks.hold("c"):
            pass
        # "b" was idle and evicted; "a" is in use and kept.
        assert len(locks) == 2
        assert "b" not in locks._locks
        assert "a" in locks._locks


async def test_turn_locks_keep_key_with_waiter_after_release():
    """A released lock with a queued turn must not be swapped for a new one."""
    from custom_components.discord_assist.conversation_cache import TurnLocks

    locks = TurnLocks(max_keys=1)
    order: list[str] = []
    first_in = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with locks.hold("a"):
            first_in.set()
            await release_first.wait()
            order.append("first done")

    async def second():
        async with locks.hold("a"):
            order.append("second start")

    t1 = asyncio.create_task(first())
    await first_in.wait()
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0)
    release_first.set()
    # While the second turn is waking, another key forces a prune.
    async with locks.hold("b"):
        pass
    async with locks.hold("a"):
        order.append("third")
    await asyncio.gather(t1, t2)
    assert order == ["first done", "second start", "third"]
