"""Button records re-read entity state on press before firing (issue #30)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.buttons import (
    ButtonStore,
    async_handle_button_press,
    capture_expectations,
    custom_id_for,
    find_state_changes,
    make_record,
    record_id_from_custom_id,
)
from custom_components.discord_assist.const import EVENT_BUTTON_ACTION
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import ConversationRouter

ENTRY_ID = "entry-1"
HA_USER = "ha-user-1"
GARAGE = "cover.garage_door"


def _router(hass: HomeAssistant) -> ConversationRouter:
    return ConversationRouter(
        hass=hass,
        agent_id=None,
        language=None,
        channels=set(),
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={7: HA_USER},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
    )


def _interaction(record_id: str, *, presser_id: int = 7) -> SimpleNamespace:
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        defer=AsyncMock(),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id_for(record_id)},
        user=SimpleNamespace(id=presser_id, display_name="Catduck", name="catduck"),
        message=SimpleNamespace(id=555, edit=AsyncMock()),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def _posted_button(hass: HomeAssistant, store: ButtonStore, entity_ids):
    """Post a button record against the entities' current state."""
    record = make_record(
        hass,
        action="close_garage",
        config_entry_id=ENTRY_ID,
        entity_ids=entity_ids,
        channel_id=111,
        message_id=555,
    )
    await store.async_add(record)
    return record


@pytest.fixture
async def store(hass: HomeAssistant) -> ButtonStore:
    button_store = ButtonStore(hass, ENTRY_ID)
    await button_store.async_load()
    return button_store


def test_custom_id_round_trip():
    assert record_id_from_custom_id(custom_id_for("abc123")) == "abc123"
    assert record_id_from_custom_id("da:other:abc123") is None
    assert record_id_from_custom_id("da:act:") is None
    assert record_id_from_custom_id(None) is None


async def test_capture_and_find_changes(hass: HomeAssistant):
    hass.states.async_set(GARAGE, "open")
    (expectation,) = capture_expectations(hass, [GARAGE])
    assert expectation.state == "open"
    assert expectation.last_changed is not None

    record = make_record(
        hass, action="a", config_entry_id=ENTRY_ID, entity_ids=[GARAGE]
    )
    assert find_state_changes(hass, record) == []

    hass.states.async_set(GARAGE, "closed")
    changes = find_state_changes(hass, record)
    assert len(changes) == 1
    assert GARAGE in changes[0]
    assert "closed" in changes[0]


async def test_press_with_unchanged_state_fires_event(
    hass: HomeAssistant, store: ButtonStore
):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    event = events[0]
    assert event.data["action"] == "close_garage"
    assert event.data["message_id"] == 555
    assert event.data["channel_id"] == 111
    assert event.data["discord_user_id"] == 7
    assert event.data["discord_user_name"] == "Catduck"
    assert event.data["ha_user_id"] == HA_USER
    assert event.data["config_entry_id"] == ENTRY_ID
    assert event.context.user_id == HA_USER
    # Acknowledged before firing, buttons retired, record consumed.
    interaction.response.defer.assert_awaited_once()
    interaction.message.edit.assert_awaited_once_with(view=None)
    assert store.get(record.record_id) is None


async def test_second_press_fires_nothing(hass: HomeAssistant, store: ButtonStore):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)
    router = _router(hass)

    await async_handle_button_press(
        hass, store=store, router=router, interaction=_interaction(record.record_id)
    )
    second = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=router, interaction=second
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    second.response.send_message.assert_awaited_once()
    text = second.response.send_message.await_args.args[0]
    assert "already been used" in text
    assert second.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_press_with_changed_state_refuses_and_retires(
    hass: HomeAssistant, store: ButtonStore
):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    hass.states.async_set(GARAGE, "closed")
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    # No event on refusal, pinned by event capture.
    assert events == []
    interaction.response.send_message.assert_awaited_once()
    text = interaction.response.send_message.await_args.args[0]
    assert text.startswith("Nothing changed")
    assert GARAGE in text
    assert "closed" in text
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    # Buttons retired and the record consumed even though nothing ran.
    interaction.message.edit.assert_awaited_once_with(view=None)
    assert store.get(record.record_id) is None


