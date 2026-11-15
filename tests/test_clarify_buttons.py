"""Clarify buttons: detailed router results, button store, press dispatcher."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.buttons import (
    ButtonStore,
    make_custom_id,
    needs_clarify_buttons,
    parse_custom_id,
)
from custom_components.discord_assist.const import CONF_CLARIFY_BUTTONS_HEURISTIC
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import ConversationRouter

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"
QUESTION = "Turn off all the lights, including the outdoor ones?"


def _result(speech, *, continue_conversation=False, conversation_id="conv-1"):
    return SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": speech}}),
        conversation_id=conversation_id,
        continue_conversation=continue_conversation,
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
        user_map={7: "ha-a"},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        speaker_context=False,
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


# --- Router: process_detailed ---------------------------------------------


async def test_process_detailed_exposes_continue_conversation():
    router = _router()
    fake = _result(QUESTION, continue_conversation=True)
    with patch(_CONVERSE, new=AsyncMock(return_value=fake)):
        detailed = await router.process_detailed(
            text="turn off the lights",
            discord_user_id=7,
            conversation_key="channel:111",
        )
    assert detailed.speech == QUESTION
    assert detailed.continue_conversation is True
    assert detailed.conversation_id == "conv-1"


async def test_process_still_returns_speech_only():
    router = _router()
    fake = _result(QUESTION, continue_conversation=True)
    with patch(_CONVERSE, new=AsyncMock(return_value=fake)):
        reply = await router.process(
            text="hi", discord_user_id=7, conversation_key="channel:111"
        )
    assert reply == QUESTION


async def test_process_detailed_defaults_continue_to_false():
    """Results without the attribute (older agents) mean no follow-up."""
    router = _router()
    fake = SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": "Done."}}),
        conversation_id="conv-9",
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=fake)):
        detailed = await router.process_detailed(
            text="hi", discord_user_id=7, conversation_key="channel:111"
        )
    assert detailed.continue_conversation is False


def test_heuristic_option_off_by_default_and_read_from_entry():
    assert _router().clarify_buttons_heuristic is False
    entry = SimpleNamespace(options={CONF_CLARIFY_BUTTONS_HEURISTIC: True})
    assert ConversationRouter.from_entry(object(), entry).clarify_buttons_heuristic
    entry_off = SimpleNamespace(options={})
    assert not ConversationRouter.from_entry(
        object(), entry_off
    ).clarify_buttons_heuristic


def test_needs_clarify_buttons_signal_and_heuristic():
    assert needs_clarify_buttons("Anything?", True) is True
    assert needs_clarify_buttons("Done.", True) is True
    assert needs_clarify_buttons("A question?", False) is False
    assert needs_clarify_buttons("A question?", False, heuristic=True) is True
    assert needs_clarify_buttons("Not a question.", False, heuristic=True) is False


# --- Button store ----------------------------------------------------------


def test_custom_id_round_trip_and_limits():
    assert parse_custom_id(make_custom_id("answer", "abc123")) == ("answer", "abc123")
    assert parse_custom_id("other:answer:abc123") is None
    assert parse_custom_id("da:answer:") is None
    assert parse_custom_id(None) is None
    assert len(make_custom_id("answer", "x" * 80)) <= 100
    with pytest.raises(ValueError):
        make_custom_id("answer", "x" * 200)


async def test_store_take_is_single_use_and_retires_group_siblings():
    store = ButtonStore()
    group = "g1"
    yes = await store.record(
        action="answer",
        conversation_key="channel:111",
        answer="Yes",
        speaker_id=7,
        channel_id=111,
        group_id=group,
    )
    no = await store.record(
        action="answer",
        conversation_key="channel:111",
        answer="No",
        speaker_id=7,
        channel_id=111,
        group_id=group,
    )
    assert await store.peek(yes.record_id) is not None
    taken = await store.take(yes.record_id)
    assert taken is not None and taken.answer == "Yes"
    assert await store.take(yes.record_id) is None
    # The sibling No button died with the Yes press.
    assert await store.peek(no.record_id) is None
    assert len(store) == 0


async def test_store_expired_records_are_refused():
    now = [1000.0]
    store = ButtonStore(ttl_seconds=10, now=lambda: now[0])
    record = await store.record(
        action="answer", conversation_key="dm:7", answer="Yes", speaker_id=7
    )
    now[0] += 11
    assert await store.peek(record.record_id) is None
    assert await store.take(record.record_id) is None
    assert len(store) == 0


async def test_store_records_survive_a_new_store_over_same_storage():
    class FakeBackend:
        def __init__(self):
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    backend = FakeBackend()
    first = ButtonStore(store=backend)
    record = await first.record(
        action="answer",
        conversation_key="channel:111",
        answer="Yes",
        speaker_id=7,
        channel_id=111,
    )
    # A "restart": a brand new store reading the same persisted data.
    second = ButtonStore(store=backend)
    loaded = await second.peek(record.record_id)
    assert loaded is not None
    assert loaded.answer == "Yes"
    assert loaded.speaker_id == 7
    assert loaded.conversation_key == "channel:111"


async def test_store_invalidate_for_conversation():
    store = ButtonStore()
    await store.record(
        action="answer", conversation_key="channel:111", answer="Yes", speaker_id=7
    )
    await store.record(
        action="answer", conversation_key="channel:222", answer="Yes", speaker_id=7
    )
    removed = await store.invalidate_for_conversation("channel:111")
    assert len(removed) == 1
    assert len(store) == 1


# --- Bot: buttons on replies and the press dispatcher ----------------------


def _message(content, *, channel_id=111):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "general"
    channel.send = AsyncMock(return_value=SimpleNamespace(id=555))
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
        content=content,
        channel=channel,
        author=SimpleNamespace(id=7, bot=False, display_name="Catduck"),
        mentions=[],
    )
    msg.ref = SimpleNamespace(message_id=1)
    msg.to_reference = MagicMock(return_value=msg.ref)
    return msg


def _client(router, monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=router, status_signal="test_signal"
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client


def _interaction(custom_id, user_id, channel):
    interaction = SimpleNamespace(
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id, display_name="Catduck"),
        channel=channel,
        channel_id=channel.id,
        message=SimpleNamespace(edit=AsyncMock()),
    )
    interaction.response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        defer=AsyncMock(),
        send_message=AsyncMock(),
    )
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


async def _ask_question(client, converse_results):
    """Drive one message whose reply asks a clarifying question."""
    msg = _message("turn off the lights")
    with patch(_CONVERSE, new=AsyncMock(side_effect=converse_results)) as converse:
        await client.on_message(msg)
    return msg, converse


def _view_of(msg):
    return msg.channel.send.await_args.kwargs["view"]


def _custom_id_for(view, label):
    for item in view.children:
        if item.label == label:
            return item.custom_id
    raise AssertionError(f"no {label} button")


async def test_question_with_continue_conversation_gets_buttons(monkeypatch):
    client = _client(_router(), monkeypatch)
    msg, _ = await _ask_question(
        client, [_result(QUESTION, continue_conversation=True)]
    )
    view = _view_of(msg)
    assert [item.label for item in view.children] == ["Yes", "No"]
    for item in view.children:
        assert item.custom_id.startswith("da:answer:")
    assert len(client.buttons) == 2
    record = await client.buttons.peek(parse_custom_id(_custom_id_for(view, "Yes"))[1])
    assert record.conversation_key == "channel:111"
    assert record.answer == "Yes"
    assert record.speaker_id == 7
    # The records know which message carries them (for later retirement).
    assert record.message_id == 555


async def test_no_buttons_when_continue_conversation_false(monkeypatch):
    client = _client(_router(), monkeypatch)
    msg, _ = await _ask_question(client, [_result("Done.")])
    assert "view" not in msg.channel.send.await_args.kwargs
    assert len(client.buttons) == 0


async def test_heuristic_buttons_off_by_default_and_on_when_enabled(monkeypatch):
    off_client = _client(_router(), monkeypatch)
    msg, _ = await _ask_question(off_client, [_result(QUESTION)])
    assert "view" not in msg.channel.send.await_args.kwargs

    on_client = _client(_router(clarify_buttons_heuristic=True), monkeypatch)
    msg2, _ = await _ask_question(on_client, [_result(QUESTION)])
    assert [item.label for item in _view_of(msg2).children] == ["Yes", "No"]


async def test_buttons_ride_on_last_chunk_of_long_reply(monkeypatch):
    client = _client(_router(), monkeypatch)
    long_question = ("word\n" * 1000) + "Shall I?"
    msg, _ = await _ask_question(
        client, [_result(long_question, continue_conversation=True)]
    )
    calls = msg.channel.send.await_args_list
    assert len(calls) > 1
    assert all("view" not in c.kwargs for c in calls[:-1])
    assert "view" in calls[-1].kwargs


async def test_press_continues_same_conversation_as_presser(monkeypatch):
    client = _client(_router(), monkeypatch)
    events = []
    calls = []

    async def fake_converse(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return _result(QUESTION, continue_conversation=True)
        events.append("converse")
        return _result("Turned them off.")

    converse = AsyncMock(side_effect=fake_converse)
    msg = _message("turn off the lights")
    with patch(_CONVERSE, new=converse):
        await client.on_message(msg)
        yes_id = _custom_id_for(_view_of(msg), "Yes")
        interaction = _interaction(yes_id, 7, msg.channel)
        interaction.response.defer = AsyncMock(
            side_effect=lambda: events.append("defer")
        )
        await client.on_interaction(interaction)

    # Acknowledged before the agent ran.
    assert events == ["defer", "converse"]
    # The answer ran as the presser, with the cached conversation id, so it
    # is the same HA conversation (and the router's same turn lock key).
    assert len(calls) == 2
    assert calls[1][1] == "Yes"
    assert calls[1][2] == "conv-1"
    assert calls[1][3].user_id == "ha-a"
    # Buttons removed, new reply posted in the channel, records consumed.
    interaction.message.edit.assert_awaited_once_with(view=None)
    sent_texts = [c.args[0] for c in msg.channel.send.await_args_list]
    assert "Turned them off." in sent_texts
    assert len(client.buttons) == 0


async def test_unauthorized_presser_refused_and_record_not_consumed(monkeypatch):
    client = _client(_router(), monkeypatch)
    msg, converse = await _ask_question(
        client, [_result(QUESTION, continue_conversation=True)]
    )
    yes_id = _custom_id_for(_view_of(msg), "Yes")
    interaction = _interaction(yes_id, 8, msg.channel)
    await client.on_interaction(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert "Only the person" in interaction.response.send_message.await_args.args[0]
    interaction.response.defer.assert_not_awaited()
    # The agent never ran for the stranger and the record is still usable.
    assert converse.await_count == 1
    assert len(client.buttons) == 2


async def test_second_press_gets_already_handled(monkeypatch):
    client = _client(_router(), monkeypatch)
    converse = AsyncMock(
        side_effect=[
            _result(QUESTION, continue_conversation=True),
            _result("Turned them off."),
        ]
    )
    msg = _message("turn off the lights")
    with patch(_CONVERSE, new=converse):
        await client.on_message(msg)
        yes_id = _custom_id_for(_view_of(msg), "Yes")
        await client.on_interaction(_interaction(yes_id, 7, msg.channel))
        calls_after_first = converse.await_count

        again = _interaction(yes_id, 7, msg.channel)
        await client.on_interaction(again)
    again.response.send_message.assert_awaited_once()
    assert "already" in again.response.send_message.await_args.args[0]
    assert converse.await_count == calls_after_first


async def test_newer_turn_retires_pending_buttons(monkeypatch):
    client = _client(_router(), monkeypatch)
    converse = AsyncMock(
        side_effect=[
            _result(QUESTION, continue_conversation=True),
            _result("Never mind."),
        ]
    )
    msg = _message("turn off the lights")
    with patch(_CONVERSE, new=converse):
        await client.on_message(msg)
        yes_id = _custom_id_for(_view_of(msg), "Yes")
        assert len(client.buttons) == 2

        await client.on_message(_message("never mind"))
        assert len(client.buttons) == 0

        stale = _interaction(yes_id, 7, msg.channel)
        await client.on_interaction(stale)
    stale.response.send_message.assert_awaited_once()
    assert "already" in stale.response.send_message.await_args.args[0]


async def test_unknown_custom_id_prefix_is_ignored(monkeypatch):
    client = _client(_router(), monkeypatch)
    channel = _message("hi").channel
    interaction = _interaction("zz:answer:abc", 7, channel)
    await client.on_interaction(interaction)
    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
