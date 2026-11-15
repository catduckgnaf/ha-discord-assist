"""Clarify buttons: detailed router results, button store, press dispatcher."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.buttons import ButtonStore
from custom_components.discord_assist.clarify import (
    ANSWER_ACTION,
    needs_clarify_buttons,
)
from custom_components.discord_assist.const import CONF_CLARIFY_BUTTONS_HEURISTIC
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    AgentTimeout,
    ConversationRouter,
    DetailedReply,
)
from tests.fake_discord import FakeTextChannel, FakeThread, FakeUser
from tests.test_buttons import Clock, FakeStore

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


# --- Bot: buttons on replies and presses ------------------------------------


def _bot_router(replies, *, heuristic=False):
    """A router double whose process_detailed returns queued replies."""
    router = MagicMock()
    router.should_respond.return_value = True
    router.ha_user_for.side_effect = lambda uid: {7: "ha-a", 8: "ha-b"}.get(uid)
    router.clarify_buttons_heuristic = heuristic

    def conversation_key(*, is_dm, channel_id, author_id, parent_id=None):
        return f"dm:{author_id}" if is_dm else f"channel:{channel_id}"

    router.conversation_key.side_effect = conversation_key
    queue = list(replies)
    router.calls = []

    async def process_detailed(**kwargs):
        router.calls.append(kwargs)
        return queue.pop(0)

    router.process_detailed = process_detailed
    return router


def _bot(router, monkeypatch):
    store = ButtonStore(store=FakeStore(), ttl_seconds=600, now=Clock())
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
            router=router,
            status_signal="sig",
            button_store=store,
            placeholder_delay=5,
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client, store


def _msg(channel, content="<@999> lights off", author_id=7):
    return channel.receive_message(
        content,
        author=FakeUser(id=author_id, display_name="Catduck"),
        mentions=[FakeUser(id=999)],
    )


def _buttons(message):
    view = message.edits[-1].kwargs.get("view") if message.edits else None
    return [] if view is None else list(view.children)


def _press(channel, message, custom_id, user_id=7):
    response = SimpleNamespace(is_done=MagicMock(return_value=True), defer=AsyncMock())
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        user=FakeUser(id=user_id, display_name="Catduck"),
        channel=channel,
        channel_id=channel.id,
        message=message,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _reply(speech, follow=False):
    return DetailedReply(speech=speech, continue_conversation=follow)


async def test_question_with_continue_conversation_gets_buttons(monkeypatch):
    client, store = _bot(_bot_router([_reply(QUESTION, True)]), monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    reply = channel.sent[0]
    assert reply.content == QUESTION
    assert [b.label for b in _buttons(reply)] == ["Yes", "No"]
    assert len(store) == 2


async def test_no_buttons_when_no_follow_up(monkeypatch):
    client, store = _bot(_bot_router([_reply("Done.")]), monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    assert _buttons(channel.sent[0]) == []
    assert len(store) == 0


async def test_heuristic_only_when_enabled(monkeypatch):
    client, store = _bot(_bot_router([_reply("All of them?")]), monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    assert _buttons(channel.sent[0]) == []
    client, store = _bot(
        _bot_router([_reply("All of them?")], heuristic=True), monkeypatch
    )
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    assert len(_buttons(channel.sent[0])) == 2


async def test_timeout_reply_never_gets_buttons(monkeypatch):
    """With the heuristic on, "Try again?" must not offer Yes / No."""
    router = _bot_router([], heuristic=True)

    async def boom(**kwargs):
        raise AgentTimeout

    router.process_detailed = boom
    client, store = _bot(router, monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    assert channel.sent[0].content.endswith("Try again?")
    assert _buttons(channel.sent[0]) == []


async def test_buttons_ride_on_last_chunk_of_long_reply(monkeypatch):
    long_question = "word\n" * 1000 + "?"
    client, store = _bot(_bot_router([_reply(long_question, True)]), monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    assert len(channel.sent) > 1
    assert _buttons(channel.sent[0]) == []
    assert len(_buttons(channel.sent[-1])) == 2


async def test_press_continues_same_conversation(monkeypatch):
    router = _bot_router([_reply(QUESTION, True), _reply("All lights are off.")])
    client, store = _bot(router, monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    question = channel.sent[0]
    yes = _buttons(question)[0]
    await client.on_interaction(_press(channel, question, yes.custom_id))
    second = router.calls[1]
    assert second["text"] == "Yes"
    assert second["discord_user_id"] == 7
    assert second["conversation_key"] == "channel:111"
    assert channel.sent[-1].content == "Yes: All lights are off."
    # The question stays as asked; only its buttons go, and both are spent.
    assert question.edits[-1].kwargs["view"] is None
    assert question.content == QUESTION
    assert len(store) == 0


async def test_press_in_thread_keeps_parent_context(monkeypatch):
    router = _bot_router([_reply(QUESTION, True), _reply("Done.")])
    client, store = _bot(router, monkeypatch)
    thread = FakeThread(id=555, parent_id=111)
    await client.on_message(_msg(thread))
    question = thread.sent[0]
    yes = _buttons(question)[0]
    await client.on_interaction(_press(thread, question, yes.custom_id))
    assert router.calls[1]["where"].parent_id == 111


async def test_other_user_cannot_answer_and_does_not_consume(monkeypatch):
    router = _bot_router([_reply(QUESTION, True)])
    client, store = _bot(router, monkeypatch)
    channel = FakeTextChannel(id=111)
    await client.on_message(_msg(channel))
    question = channel.sent[0]
    yes = _buttons(question)[0]
    await client.on_interaction(_press(channel, question, yes.custom_id, user_id=8))
    assert len(router.calls) == 1
    assert len(store) == 2


async def test_newer_turn_retires_pending_buttons(monkeypatch):
    router = _bot_router([_reply(QUESTION, True), _reply("Okay.")])
    client, store = _bot(router, monkeypatch)
    channel = FakeTextChannel(id=111)
    client.get_channel = MagicMock(return_value=channel)

    async def fetch_message(message_id):
        return next(m for m in channel.sent if m.id == message_id)

    channel.fetch_message = fetch_message
    await client.on_message(_msg(channel))
    question = channel.sent[0]
    await client.on_message(_msg(channel, "<@999> never mind"))
    assert len(store) == 0
    assert question.edits[-1].kwargs["view"] is None


def test_answer_action_name():
    assert ANSWER_ACTION == "answer"
