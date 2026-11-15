import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import custom_components.discord_assist.bot as bot_module
from custom_components.discord_assist.bot import (
    ERROR_REPLY,
    PLACEHOLDER_REPLY,
    RATE_LIMIT_REPLY,
    TIMEOUT_REPLY,
    DiscordAssistClient,
)
from custom_components.discord_assist.const import DEFAULT_PLACEHOLDER_DELAY
from custom_components.discord_assist.conversation_cache import RateLimitDecision
from custom_components.discord_assist.conversation_router import AgentTimeout
from custom_components.discord_assist.text_utils import chunk_message
from tests.fake_discord import (
    FakeDMChannel,
    FakeTextChannel,
    FakeThread,
    FakeUser,
    assert_first_send_is_reply,
)


def _message(
    content,
    *,
    author_bot=False,
    is_dm=False,
    mentions=None,
    channel_id=111,
    parent_id=None,
):
    if is_dm:
        channel = FakeDMChannel(id=channel_id)
    elif parent_id is not None:
        channel = FakeThread(id=channel_id, parent_id=parent_id)
    else:
        channel = FakeTextChannel(id=channel_id)
    return channel.receive_message(
        content,
        author=FakeUser(id=7, bot=author_bot, display_name="Catduck"),
        mentions=mentions or [],
    )


def _first_send(msg):
    return msg.channel.send_calls[0]


def _hass():
    return SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))


def _client(router, monkeypatch, *, placeholder_delay=None):
    kwargs = {}
    if placeholder_delay is not None:
        kwargs["placeholder_delay"] = placeholder_delay
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=_hass(), router=router, status_signal="test_signal", **kwargs
        )
    # discord.Client.user is normally read-only; stub it for tests
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client


