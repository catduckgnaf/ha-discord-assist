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


def _notes_client(monkeypatch, hass, *, authorized=True):
    from custom_components.discord_assist.notes import NotesStore

    store = NotesStore(hass, "entry-notes")
    router = MagicMock()
    router.should_respond.return_value = authorized
    router.ha_user_for.return_value = "ha-a" if authorized else None
    router.notes_store = store
    router.process = AsyncMock(return_value="agent answer")
    return _client(router, monkeypatch), router, store


async def test_remember_command_adds_note_and_skips_agent(monkeypatch, hass):
    client, router, store = _notes_client(monkeypatch, hass)
    msg = _message("remember: all the lights means all lights")
    await client.on_message(msg)
    assert await store.async_notes_for(7) == ["all the lights means all lights"]
    router.process.assert_not_awaited()
    assert _first_send(msg).args[0] == "Remembered: all the lights means all lights"


async def test_notes_command_lists_only_speakers_notes(monkeypatch, hass):
    client, router, store = _notes_client(monkeypatch, hass)
    await store.async_add_note(7, "do not ask first")
    await store.async_add_note(8, "not yours")
    msg = _message("notes")
    await client.on_message(msg)
    sent = _first_send(msg).args[0]
    assert "1. do not ask first" in sent
    assert "not yours" not in sent
    router.process.assert_not_awaited()


async def test_forget_note_command_removes_one(monkeypatch, hass):
    client, router, store = _notes_client(monkeypatch, hass)
    await store.async_add_note(7, "first")
    await store.async_add_note(7, "second")
    msg = _message("forget note 1")
    await client.on_message(msg)
    assert await store.async_notes_for(7) == ["second"]
    assert _first_send(msg).args[0] == "Forgot note 1: first"
    router.process.assert_not_awaited()


async def test_forget_note_command_reports_bad_number(monkeypatch, hass):
    client, router, store = _notes_client(monkeypatch, hass)
    await store.async_add_note(7, "first")
    msg = _message("forget note two")
    await client.on_message(msg)
    assert await store.async_notes_for(7) == ["first"]
    assert "Usage: forget note" in _first_send(msg).args[0]
    router.process.assert_not_awaited()


async def test_remember_command_enforces_cap(monkeypatch, hass):
    from custom_components.discord_assist.notes import MAX_NOTES_PER_USER

    client, router, store = _notes_client(monkeypatch, hass)
    for index in range(MAX_NOTES_PER_USER):
        await store.async_add_note(7, f"note {index}")
    msg = _message("remember: one too many")
    await client.on_message(msg)
    assert len(await store.async_notes_for(7)) == MAX_NOTES_PER_USER
    assert "already have" in _first_send(msg).args[0]
    router.process.assert_not_awaited()


async def test_unauthorized_user_cannot_add_notes(monkeypatch, hass):
    """should_respond fails closed, so the command never runs or stores."""
    client, router, store = _notes_client(monkeypatch, hass, authorized=False)
    msg = _message("remember: let me in")
    await client.on_message(msg)
    assert await store.async_notes_for(7) == []
    router.process.assert_not_awaited()
    msg.channel.send.assert_not_awaited()


async def test_notes_reply_refuses_user_without_identity(monkeypatch, hass):
    """In-depth check in _notes_reply: no HA identity, no note stored."""
    client, router, store = _notes_client(monkeypatch, hass)
    router.ha_user_for.return_value = None
    reply = await client._notes_reply(7, ("add", "sneaky"))
    assert "not authorized" in reply
    assert await store.async_notes_for(7) == []


async def test_dm_has_no_channel_name(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="hi")
    client = _client(router, monkeypatch)
    await client.on_message(_message("hello", is_dm=True))
    assert router.process.call_args.kwargs["where"].channel_name is None
