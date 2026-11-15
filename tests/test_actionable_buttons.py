"""Tests for actionable notification buttons (#24)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.discord_assist.buttons import (
    ActionButtonDispatcher,
    ButtonRecordStore,
    ButtonSpec,
    async_send_action_message,
    make_custom_id,
    parse_custom_id,
)
from custom_components.discord_assist.const import EVENT_ACTION
from custom_components.discord_assist.logbook import async_describe_events

ENTRY_ID = "entry-24-test"
USERS = {7: "ha-andrew", 8: "ha-other"}


def _ha_user_for(discord_user_id):
    return USERS.get(discord_user_id)


def _dispatcher(hass, store):
    return ActionButtonDispatcher(
        hass, store, config_entry_id=ENTRY_ID, ha_user_for=_ha_user_for
    )


def _press(dispatcher, record_id, **overrides):
    kwargs = dict(
        record_id=record_id,
        discord_user_id=7,
        discord_user_name="Andrew",
        message_id=4242,
        channel_id=111,
    )
    kwargs.update(overrides)
    return dispatcher.async_handle_press(**kwargs)


def test_custom_id_round_trip_and_prefix():
    assert parse_custom_id(make_custom_id("abc123")) == "abc123"
    assert make_custom_id("abc123") == "da:act:abc123"
    assert parse_custom_id("other:act:abc123") is None
    assert parse_custom_id("da:act:") is None
    assert parse_custom_id(None) is None
    assert len(make_custom_id("x" * 90)) <= 100


async def test_press_fires_one_event_with_all_fields_and_context(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("unlock_front_door", label="Unlock front door")
    events = async_capture_events(hass, EVENT_ACTION)

    assert await _press(dispatcher, record.record_id) is True
    await hass.async_block_till_done()

    assert len(events) == 1
    event = events[0]
    assert event.data["action"] == "unlock_front_door"
    assert event.data["label"] == "Unlock front door"
    assert event.data["message_id"] == 4242
    assert event.data["channel_id"] == 111
    assert event.data["discord_user_id"] == 7
    assert event.data["discord_user_name"] == "Andrew"
    assert event.data["ha_user_id"] == "ha-andrew"
    assert event.data["config_entry_id"] == ENTRY_ID
    # The press runs as the mapped HA user, so automations can check who pressed.
    assert event.context.user_id == "ha-andrew"


async def test_second_press_fires_nothing_single_use(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("ignore")
    events = async_capture_events(hass, EVENT_ACTION)

    assert await _press(dispatcher, record.record_id) is True
    assert await _press(dispatcher, record.record_id) is False
    await hass.async_block_till_done()

    assert len(events) == 1
    assert await store.async_peek(record.record_id) is None


async def test_multi_use_record_fires_twice(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("snooze", multi_use=True)
    events = async_capture_events(hass, EVENT_ACTION)

    assert await _press(dispatcher, record.record_id) is True
    assert await _press(dispatcher, record.record_id) is True
    await hass.async_block_till_done()

    assert len(events) == 2
    assert await store.async_peek(record.record_id) is not None


async def test_unmapped_presser_refused_and_record_not_claimed(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("unlock_front_door")
    events = async_capture_events(hass, EVENT_ACTION)

    assert (
        await _press(
            dispatcher, record.record_id, discord_user_id=999, discord_user_name="Rando"
        )
        is False
    )
    await hass.async_block_till_done()
    assert events == []
    # The refused press did not burn the single-use button.
    assert await store.async_peek(record.record_id) is not None
    assert await _press(dispatcher, record.record_id) is True


async def test_disallowed_presser_refused_when_record_names_pressers(
    hass: HomeAssistant,
):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record(
        "unlock_front_door", allowed_discord_user_ids=(7,)
    )
    events = async_capture_events(hass, EVENT_ACTION)

    # User 8 is mapped in general, but this record only allows user 7.
    assert await _press(dispatcher, record.record_id, discord_user_id=8) is False
    await hass.async_block_till_done()
    assert events == []
    assert await store.async_peek(record.record_id) is not None
    assert await _press(dispatcher, record.record_id) is True


async def test_record_named_unmapped_presser_fires_with_null_ha_user(
    hass: HomeAssistant,
):
    """A record may explicitly allow a presser who has no HA mapping."""
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("ignore", allowed_discord_user_ids=(999,))
    events = async_capture_events(hass, EVENT_ACTION)

    assert (
        await _press(
            dispatcher, record.record_id, discord_user_id=999, discord_user_name="Guest"
        )
        is True
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["ha_user_id"] is None
    assert events[0].context.user_id is None


async def test_records_survive_a_new_store_instance(hass: HomeAssistant):
    """Records are persisted, so buttons posted before a restart still work."""
    store = ButtonRecordStore(hass, ENTRY_ID)
    record = await store.async_record("unlock_front_door")

    reloaded = ButtonRecordStore(hass, ENTRY_ID)
    peeked = await reloaded.async_peek(record.record_id)
    assert peeked is not None
    assert peeked.action == "unlock_front_door"
    # And a single-use claim through the reloaded store is still single-use.
    assert await reloaded.async_take(record.record_id) is not None
    assert await ButtonRecordStore(hass, ENTRY_ID).async_take(record.record_id) is None


async def test_expired_record_is_refused(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("stale", ttl_seconds=-1)
    events = async_capture_events(hass, EVENT_ACTION)

    assert await _press(dispatcher, record.record_id) is False
    await hass.async_block_till_done()
    assert events == []


def _interaction(custom_id, *, user_id=7):
    response = MagicMock()
    response.is_done = MagicMock(return_value=False)
    response.defer = AsyncMock()
    response.send_message = AsyncMock()
    return SimpleNamespace(
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id, display_name="Andrew", name="andrew"),
        message=SimpleNamespace(id=4242, channel=SimpleNamespace(id=111)),
        channel_id=111,
        response=response,
    )


async def test_handle_interaction_fires_and_defers(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("unlock_front_door")
    events = async_capture_events(hass, EVENT_ACTION)
    interaction = _interaction(make_custom_id(record.record_id))

    assert await dispatcher.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["message_id"] == 4242
    interaction.response.defer.assert_awaited_once()


async def test_handle_interaction_ignores_unknown_prefix(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    events = async_capture_events(hass, EVENT_ACTION)
    interaction = _interaction("someone_else:act:abc")

    assert await dispatcher.async_handle_interaction(interaction) is False
    await hass.async_block_till_done()
    assert events == []
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()


async def test_handle_interaction_refused_press_gets_ephemeral_reply(
    hass: HomeAssistant,
):
    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("unlock_front_door")
    interaction = _interaction(make_custom_id(record.record_id), user_id=999)

    assert await dispatcher.async_handle_interaction(interaction) is False
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_send_helper_posts_view_with_recorded_buttons(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=555))

    sent = await async_send_action_message(
        store,
        channel,
        "Someone is at the front door",
        [
            ButtonSpec(action="unlock_front_door", label="Unlock front door"),
            ButtonSpec(action="ignore", label="Ignore", style="secondary"),
        ],
    )

    assert sent.id == 555
    kwargs = channel.send.await_args.kwargs
    assert channel.send.await_args.args[0] == "Someone is at the front door"
    assert kwargs["allowed_mentions"].everyone is False
    view = kwargs["view"]
    assert isinstance(view, discord.ui.View)
    assert view.timeout is None
    custom_ids = [item.custom_id for item in view.children]
    assert all(parse_custom_id(custom_id) for custom_id in custom_ids)
    # Each posted button has a persisted record behind it.
    for custom_id in custom_ids:
        assert await store.async_peek(parse_custom_id(custom_id)) is not None


async def test_client_on_interaction_dispatches(hass: HomeAssistant, monkeypatch):
    """The gateway client's on_interaction routes da:act: presses."""
    from custom_components.discord_assist.bot import DiscordAssistClient

    store = ButtonRecordStore(hass, ENTRY_ID)
    dispatcher = _dispatcher(hass, store)
    record = await store.async_record("unlock_front_door")
    events = async_capture_events(hass, EVENT_ACTION)

    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass,
            router=MagicMock(),
            status_signal="test_signal",
            button_store=store,
            action_dispatcher=dispatcher,
        )
    interaction = _interaction(make_custom_id(record.record_id))
    await client.on_interaction(interaction)
    await hass.async_block_till_done()
    assert len(events) == 1

    # Unknown prefixes are ignored by the client too.
    other = _interaction("nope:1")
    await client.on_interaction(other)
    await hass.async_block_till_done()
    assert len(events) == 1


def test_logbook_description_renders(hass: HomeAssistant):
    described = {}

    def capture(domain, event_type, describer):
        described[(domain, event_type)] = describer

    async_describe_events(hass, capture)
    describer = described[("discord_assist", EVENT_ACTION)]

    result = describer(
        Event(
            EVENT_ACTION,
            {"discord_user_name": "Andrew", "action": "Unlock front door"},
        )
    )
    assert result[LOGBOOK_ENTRY_NAME] == "Andrew"
    assert result[LOGBOOK_ENTRY_MESSAGE] == "Andrew pressed Unlock front door"

    # A stored label wins over the action id, like the button the user saw.
    labelled = describer(
        Event(
            EVENT_ACTION,
            {
                "discord_user_name": "Andrew",
                "action": "unlock_front_door",
                "label": "Unlock front door",
            },
        )
    )
    assert labelled[LOGBOOK_ENTRY_MESSAGE] == "Andrew pressed Unlock front door"


async def test_record_rejects_overlong_custom_id(hass: HomeAssistant):
    store = ButtonRecordStore(hass, ENTRY_ID)
    with pytest.raises(ValueError):
        await store.async_record("x", record_id="x" * 200)
