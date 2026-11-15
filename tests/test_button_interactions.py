from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from custom_components.discord_assist.bot import (
    ALREADY_HANDLED_REPLY,
    NOT_ALLOWED_REPLY,
    DiscordAssistClient,
)
from custom_components.discord_assist.buttons import (
    ButtonOutcome,
    ButtonSpec,
    ButtonStore,
    async_send_with_buttons,
)
from tests.test_buttons import Clock, FakeStore


class FakeResponse:
    def __init__(self):
        self._done = False
        self.defer = AsyncMock(side_effect=self._defer)
        self.send_message = AsyncMock(side_effect=self._send)

    async def _defer(self, **kwargs):
        self._done = True

    async def _send(self, *args, **kwargs):
        self._done = True

    def is_done(self):
        return self._done


def _interaction(custom_id, user_id=7):
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id} if custom_id is not None else {},
        user=SimpleNamespace(id=user_id),
        response=FakeResponse(),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )


def _followup_texts(interaction):
    return [
        call.args[0] if call.args else call.kwargs.get("text", "")
        for call in interaction.followup.send.await_args_list
    ]


def _client(monkeypatch, *, store, router, handlers=None):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(),
            router=router,
            status_signal="test_signal",
            button_store=store,
            button_handlers=handlers,
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client


def _router(ha_user="ha-a"):
    router = MagicMock()
    router.ha_user_for.return_value = ha_user
    return router


async def _recorded_store(**kwargs):
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=Clock())
    record = await store.record("confirm", **kwargs)
    return store, record


async def test_foreign_prefix_is_ignored(monkeypatch):
    store, record = await _recorded_store()
    handler = AsyncMock(return_value="Done.")
    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    interaction = _interaction(f"other:confirm:{record.record_id}")
    await client.on_interaction(interaction)
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    handler.assert_not_awaited()
    assert await store.peek(record.record_id) is not None


async def test_interaction_without_custom_id_is_ignored(monkeypatch):
    store, _record = await _recorded_store()
    client = _client(monkeypatch, store=store, router=_router())
    interaction = _interaction(None)
    await client.on_interaction(interaction)
    interaction.response.defer.assert_not_awaited()


async def test_press_runs_handler_once_and_edits_message(monkeypatch):
    store, record = await _recorded_store(allowed_user_ids=(7,))
    seen = []

    async def handler(taken, interaction):
        seen.append(taken.record_id)
        return "Confirmed."

    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    interaction = _interaction(record.custom_id)
    await client.on_interaction(interaction)

    assert seen == [record.record_id]
    interaction.response.defer.assert_awaited_once()
    interaction.message.edit.assert_awaited_once()
    assert interaction.message.edit.await_args.kwargs["content"] == "Confirmed."
    assert interaction.message.edit.await_args.kwargs["view"] is None
    assert _followup_texts(interaction) == ["Confirmed."]
    assert await store.peek(record.record_id) is None


async def test_second_press_gets_already_handled(monkeypatch):
    store, record = await _recorded_store()
    handler = AsyncMock(return_value="Confirmed.")
    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    await client.on_interaction(_interaction(record.custom_id))
    second = _interaction(record.custom_id)
    await client.on_interaction(second)
    handler.assert_awaited_once()
    assert _followup_texts(second) == [ALREADY_HANDLED_REPLY]


async def test_unauthorized_presser_is_refused_and_does_not_consume(monkeypatch):
    store, record = await _recorded_store()
    handler = AsyncMock(return_value="Confirmed.")
    client = _client(
        monkeypatch,
        store=store,
        router=_router(ha_user=None),
        handlers={"confirm": handler},
    )
    interaction = _interaction(record.custom_id, user_id=42)
    await client.on_interaction(interaction)
    handler.assert_not_awaited()
    assert _followup_texts(interaction) == [NOT_ALLOWED_REPLY]
    # The legitimate presser can still use the button afterwards.
    assert await store.peek(record.record_id) is not None


async def test_presser_not_named_on_record_is_refused(monkeypatch):
    store, record = await _recorded_store(allowed_user_ids=(8,))
    handler = AsyncMock(return_value="Confirmed.")
    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    interaction = _interaction(record.custom_id, user_id=7)
    await client.on_interaction(interaction)
    handler.assert_not_awaited()
    assert _followup_texts(interaction) == [NOT_ALLOWED_REPLY]
    assert await store.peek(record.record_id) is not None


async def test_unknown_record_gets_already_handled(monkeypatch):
    store, _record = await _recorded_store()
    client = _client(monkeypatch, store=store, router=_router())
    interaction = _interaction("da:confirm:missing1")
    await client.on_interaction(interaction)
    assert _followup_texts(interaction) == [ALREADY_HANDLED_REPLY]


async def test_press_without_handler_uses_payload_response_text(monkeypatch):
    store, record = await _recorded_store(payload={"response_text": "Lights on."})
    client = _client(monkeypatch, store=store, router=_router())
    interaction = _interaction(record.custom_id)
    await client.on_interaction(interaction)
    assert _followup_texts(interaction) == ["Lights on."]
    assert await store.peek(record.record_id) is None


async def test_outcome_keeps_message_text_and_appends_outcome(monkeypatch):
    store, record = await _recorded_store()

    async def handler(taken, interaction):
        return ButtonOutcome(outcome="Unlocked by Catduck", ephemeral=None)

    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    interaction = _interaction(record.custom_id)
    interaction.message.content = "Front door: someone is here"
    await client.on_interaction(interaction)
    kwargs = interaction.message.edit.await_args.kwargs
    assert kwargs["content"] == "Front door: someone is here\nUnlocked by Catduck"
    assert kwargs["view"] is None
    interaction.followup.send.assert_not_awaited()


async def test_pressing_one_button_claims_its_siblings(monkeypatch):
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=Clock())
    yes = await store.record("confirm", group_id="g1")
    no = await store.record("confirm", group_id="g1")
    other = await store.record("confirm", group_id="g2")
    assert (await store.take(yes.record_id)).record_id == yes.record_id
    assert await store.take(no.record_id) is None
    assert await store.peek(other.record_id) is not None


async def test_multi_use_record_survives_presses(monkeypatch):
    store, record = await _recorded_store(multi_use=True)
    handler = AsyncMock(return_value="Sent.")
    client = _client(
        monkeypatch, store=store, router=_router(), handlers={"confirm": handler}
    )
    first = _interaction(record.custom_id)
    second = _interaction(record.custom_id)
    await client.on_interaction(first)
    await client.on_interaction(second)
    assert handler.await_count == 2
    # A multi-use button keeps its buttons.
    first.message.edit.assert_not_awaited()


async def test_send_with_buttons_binds_and_stops_the_view():
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=Clock())
    sent = []

    async def send(content, **kwargs):
        message = SimpleNamespace(id=5000 + len(sent), content=content, **kwargs)
        sent.append(message)
        return message

    channel = SimpleNamespace(id=111)
    messages = await async_send_with_buttons(
        store,
        channel,
        "word\n" * 1000,
        [ButtonSpec("Yes", "confirm"), ButtonSpec("No", "confirm")],
        send=send,
    )
    assert len(messages) > 1
    assert all(not hasattr(m, "view") for m in messages[:-1])
    view = messages[-1].view
    # Stopped, so discord.py does not keep it in its view store forever.
    assert view.is_finished()
    ids = [item.custom_id for item in view.children]
    records = [await store.peek(cid.split(":")[2]) for cid in ids]
    assert {r.message_id for r in records} == {messages[-1].id}
    assert {r.channel_id for r in records} == {111}
    assert len({r.group_id for r in records}) == 1
