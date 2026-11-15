from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.discord_assist.const import (
    CONF_RATE_LIMIT_BURST,
    CONF_RATE_LIMIT_PER_MINUTE,
)
from custom_components.discord_assist.conversation_cache import (
    ConversationCache,
    RateLimiter,
)
from custom_components.discord_assist.conversation_router import (
    ConversationRouter,
    RateLimited,
)

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _result(speech="ok"):
    return SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": speech}}),
        conversation_id="conv",
    )


def _router(**overrides):
    defaults = dict(
        hass=object(),
        agent_id="conversation.ollama",
        language=None,
        channels={111},
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={7: "ha-a", 8: "ha-b"},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        speaker_context=False,
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def test_limiter_burst_then_refusal_notifies_once():
    limiter = RateLimiter(per_minute=10, burst=3, now=FakeClock())
    assert [limiter.check("7").allowed for _ in range(3)] == [True, True, True]
    first = limiter.check("7")
    second = limiter.check("7")
    assert first.allowed is False
    assert first.should_notify is True
    assert second.allowed is False
    assert second.should_notify is False


def test_limiter_refill_allows_again_and_resets_notice():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=1, now=clock)
    assert limiter.check("7").allowed is True
    assert limiter.check("7").should_notify is True
    assert limiter.check("7").should_notify is False
    clock.advance(1)
    assert limiter.check("7").allowed is True
    # A new throttle window gets a fresh notice.
    assert limiter.check("7").should_notify is True


def test_limiter_disabled_at_zero():
    limiter = RateLimiter(per_minute=0, burst=3, now=FakeClock())
    assert limiter.enabled is False
    assert all(limiter.check("7").allowed for _ in range(50))
    assert len(limiter) == 0


def test_limiter_is_per_user_and_bounded():
    limiter = RateLimiter(per_minute=10, burst=1, max_keys=2, now=FakeClock())
    assert limiter.check("7").allowed is True
    assert limiter.check("7").allowed is False
    assert limiter.check("8").allowed is True
    limiter.check("9")
    assert len(limiter) <= 2


async def test_process_burst_refused_without_agent_call():
    router = _router(rate_limit_per_minute=10, rate_limit_burst=3)
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        for _ in range(3):
            reply = await router.process(
                text="hi", discord_user_id=7, conversation_key="dm:7"
            )
            assert reply == "ok"
        with pytest.raises(RateLimited) as first:
            await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
        with pytest.raises(RateLimited) as second:
            await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    assert converse.await_count == 3
    assert first.value.should_notify is True
    assert second.value.should_notify is False


async def test_process_refill_allows_again():
    clock = FakeClock()
    router = _router(
        rate_limiter=RateLimiter(per_minute=60, burst=1, now=clock),
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
        with pytest.raises(RateLimited):
            await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
        clock.advance(1)
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    assert converse.await_count == 2


async def test_process_disabled_at_zero():
    router = _router(rate_limit_per_minute=0)
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        for _ in range(10):
            await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    assert converse.await_count == 10


async def test_process_limit_is_per_user():
    router = _router(rate_limit_per_minute=10, rate_limit_burst=1)
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
        with pytest.raises(RateLimited):
            await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
        await router.process(text="hi", discord_user_id=8, conversation_key="dm:8")
    assert converse.await_count == 2


def test_router_check_rate_limit_decision_is_testable():
    router = _router(rate_limit_per_minute=10, rate_limit_burst=1)
    assert router.check_rate_limit(7).allowed is True
    denied = router.check_rate_limit(7)
    assert denied.allowed is False
    assert denied.should_notify is True
    assert router.check_rate_limit(7).should_notify is False


def test_from_entry_reads_rate_limit_options():
    entry = SimpleNamespace(
        options={
            CONF_RATE_LIMIT_PER_MINUTE: 5,
            CONF_RATE_LIMIT_BURST: 2,
        }
    )
    router = ConversationRouter.from_entry(object(), entry)
    assert router.rate_limit_per_minute == 5
    assert router.rate_limit_burst == 2
    assert router.rate_limiter.enabled is True


def test_from_entry_rate_limit_zero_disables_and_bad_value_uses_default():
    entry = SimpleNamespace(options={CONF_RATE_LIMIT_PER_MINUTE: 0})
    router = ConversationRouter.from_entry(object(), entry)
    assert router.rate_limiter.enabled is False

    bad = SimpleNamespace(options={CONF_RATE_LIMIT_PER_MINUTE: "soon"})
    router = ConversationRouter.from_entry(object(), bad)
    assert router.rate_limit_per_minute == 10