async def test_press_with_changed_last_changed_refuses(
    hass: HomeAssistant, store: ButtonStore
):
    """Same state string, but the entity changed again since posting."""
    hass.states.async_set(GARAGE, "closed")
    record = await _posted_button(hass, store, [GARAGE])
    hass.states.async_set(GARAGE, "open")
    hass.states.async_set(GARAGE, "closed")
    assert hass.states.get(GARAGE).state == "closed"
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    assert events == []
    text = interaction.response.send_message.await_args.args[0]
    assert text.startswith("Nothing changed")
    assert GARAGE in text
    interaction.message.edit.assert_awaited_once_with(view=None)


async def test_press_with_missing_entity_refuses(
    hass: HomeAssistant, store: ButtonStore
):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    hass.states.async_remove(GARAGE)
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    assert events == []
    text = interaction.response.send_message.await_args.args[0]
    assert text.startswith("Nothing changed")
    assert "no longer exists" in text
    interaction.message.edit.assert_awaited_once_with(view=None)
    assert store.get(record.record_id) is None


async def test_unmapped_presser_fires_nothing(hass: HomeAssistant, store: ButtonStore):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id, presser_id=999)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    assert events == []
    text = interaction.response.send_message.await_args.args[0]
    assert "not authorized" in text
    assert store.get(record.record_id) is None


async def test_expired_record_press_is_already_handled(
    hass: HomeAssistant, store: ButtonStore
):
    hass.states.async_set(GARAGE, "open")
    record = make_record(
        hass,
        action="close_garage",
        config_entry_id=ENTRY_ID,
        entity_ids=[GARAGE],
        ttl=timedelta(seconds=-1),
    )
    await store.async_add(record)
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    interaction = _interaction(record.record_id)
    await async_handle_button_press(
        hass, store=store, router=_router(hass), interaction=interaction
    )
    await hass.async_block_till_done()

    assert events == []
    text = interaction.response.send_message.await_args.args[0]
    assert "already been used" in text


async def test_store_persists_records_across_instances(hass: HomeAssistant):
    hass.states.async_set(GARAGE, "open")
    first = ButtonStore(hass, ENTRY_ID)
    await first.async_load()
    record = make_record(
        hass, action="close_garage", config_entry_id=ENTRY_ID, entity_ids=[GARAGE]
    )
    await first.async_add(record)

    second = ButtonStore(hass, ENTRY_ID)
    await second.async_load()
    loaded = second.get(record.record_id)
    assert loaded is not None
    assert loaded.action == "close_garage"
    assert loaded.expectations[0].entity_id == GARAGE
    assert loaded.expectations[0].state == "open"

    taken = await second.async_take(record.record_id)
    assert taken is not None
    assert await second.async_take(record.record_id) is None


async def test_client_on_interaction_dispatches_press(
    hass: HomeAssistant, store: ButtonStore, monkeypatch
):
    hass.states.async_set(GARAGE, "open")
    record = await _posted_button(hass, store, [GARAGE])
    events = async_capture_events(hass, EVENT_BUTTON_ACTION)

    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass,
            router=_router(hass),
            status_signal="test_signal",
            button_store=store,
        )
    await client.on_interaction(_interaction(record.record_id))
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_client_on_interaction_ignores_other_components(
    hass: HomeAssistant, store: ButtonStore
):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass,
            router=_router(hass),
            status_signal="test_signal",
            button_store=store,
        )
    interaction = _interaction("whatever")
    interaction.data = {"custom_id": "some_other:component"}
    await client.on_interaction(interaction)
    interaction.response.send_message.assert_not_called()
    interaction.response.defer.assert_not_called()


def test_record_serialization_round_trip(hass: HomeAssistant):
    hass.states.async_set(GARAGE, "open")
    record = make_record(
        hass,
        action="close_garage",
        config_entry_id=ENTRY_ID,
        entity_ids=[GARAGE],
        channel_id=111,
        message_id=555,
        data={"origin": "test"},
    )
    restored = type(record).from_dict(record.to_dict())
    assert restored == record
    assert dt_util.parse_datetime(restored.expires_at) is not None
