import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.intent import IntentResponseErrorCode, IntentResponseType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_AGENT_TIMEOUT,
    CONF_ALLOWLIST,
    CONF_PREFER_LOCAL,
    CONF_ROOM_MAP,
    CONF_USER_MAP,
    LOCAL_AGENT_ID,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    AgentTimeout,
    ConversationRouter,
    MessageContext,
    make_conversation_key,
    resolve_ha_user_id,
)

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"


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
        speaker_context=False,
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
        entry_id="entry-1",
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_ALLOWLIST: ["123456789", "not-a-number", "987654321"],
            CONF_USER_MAP: {"111": "ha-user-a", "bad-key": "ha-user-b"},
        },
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


def _result(speech="ok", conversation_id="conv", response_type=None, error_code=None):
    return SimpleNamespace(
        response=SimpleNamespace(
            speech={"plain": {"speech": speech}},
            response_type=response_type or IntentResponseType.ACTION_DONE,
            error_code=error_code,
        ),
        conversation_id=conversation_id,
    )


def test_room_device_for_channel_and_thread():
    router = _router(room_map={111: "dev-den"})
    assert router.device_for(111, None) == "dev-den"
    assert router.device_for(999, 111) == "dev-den"
    assert router.device_for(222, None) is None


async def test_process_passes_room_device():
    router = _router(user_map={7: "ha-a"}, room_map={111: "dev-den"})
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(
            text="close the blinds",
            discord_user_id=7,
            conversation_key="channel:999",
            where=MessageContext(channel_id=999, parent_id=111),
        )
    assert converse.call_args.kwargs["device_id"] == "dev-den"


async def test_prefer_local_handles_without_llm():
    router = _router(user_map={7: "ha-a"}, prefer_local=True)
    with patch(_CONVERSE, new=AsyncMock(return_value=_result("Turned off"))) as conv:
        reply = await router.process(
            text="turn off the lights", discord_user_id=7, conversation_key="dm:7"
        )
    assert reply == "Turned off"
    assert conv.await_count == 1
    assert conv.call_args.kwargs["agent_id"] == LOCAL_AGENT_ID
    # The local agent's conversation is not cached as the LLM's.
    assert router.cache.get("dm:7") is None


@pytest.mark.parametrize(
    "code",
    [IntentResponseErrorCode.NO_INTENT_MATCH, IntentResponseErrorCode.NO_VALID_TARGETS],
)
async def test_prefer_local_falls_back_when_not_understood(code):
    router = _router(user_map={7: "ha-a"}, prefer_local=True)
    local = _result("Sorry", response_type=IntentResponseType.ERROR, error_code=code)
    llm = _result("Done by LLM", conversation_id="llm-1")
    with patch(_CONVERSE, new=AsyncMock(side_effect=[local, llm])) as conv:
        reply = await router.process(
            text="make it cozy", discord_user_id=7, conversation_key="dm:7"
        )
    assert reply == "Done by LLM"
    assert conv.call_args_list[1].kwargs["agent_id"] == "conversation.ollama"
    assert router.cache.get("dm:7") == "llm-1"


async def test_prefer_local_does_not_retry_failed_action():
    """A matched intent that failed must not be re-run by the LLM."""
    router = _router(user_map={7: "ha-a"}, prefer_local=True)
    failed = _result(
        "Failed",
        response_type=IntentResponseType.ERROR,
        error_code=IntentResponseErrorCode.FAILED_TO_HANDLE,
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=failed)) as conv:
        reply = await router.process(
            text="unlock the door", discord_user_id=7, conversation_key="dm:7"
        )
    assert reply == "Failed"
    assert conv.await_count == 1


async def test_prefer_local_skipped_when_agent_is_local():
    router = _router(user_map={7: "ha-a"}, prefer_local=True, agent_id=LOCAL_AGENT_ID)
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as conv:
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    assert conv.await_count == 1


async def test_agent_timeout_raises():
    router = _router(user_map={7: "ha-a"}, agent_timeout=0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    with patch(_CONVERSE, new=slow), pytest.raises(AgentTimeout):
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    # The turn was released, so the next one runs.
    async with asyncio.timeout(1), router.turns.hold("dm:7"):
        pass


async def test_turns_in_one_conversation_run_one_at_a_time():
    router = _router(user_map={7: "ha-a"})
    active = 0
    peak = 0

    async def converse(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _result()

    with patch(_CONVERSE, new=converse):
        await asyncio.gather(
            *(
                router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
                for _ in range(3)
            )
        )
    assert peak == 1


async def test_speaker_prompt_names_speaker_room_and_channel(hass):
    user = await hass.auth.async_create_user("Andrew")
    area = ar.async_get(hass).async_create("Den")
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "den-speaker")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    router = _router(
        hass=hass,
        user_map={7: user.id},
        room_map={111: device.id},
        speaker_context=True,
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as conv:
        await router.process(
            text="close the blinds",
            discord_user_id=7,
            conversation_key="channel:111",
            where=MessageContext(
                display_name="Cat\nduck", channel_name="den", channel_id=111
            ),
        )
    prompt = conv.call_args.kwargs["extra_system_prompt"]
    assert "Speaker: Cat duck (Home Assistant user Andrew)." in prompt
    assert "Channel: #den." in prompt
    assert "in the Den" in prompt
    # Newlines in a Discord name cannot add lines to the prompt.
    assert prompt.count("\n") == 4


async def test_speaker_context_off_sends_no_prompt():
    router = _router(user_map={7: "ha-a"})
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as conv:
        await router.process(text="hi", discord_user_id=7, conversation_key="dm:7")
    assert conv.call_args.kwargs["extra_system_prompt"] is None


def test_from_entry_reads_new_options():
    entry = SimpleNamespace(
        entry_id="entry-1",
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_ROOM_MAP: {"111": "dev-den", "bad": "x"},
            CONF_PREFER_LOCAL: True,
            CONF_AGENT_TIMEOUT: 30,
        },
    )
    router = ConversationRouter.from_entry(object(), entry)
    assert router.room_map == {111: "dev-den"}
    assert router.prefer_local is True
    assert router.agent_timeout == 30.0
    assert router.speaker_context is True
