import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from custom_components.discord_assist.bot import (
    ERROR_REPLY,
    PLACEHOLDER_REPLY,
    TIMEOUT_REPLY,
    DiscordAssistClient,
)
from custom_components.discord_assist.const import DEFAULT_PLACEHOLDER_DELAY
from custom_components.discord_assist.conversation_router import AgentTimeout
from custom_components.discord_assist.text_utils import chunk_message


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
        channel = MagicMock(spec=discord.DMChannel)
    elif parent_id is not None:
        channel = MagicMock(spec=discord.Thread)
        channel.parent_id = parent_id
    else:
        channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
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


def _client(router, monkeypatch, *, placeholder_delay=None):
    kwargs = {}
    if placeholder_delay is not None:
        kwargs["placeholder_delay"] = placeholder_delay
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=router, status_signal="test_signal", **kwargs
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


def _placeholder():
    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    return placeholder


def _assert_no_mentions(call):
    mentions = call.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


def test_placeholder_delay_defaults_to_three_seconds(monkeypatch):
    client = _client(MagicMock(), monkeypatch)
    assert DEFAULT_PLACEHOLDER_DELAY == 3.0
    assert client.placeholder_delay == 3.0


async def test_fast_answer_never_shows_placeholder(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="Quick answer.")
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> quick", mentions=[SimpleNamespace(id=999)])
    await client.on_message(msg)
    # Exactly one send: the answer itself, as a reply. No placeholder.
    msg.channel.send.assert_awaited_once_with(
        "Quick answer.", reference=msg.ref, mention_author=False
    )


async def test_slow_answer_posts_one_placeholder_and_edits_it(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_process(**kwargs):
        await asyncio.sleep(0.2)
        return "Slow answer."

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> slow", mentions=[SimpleNamespace(id=999)])
    placeholder = _placeholder()
    msg.channel.send = AsyncMock(return_value=placeholder)
    await client.on_message(msg)
    router.process.assert_awaited_once()
    # One send only: the placeholder, posted as a reply.
    msg.channel.send.assert_awaited_once_with(
        PLACEHOLDER_REPLY, reference=msg.ref, mention_author=False
    )
    # The answer replaces the placeholder in place, with no mentions.
    placeholder.edit.assert_awaited_once()
    edit_call = placeholder.edit.await_args
    assert edit_call.kwargs["content"] == "Slow answer."
    _assert_no_mentions(edit_call)


async def test_slow_long_answer_edits_first_chunk_and_sends_rest(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    answer = "word\n" * 1000

    async def slow_process(**kwargs):
        await asyncio.sleep(0.2)
        return answer

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> essay", mentions=[SimpleNamespace(id=999)])
    placeholder = _placeholder()
    sent = []

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))
        return placeholder if len(sent) == 1 else MagicMock()

    msg.channel.send = AsyncMock(side_effect=fake_send)
    await client.on_message(msg)
    chunks = chunk_message(answer)
    assert len(chunks) > 1
    # First send is the placeholder; the rest are the remaining chunks.
    assert sent[0][0] == (PLACEHOLDER_REPLY,)
    assert [call[0][0] for call in sent[1:]] == chunks[1:]
    assert all(call[1] == {} for call in sent[1:])
    placeholder.edit.assert_awaited_once()
    edit_call = placeholder.edit.await_args
    assert edit_call.kwargs["content"] == chunks[0]
    _assert_no_mentions(edit_call)


async def test_slow_timeout_edits_placeholder(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_timeout(**kwargs):
        await asyncio.sleep(0.2)
        raise AgentTimeout

    router.process = AsyncMock(side_effect=slow_timeout)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> slow", mentions=[SimpleNamespace(id=999)])
    placeholder = _placeholder()
    msg.channel.send = AsyncMock(return_value=placeholder)
    await client.on_message(msg)
    msg.channel.send.assert_awaited_once_with(
        PLACEHOLDER_REPLY, reference=msg.ref, mention_author=False
    )
    placeholder.edit.assert_awaited_once()
    edit_call = placeholder.edit.await_args
    assert edit_call.kwargs["content"] == TIMEOUT_REPLY
    _assert_no_mentions(edit_call)


async def test_slow_error_edits_placeholder(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_error(**kwargs):
        await asyncio.sleep(0.2)
        raise RuntimeError("boom")

    router.process = AsyncMock(side_effect=slow_error)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> slow", mentions=[SimpleNamespace(id=999)])
    placeholder = _placeholder()
    msg.channel.send = AsyncMock(return_value=placeholder)
    await client.on_message(msg)
    placeholder.edit.assert_awaited_once()
    edit_call = placeholder.edit.await_args
    assert edit_call.kwargs["content"] == ERROR_REPLY
    _assert_no_mentions(edit_call)


async def test_fast_timeout_and_error_show_no_placeholder(monkeypatch):
    for side_effect, expected in [
        (AgentTimeout(), TIMEOUT_REPLY),
        (RuntimeError("boom"), ERROR_REPLY),
    ]:
        router = MagicMock()
        router.should_respond.return_value = True
        router.process = AsyncMock(side_effect=side_effect)
        client = _client(router, monkeypatch, placeholder_delay=0.05)
        msg = _message("<@999> hi", mentions=[SimpleNamespace(id=999)])
        await client.on_message(msg)
        msg.channel.send.assert_awaited_once_with(
            expected, reference=msg.ref, mention_author=False
        )


async def test_placeholder_edit_failure_falls_back_to_send(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_process(**kwargs):
        await asyncio.sleep(0.2)
        return "Recovered answer."

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> slow", mentions=[SimpleNamespace(id=999)])
    placeholder = _placeholder()
    placeholder.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "gone"))
    msg.channel.send = AsyncMock(return_value=placeholder)
    await client.on_message(msg)
    placeholder.edit.assert_awaited_once()
    # Placeholder send + fallback send of the answer as a reply.
    assert msg.channel.send.await_count == 2
    assert msg.channel.send.await_args_list[1].args[0] == "Recovered answer."


async def test_placeholder_send_failure_falls_back_to_send(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True

    async def slow_process(**kwargs):
        await asyncio.sleep(0.2)
        return "Answer despite no placeholder."

    router.process = AsyncMock(side_effect=slow_process)
    client = _client(router, monkeypatch, placeholder_delay=0.05)
    msg = _message("<@999> slow", mentions=[SimpleNamespace(id=999)])
    msg.channel.send = AsyncMock(
        side_effect=[
            discord.HTTPException(MagicMock(), "missing permissions"),
            MagicMock(),
        ]
    )
    await client.on_message(msg)
    assert msg.channel.send.await_count == 2
    assert msg.channel.send.await_args_list[1].args[0] == (
        "Answer despite no placeholder."
    )