async def test_on_message_dispatches_and_replies(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="The light is on.")
    client = _client(router, monkeypatch)
    msg = _message("<@999> is the light on?", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    router.process.assert_awaited_once()
    assert len(msg.channel.sent) == 1
    call = assert_first_send_is_reply(msg.channel, to=msg)
    assert call.content == "The light is on."
    assert call.kwargs["mention_author"] is False
    assert msg.to_reference_calls == [{"fail_if_not_exists": False}]
    where = router.process.call_args.kwargs["where"]
    assert where.display_name == "Catduck"
    assert where.channel_id == 111


async def test_on_message_ignores_other_bots(monkeypatch):
    router = MagicMock()
    client = _client(router, monkeypatch)
    msg = _message("hi", author_bot=True)
    await client.on_message(msg)
    router.should_respond.assert_not_called()


async def test_on_message_skips_when_should_respond_false(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = False
    router.process = AsyncMock()
    client = _client(router, monkeypatch)
    await client.on_message(_message("random channel chatter"))
    router.process.assert_not_called()


async def test_on_message_posts_apology_on_error(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(side_effect=RuntimeError("boom"))
    client = _client(router, monkeypatch)
    msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    sent = msg.channel.sent[0]
    assert "error" in sent.content.lower()


async def test_on_message_replies_when_typing_fails(monkeypatch):
    """A typing-indicator failure must not block the real reply (spec §11)."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Lights are off.")
    client = _client(router, monkeypatch)

    msg = _message("<@999> lights?", mentions=[FakeUser(id=999)])
    # Fake channel whose typing raises every time it is triggered
    msg.channel.typing_enter_error = discord.HTTPException(MagicMock(), "forbidden")

    await client.on_message(msg)

    router.process.assert_awaited_once()
    assert len(msg.channel.sent) == 1
    call = assert_first_send_is_reply(msg.channel, to=msg)
    assert call.content == "Lights are off."
    assert call.kwargs["mention_author"] is False


async def test_on_message_does_not_raise_when_send_fails(monkeypatch):
    """A send failure must be caught and logged, not propagated."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Reply text.")
    client = _client(router, monkeypatch)

    msg = _message("<@999> hello", mentions=[FakeUser(id=999)])
    msg.channel.send_error = discord.HTTPException(MagicMock(), "missing permissions")

    # Must not raise
    await client.on_message(msg)
    assert msg.channel.sent == []


def test_client_disables_all_mentions():
    with patch("discord.Client.__init__", return_value=None) as mock_init:
        DiscordAssistClient(hass=object(), router=MagicMock(), status_signal="s")
    mentions = mock_init.call_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


async def test_on_message_passes_thread_parent(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = False
    client = _client(router, monkeypatch)
    await client.on_message(_message("hi", channel_id=999, parent_id=111))
    assert router.should_respond.call_args.kwargs["parent_id"] == 111


async def test_connection_state_dispatches_changes(monkeypatch):
    client = _client(MagicMock(), monkeypatch)
    with patch(
        "custom_components.discord_assist.bot.async_dispatcher_send"
    ) as mock_send:
        await client.on_ready()
        await client.on_ready()
        assert client.connected is True
        await client.on_disconnect()
        assert client.connected is False
    assert mock_send.call_count == 2


async def test_on_message_replies_on_timeout(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(side_effect=AgentTimeout())
    client = _client(router, monkeypatch)
    msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    assert _first_send(msg).content == TIMEOUT_REPLY


async def test_on_message_slow_down_notice_once(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.check_rate_limit.return_value = RateLimitDecision(False, True)
    router.process = AsyncMock(return_value="never")
    client = _client(router, monkeypatch)
    msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    router.process.assert_not_called()
    assert _first_send(msg).content == RATE_LIMIT_REPLY
    assert "Slow down" in RATE_LIMIT_REPLY
    # Throttled before any typing indicator was shown.
    assert msg.channel.typing_calls == 0


async def test_on_message_rate_limited_silence_after_notice(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.check_rate_limit.return_value = RateLimitDecision(False, False)
    router.process = AsyncMock(return_value="never")
    client = _client(router, monkeypatch)
    msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    router.process.assert_not_called()
    assert msg.channel.send_calls == []
    assert msg.channel.typing_calls == 0


async def test_on_message_allowed_skips_second_rate_check(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.check_rate_limit.return_value = RateLimitDecision(True, False)
    router.process = AsyncMock(return_value="ok")
    client = _client(router, monkeypatch)
    msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    router.check_rate_limit.assert_called_once_with(7)
    assert router.process.call_args.kwargs["rate_checked"] is True


async def test_long_reply_only_first_piece_is_a_reply(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="word\n" * 1000)
    client = _client(router, monkeypatch)
    msg = _message("<@999> essay", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    calls = msg.channel.send_calls
    assert len(calls) > 1
    assert_first_send_is_reply(msg.channel, to=msg)
    assert calls[0].kwargs["mention_author"] is False
    assert all(set(c.kwargs) == {"nonce"} for c in calls[1:])


async def test_typing_failure_does_not_rerun_agent(monkeypatch):
    """A typing trigger that raises on every refresh never re-runs the agent."""
    monkeypatch.setattr(bot_module, "TYPING_REFRESH_SECONDS", 0.01)
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_process(**kwargs):
        await asyncio.sleep(0.05)
        return "Unlocked."

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch)
    msg = _message("<@999> unlock", mentions=[FakeUser(id=999)])
    msg.channel.typing_enter_error = discord.HTTPException(MagicMock(), "gone")
    await client.on_message(msg)
    router.process.assert_awaited_once()
    assert msg.channel.typing_calls > 1
    assert _first_send(msg).content == "Unlocked."


async def test_typing_stops_at_cap_while_agent_still_working(monkeypatch):
    """The indicator stops after the cap even if the agent is still working."""
    monkeypatch.setattr(bot_module, "TYPING_CAP_SECONDS", 0.03)
    monkeypatch.setattr(bot_module, "TYPING_REFRESH_SECONDS", 0.01)
    router = MagicMock()
    router.should_respond.return_value = True
    calls_when_agent_finished = []

    async def slow_process(**kwargs):
        await asyncio.sleep(0.15)
        calls_when_agent_finished.append(msg.channel.typing_calls)
        return "Done."

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    await client.on_message(msg)

    router.process.assert_awaited_once()
    assert _first_send(msg).content == "Done."
    # Refreshing every 0.01s for the agent's 0.15s would be ~15 triggers;
    # the 0.03s cap bounds it to a handful, and none happen after the cap.
    assert 1 <= calls_when_agent_finished[0] <= 5
    assert msg.channel.typing_calls == calls_when_agent_finished[0]


async def test_typing_stops_when_answer_is_ready(monkeypatch):
    """Typing is cancelled once the reply is ready, well before the cap."""
    router = MagicMock()
    router.should_respond.return_value = True

    async def quick_process(**kwargs):
        await asyncio.sleep(0.01)
        return "Quick."

    router.process = AsyncMock(side_effect=quick_process)
    client = _client(router, monkeypatch)
    msg = _message("<@999> quick", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    await asyncio.sleep(0.05)
    router.process.assert_awaited_once()
    assert msg.channel.typing_calls == 1
    assert _first_send(msg).content == "Quick."


async def test_dm_has_no_channel_name(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    assert router.process.call_args.kwargs["where"].channel_name is None


def _assert_no_mentions(call):
    mentions = call.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


def _slow(result, delay=0.2):
    async def process(**kwargs):
        await asyncio.sleep(delay)
        if isinstance(result, BaseException):
            raise result
        return result

    return AsyncMock(side_effect=process)


def _slow_client(router, monkeypatch):
    router.should_respond.return_value = True
    return _client(router, monkeypatch, placeholder_delay=0.05)


def test_placeholder_delay_defaults_to_three_seconds(monkeypatch):
    client = _client(MagicMock(), monkeypatch)
    assert DEFAULT_PLACEHOLDER_DELAY == 3.0
    assert client.placeholder_delay == 3.0


async def test_fast_answer_never_shows_placeholder(monkeypatch):
    router = MagicMock()
    router.process = AsyncMock(return_value="Quick answer.")
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> quick", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    # Exactly one send: the answer itself, as a reply. No placeholder.
    assert len(msg.channel.send_calls) == 1
    call = assert_first_send_is_reply(msg.channel, to=msg)
    assert call.content == "Quick answer."


async def test_slow_answer_posts_one_placeholder_and_edits_it(monkeypatch):
    router = MagicMock()
    router.process = _slow("Slow answer.")
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    router.process.assert_awaited_once()
    # One send only: the placeholder, posted as a reply.
    assert len(msg.channel.send_calls) == 1
    call = assert_first_send_is_reply(msg.channel, to=msg)
    assert call.content == PLACEHOLDER_REPLY
    assert call.kwargs["mention_author"] is False
    # The answer replaces the placeholder in place, with no mentions.
    placeholder = call.message
    assert len(placeholder.edits) == 1
    assert placeholder.content == "Slow answer."
    _assert_no_mentions(placeholder.edits[0])


async def test_slow_long_answer_edits_first_chunk_and_sends_rest(monkeypatch):
    router = MagicMock()
    answer = "word\n" * 1000
    router.process = _slow(answer)
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> essay", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    chunks = chunk_message(answer)
    assert len(chunks) > 1
    sends = msg.channel.send_calls
    # First send is the placeholder; the rest are the remaining chunks.
    assert sends[0].content == PLACEHOLDER_REPLY
    assert [c.content for c in sends[1:]] == chunks[1:]
    assert all(set(c.kwargs) == {"nonce"} for c in sends[1:])
    placeholder = sends[0].message
    assert len(placeholder.edits) == 1
    assert placeholder.content == chunks[0]
    _assert_no_mentions(placeholder.edits[0])


async def test_slow_timeout_edits_placeholder(monkeypatch):
    router = MagicMock()
    router.process = _slow(AgentTimeout())
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    assert len(msg.channel.send_calls) == 1
    placeholder = assert_first_send_is_reply(msg.channel, to=msg).message
    assert len(placeholder.edits) == 1
    assert placeholder.content == TIMEOUT_REPLY
    _assert_no_mentions(placeholder.edits[0])


async def test_slow_error_edits_placeholder(monkeypatch):
    router = MagicMock()
    router.process = _slow(RuntimeError("boom"))
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    await client.on_message(msg)
    placeholder = assert_first_send_is_reply(msg.channel, to=msg).message
    assert len(placeholder.edits) == 1
    assert placeholder.content == ERROR_REPLY
    _assert_no_mentions(placeholder.edits[0])


async def test_fast_timeout_and_error_show_no_placeholder(monkeypatch):
    for side_effect, expected in [
        (AgentTimeout(), TIMEOUT_REPLY),
        (RuntimeError("boom"), ERROR_REPLY),
    ]:
        router = MagicMock()
        router.process = AsyncMock(side_effect=side_effect)
        client = _slow_client(router, monkeypatch)
        msg = _message("<@999> hi", mentions=[FakeUser(id=999)])
        await client.on_message(msg)
        assert len(msg.channel.send_calls) == 1
        assert assert_first_send_is_reply(msg.channel, to=msg).content == expected


async def test_placeholder_edit_failure_falls_back_to_send(monkeypatch):
    router = MagicMock()
    router.process = _slow("Recovered answer.")
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    channel_send = msg.channel.send

    async def send(content=None, **kwargs):
        sent = await channel_send(content, **kwargs)
        sent.edit_error = discord.HTTPException(MagicMock(), "gone")
        return sent

    msg.channel.send = send
    await client.on_message(msg)
    sends = msg.channel.send_calls
    assert len(sends[0].message.edits) == 1
    # Placeholder send + fallback send of the answer as a reply.
    assert len(sends) == 2
    assert sends[1].content == "Recovered answer."


async def test_placeholder_send_failure_falls_back_to_send(monkeypatch):
    router = MagicMock()
    router.process = _slow("Answer despite no placeholder.")
    client = _slow_client(router, monkeypatch)
    msg = _message("<@999> slow", mentions=[FakeUser(id=999)])
    channel_send = msg.channel.send
    attempts = []

    async def send(content=None, **kwargs):
        attempts.append(content)
        if len(attempts) == 1:
            raise discord.HTTPException(MagicMock(), "missing permissions")
        return await channel_send(content, **kwargs)

    msg.channel.send = send
    await client.on_message(msg)
    assert attempts == [PLACEHOLDER_REPLY, "Answer despite no placeholder."]
    assert [c.content for c in msg.channel.send_calls] == [
        "Answer despite no placeholder."
    ]


def _event_client(router, monkeypatch, *, exclude_text=False):
    router.should_respond.return_value = True
    router.check_rate_limit.return_value = RateLimitDecision(True, False)
    router.ha_user_for.return_value = (
        "ha-a"
        if isinstance(router.ha_user_for.return_value, MagicMock)
        else router.ha_user_for.return_value
    )
    router.config_entry_id = "entry-123"
    router.event_exclude_text = exclude_text
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    client.hass = SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))
    return client


async def test_accepted_message_fires_event_once_with_fields(monkeypatch):
    router = MagicMock()
    client = _event_client(router, monkeypatch)
    msg = _message("<@999> movie time", mentions=[FakeUser(id=999)])
    msg.channel.name = "general"
    await client.on_message(msg)
    client.hass.bus.async_fire.assert_called_once()
    event_type, data = client.hass.bus.async_fire.call_args.args
    context = client.hass.bus.async_fire.call_args.kwargs["context"]
    assert event_type == "discord_assist_message"
    # Snowflakes go out as strings so the frontend cannot round them.
    assert data == {
        "text": "movie time",
        "channel_id": "111",
        "channel_name": "general",
        "is_dm": False,
        "discord_user_id": "7",
        "ha_user_id": "ha-a",
        "message_id": str(msg.id),
        "config_entry_id": "entry-123",
    }
    assert context.user_id == "ha-a"
    router.ha_user_for.assert_called_with(7)


async def test_rejected_message_fires_no_event(monkeypatch):
    router = MagicMock()
    client = _event_client(router, monkeypatch)
    router.should_respond.return_value = False
    await client.on_message(_message("random channel chatter"))
    client.hass.bus.async_fire.assert_not_called()
    router.process.assert_not_called()


async def test_rate_limited_message_fires_no_event(monkeypatch):
    router = MagicMock()
    client = _event_client(router, monkeypatch)
    router.check_rate_limit.return_value = RateLimitDecision(False, False)
    await client.on_message(_message("<@999> spam", mentions=[FakeUser(id=999)]))
    client.hass.bus.async_fire.assert_not_called()


async def test_unmapped_user_fires_no_event(monkeypatch):
    """should_respond passing is not enough: no HA identity, no event."""
    router = MagicMock()
    router.ha_user_for.return_value = None
    client = _event_client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    client.hass.bus.async_fire.assert_not_called()


async def test_event_exclude_text_option_omits_text(monkeypatch):
    router = MagicMock()
    client = _event_client(router, monkeypatch, exclude_text=True)
    await client.on_message(_message("secret words", is_dm=True))
    client.hass.bus.async_fire.assert_called_once()
    data = client.hass.bus.async_fire.call_args.args[1]
    assert "text" not in data
    assert data["ha_user_id"] == "ha-a"
    assert data["is_dm"] is True
    assert data["channel_name"] is None


async def test_accepted_message_fires_event_on_real_bus(hass, monkeypatch):
    """End to end on a real HA bus: one event, carrying the HA identity."""
    from datetime import timedelta

    from custom_components.discord_assist.const import EVENT_MESSAGE
    from custom_components.discord_assist.conversation_cache import ConversationCache
    from custom_components.discord_assist.conversation_router import (
        ConversationRouter,
    )

    router = ConversationRouter(
        hass=hass,
        agent_id="conversation.ollama",
        language=None,
        channels={111},
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={7: "ha-a"},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        speaker_context=False,
        config_entry_id="entry-real",
    )
    router.process = AsyncMock(return_value="hi")
    events = []
    hass.bus.async_listen(EVENT_MESSAGE, lambda event: events.append(event))
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass, router=router, status_signal="test_signal"
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    await client.on_message(_message("movie time"))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].event_type == EVENT_MESSAGE
    assert events[0].data["text"] == "movie time"
    assert events[0].data["config_entry_id"] == "entry-real"
    assert events[0].context.user_id == "ha-a"
