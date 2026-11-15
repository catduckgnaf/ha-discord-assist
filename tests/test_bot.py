from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from custom_components.discord_assist.bot import TIMEOUT_REPLY, DiscordAssistClient
from custom_components.discord_assist.conversation_router import AgentTimeout
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


def _client(router, monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=router, status_signal="test_signal"
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
    # Make the typing context manager raise on enter
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
    assert all(c.kwargs == {} for c in calls[1:])


async def test_typing_failure_after_answer_does_not_rerun_agent(monkeypatch):
    """If the typing indicator fails on exit, the agent must not run again."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Unlocked.")
    client = _client(router, monkeypatch)
    msg = _message("<@999> unlock", mentions=[FakeUser(id=999)])
    msg.channel.typing_exit_error = discord.HTTPException(MagicMock(), "gone")
    await client.on_message(msg)
    router.process.assert_awaited_once()
    assert _first_send(msg).content == "Unlocked."


async def test_dm_has_no_channel_name(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    assert router.process.call_args.kwargs["where"].channel_name is None
