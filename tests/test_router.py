from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_ALLOWLIST,
    CONF_USER_MAP,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    ConversationRouter,
    make_conversation_key,
    resolve_ha_user_id,
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
        user_map={},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def test_make_conversation_key_dm_vs_channel():
    assert make_conversation_key(is_dm=True, channel_id=5, author_id=9) == "dm:9"
    assert make_conversation_key(is_dm=False, channel_id=5, author_id=9) == "channel:5"


@pytest.mark.parametrize(
    ("user_map", "fallback", "discord_id", "expected"),
    [
        ({7: "ha-a"}, "ha-fb", 7, "ha-a"),
        ({7: "ha-a"}, "ha-fb", 8, "ha-fb"),
        ({}, None, 8, None),
    ],
)
def test_resolve_ha_user_id(user_map, fallback, discord_id, expected):
    assert resolve_ha_user_id(user_map, fallback, discord_id) == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(is_dm=True, was_mentioned=False, channel_id=0, author_id=1), True),
        (dict(is_dm=False, was_mentioned=False, channel_id=111, author_id=1), True),
        (dict(is_dm=False, was_mentioned=True, channel_id=222, author_id=1), True),
        (dict(is_dm=False, was_mentioned=False, channel_id=222, author_id=1), False),
    ],
)
def test_should_respond_scope(kwargs, expected):
    assert _router(user_map={1: "ha-a"}).should_respond(**kwargs) is expected


def test_should_respond_dms_disabled():
    router = _router(respond_dms=False, user_map={1: "ha-a"})
    result = router.should_respond(
        is_dm=True, was_mentioned=False, channel_id=0, author_id=1
    )
    assert result is False


def test_should_respond_allowlist_blocks_unlisted():
    router = _router(allowlist={42}, fallback_user="ha-fb")
    unlisted = router.should_respond(
        is_dm=True, was_mentioned=False, channel_id=0, author_id=1
    )
    listed = router.should_respond(
        is_dm=True, was_mentioned=False, channel_id=0, author_id=42
    )
    assert unlisted is False
    assert listed is True


async def test_process_calls_async_converse_and_caches():
    router = _router(user_map={7: "ha-a"})
    fake_result = SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": "Hi there"}}),
        conversation_id="conv-1",
    )
    with patch(
        "custom_components.discord_assist.conversation_router.async_converse",
        new=AsyncMock(return_value=fake_result),
    ) as mock_converse:
        reply = await router.process(
            text="hello", discord_user_id=7, conversation_key="channel:111"
        )
    assert reply == "Hi there"
    # conversation_id was cached for the next turn
    assert router.cache.get("channel:111") == "conv-1"
    # mapped user identity passed via Context(user_id=...)
    _, kwargs = mock_converse.call_args
    assert kwargs["agent_id"] == "conversation.ollama"
    ctx = mock_converse.call_args.args[3]
    assert ctx.user_id == "ha-a"


async def test_process_handles_missing_speech():
    router = _router(user_map={1: "ha-a"})
    fake_result = SimpleNamespace(
        response=SimpleNamespace(speech={}), conversation_id="conv-2"
    )
    with patch(
        "custom_components.discord_assist.conversation_router.async_converse",
        new=AsyncMock(return_value=fake_result),
    ):
        reply = await router.process(
            text="hi", discord_user_id=1, conversation_key="dm:1"
        )
    assert reply == ""


def test_from_entry_skips_non_numeric_allowlist_and_user_map():
    """Non-numeric allowlist entries and user_map keys are silently dropped."""
    from types import SimpleNamespace

    entry = SimpleNamespace(
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_ALLOWLIST: ["123456789", "not-a-number", "987654321"],
            CONF_USER_MAP: {"111": "ha-user-a", "bad-key": "ha-user-b"},
        }
    )
    hass = object()
    router = ConversationRouter.from_entry(hass, entry)
    assert router.allowlist == {123456789, 987654321}
    assert router.user_map == {111: "ha-user-a"}


def test_should_respond_fails_closed_with_no_identity():
    """No allowlist, no mapping, no fallback: nobody gets in."""
    router = _router()
    result = router.should_respond(
        is_dm=True, was_mentioned=False, channel_id=0, author_id=1
    )
    assert result is False
    assert router.has_authorized_users is False


def test_allowlisted_user_needs_fallback_identity():
    no_fallback = _router(allowlist={42})
    with_fallback = _router(allowlist={42}, fallback_user="ha-fb")
    kwargs = dict(is_dm=True, was_mentioned=False, channel_id=0, author_id=42)
    assert no_fallback.should_respond(**kwargs) is False
    assert with_fallback.should_respond(**kwargs) is True


def test_fallback_alone_does_not_open_bot_to_everyone():
    router = _router(fallback_user="ha-fb")
    result = router.should_respond(
        is_dm=True, was_mentioned=False, channel_id=0, author_id=1
    )
    assert result is False


def test_should_respond_in_thread_of_listened_channel():
    router = _router(user_map={1: "ha-a"})
    in_thread = router.should_respond(
        is_dm=False, was_mentioned=False, channel_id=999, author_id=1, parent_id=111
    )
    other_thread = router.should_respond(
        is_dm=False, was_mentioned=False, channel_id=998, author_id=1, parent_id=222
    )
    assert in_thread is True
    assert other_thread is False


async def test_process_refuses_unauthorized_user():
    router = _router()
    with pytest.raises(PermissionError):
        await router.process(text="hi", discord_user_id=1, conversation_key="dm:1")
