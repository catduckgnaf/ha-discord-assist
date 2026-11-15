from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from custom_components.discord_assist.bot import TIMEOUT_REPLY, DiscordAssistClient
from custom_components.discord_assist.conversation_router import AgentTimeout


def _message(
    content,
    *,
    author_bot=False,
    is_dm=False,
    mentions=None,
    channel_id=111,
    parent_id=None,
    message_id=4242,
):
    if is_dm:
        channel = MagicMock(spec=discord.DMChannel)
    elif parent_id is not None:
        channel = MagicMock(spec=discord.Thread)
        channel.parent_id = parent_id
    else:
        channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "general"
    channel.send = AsyncMock()
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
        id=message_id,
        content=content,
        channel=channel,
        author=SimpleNamespace(id=7, bot=author_bot, display_name="Catduck"),
        mentions=mentions or [],
    )
    msg.ref = SimpleNamespace(message_id=1)
    msg.to_reference = MagicMock(return_value=msg.ref)
    return msg


def _first_send(msg):
    return msg.channel.send.await_args_list[0]


def _hass():
    return SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))


def _client(router, monkeypatch, **kwargs):
    # A MagicMock router answers every attribute; pin the event-related
    # ones to realistic values unless the test set them explicitly.
    if isinstance(router, MagicMock):
        if isinstance(router.ha_user_for.return_value, MagicMock):
            router.ha_user_for.return_value = "ha-a"
        router.event_exclude_text = False
        router.config_entry_id = "entry-123"
    kwargs.setdefault("config_entry_id", "entry-123")
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
    msg = _message("<@999> is the light on?", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    router.process.assert_awaited_once()
    msg.channel.send.assert_awaited_once_with(
        "The light is on.", reference=msg.ref, mention_author=False
    )
    msg.to_reference.assert_called_once_with(fail_if_not_exists=False)
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
    msg = _message("<@999> hi", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    sent = msg.channel.send.await_args.args[0]
    assert "error" in sent.lower()


async def test_on_message_replies_when_typing_fails(monkeypatch):
    """A typing-indicator failure must not block the real reply (spec §11)."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Lights are off.")
    client = _client(router, monkeypatch)

    msg = _message("<@999> lights?", mentions=[SimpleNamespace(id=999)])
    # Make the typing context manager raise on enter
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "forbidden")
    )
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    msg.channel.typing = MagicMock(return_value=typing_cm)

    await client.on_message(msg)

    router.process.assert_awaited_once()
    msg.channel.send.assert_awaited_once_with(
        "Lights are off.", reference=msg.ref, mention_author=False
    )


async def test_on_message_does_not_raise_when_send_fails(monkeypatch):
    """A send failure must be caught and logged, not propagated."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Reply text.")
    client = _client(router, monkeypatch)

    msg = _message("<@999> hello", mentions=[SimpleNamespace(id=999)])
    msg.channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "missing permissions")
    )

    # Must not raise
    await client.on_message(msg)


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
    msg = _message("<@999> hi", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    assert _first_send(msg).args[0] == TIMEOUT_REPLY


async def test_long_reply_only_first_piece_is_a_reply(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="word\n" * 1000)
    client = _client(router, monkeypatch)
    msg = _message("<@999> essay", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    calls = msg.channel.send.await_args_list
    assert len(calls) > 1
    assert calls[0].kwargs == {"reference": msg.ref, "mention_author": False}
    assert all(c.kwargs == {} for c in calls[1:])


async def test_typing_failure_after_answer_does_not_rerun_agent(monkeypatch):
    """If the typing indicator fails on exit, the agent must not run again."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Unlocked.")
    client = _client(router, monkeypatch)
    msg = _message("<@999> unlock", mentions=[SimpleNamespace(id=999)])
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "gone")
    )
    msg.channel.typing = MagicMock(return_value=typing_cm)
    await client.on_message(msg)
    router.process.assert_awaited_once()
    assert _first_send(msg).args[0] == "Unlocked."


async def test_dm_has_no_channel_name(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    assert router.process.call_args.kwargs["where"].channel_name is None


async def test_accepted_message_fires_event_once_with_fields(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    msg = _message("<@999> movie time", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    client.hass.bus.async_fire.assert_called_once()
    event_type, data = client.hass.bus.async_fire.call_args.args
    context = client.hass.bus.async_fire.call_args.kwargs["context"]
    assert event_type == "discord_assist_message"
    assert data == {
        "text": "movie time",
        "channel_id": 111,
        "channel_name": "general",
        "is_dm": False,
        "discord_user_id": 7,
        "ha_user_id": "ha-a",
        "message_id": 4242,
        "config_entry_id": "entry-123",
    }
    assert context.user_id == "ha-a"
    router.ha_user_for.assert_called_with(7)


async def test_rejected_message_fires_no_event(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = False
    router.process = AsyncMock()
    client = _client(router, monkeypatch)
    await client.on_message(_message("random channel chatter"))
    client.hass.bus.async_fire.assert_not_called()
    router.process.assert_not_called()


async def test_unmapped_user_fires_no_event(monkeypatch):
    """should_respond passing is not enough: no HA identity, no event."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.ha_user_for.return_value = None
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello"))
    client.hass.bus.async_fire.assert_not_called()


async def test_event_exclude_text_option_omits_text(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch, event_exclude_text=True)
    await client.on_message(_message("secret words"))
    client.hass.bus.async_fire.assert_called_once()
    data = client.hass.bus.async_fire.call_args.args[1]
    assert "text" not in data
    assert data["ha_user_id"] == "ha-a"
    assert data["channel_id"] == 111


async def test_dm_event_has_no_channel_name(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    data = client.hass.bus.async_fire.call_args.args[1]
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
    assert client.config_entry_id == "entry-real"
    await client.on_message(_message("movie time"))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].event_type == EVENT_MESSAGE
    assert events[0].data["text"] == "movie time"
    assert events[0].data["config_entry_id"] == "entry-real"
    assert events[0].context.user_id == "ha-a"


def test_client_falls_back_to_router_config_entry_id(monkeypatch):
    router = MagicMock()
    router.config_entry_id = "entry-from-router"
    router.event_exclude_text = True
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=_hass(), router=router, status_signal="test_signal"
        )
    assert client.config_entry_id == "entry-from-router"
    assert client.event_exclude_text is True
