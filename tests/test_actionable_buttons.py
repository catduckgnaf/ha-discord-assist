"""Actionable notification buttons (issue #24) on the shared button store."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from homeassistant.core import HomeAssistant

from custom_components.discord_assist.actions import (
    ACTION,
    action_specs,
    async_send_action_message,
    make_action_handler,
)
from custom_components.discord_assist.bot import NOT_ALLOWED_REPLY, DiscordAssistClient
from custom_components.discord_assist.buttons import ButtonStore
from custom_components.discord_assist.const import EVENT_ACTION
from custom_components.discord_assist.logbook import async_describe_events
from tests.test_buttons import Clock, FakeStore


def _router(ha_user="ha-a"):
    router = MagicMock()
    router.ha_user_for.side_effect = lambda uid: ha_user if uid == 7 else None
    return router


def _client(hass, router, store):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass, router=router, status_signal="sig", button_store=store
        )
    client.register_button_handler(ACTION, make_action_handler(hass, client, "entry-1"))

    async def send_to_channel(channel, content, **kwargs):
        return await channel.send(content, **kwargs)

    client.send_to_channel = send_to_channel
    return client


class _Channel:
    id = 111

    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        message = SimpleNamespace(
            id=5000 + len(self.sent), content=content, edit=AsyncMock(), **kwargs
        )
        self.sent.append(message)
        return message


def _press(message, custom_id, user_id=7):
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        defer=AsyncMock(),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id, display_name="Catduck"),
        channel_id=111,
        message=message,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def _posted(hass, buttons, router=None):
    store = ButtonStore(store=FakeStore(), ttl_seconds=600, now=Clock())
    client = _client(hass, router or _router(), store)
    channel = _Channel()
    messages = await async_send_action_message(
        client, channel, "Someone is at the door", buttons
    )
    message = messages[-1]
    ids = [item.custom_id for item in message.view.children]
    return client, store, message, ids


async def test_press_fires_one_event_as_the_presser(hass: HomeAssistant):
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    client, store, message, ids = await _posted(
        hass,
        [
            {"id": "unlock_front_door", "label": "Unlock front door"},
            {"id": "ignore", "label": "Ignore"},
        ],
    )
    await client.on_interaction(_press(message, ids[0]))
    await hass.async_block_till_done()
    assert len(events) == 1
    event = events[0]
    assert event.data == {
        "action": "unlock_front_door",
        "label": "Unlock front door",
        "message_id": str(message.id),
        "channel_id": "111",
        "discord_user_id": "7",
        "discord_user_name": "Catduck",
        "ha_user_id": "ha-a",
        "config_entry_id": "entry-1",
    }
    assert event.context.user_id == "ha-a"
    # Single use: the buttons go, the alert text stays with the outcome.
    edit = message.edit.await_args.kwargs
    assert edit["view"] is None
    assert edit["content"] == (
        "Someone is at the door\nCatduck pressed Unlock front door"
    )
    # The sibling button on the same message is claimed with it.
    await client.on_interaction(_press(message, ids[1]))
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_named_presser_without_identity_is_refused(hass: HomeAssistant):
    """Being named on the record is not enough without an HA identity."""
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    client, store, message, ids = await _posted(
        hass, [{"id": "open", "label": "Open", "allowed_discord_user_ids": [8]}]
    )
    press = _press(message, ids[0], user_id=8)
    await client.on_interaction(press)
    await hass.async_block_till_done()
    assert events == []
    replies = [
        call.args[0]
        for call in press.followup.send.await_args_list
        + press.response.send_message.await_args_list
    ]
    assert replies == [NOT_ALLOWED_REPLY]
    # Refused presses never burn the button.
    assert await store.peek(ids[0].split(":")[2]) is not None


async def test_multi_use_button_keeps_firing(hass: HomeAssistant):
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    client, store, message, ids = await _posted(
        hass, [{"id": "ping", "label": "Ping", "multi_use": True}]
    )
    await client.on_interaction(_press(message, ids[0]))
    await client.on_interaction(_press(message, ids[0]))
    await hass.async_block_till_done()
    assert len(events) == 2
    message.edit.assert_not_awaited()


def test_link_style_is_not_offered():
    (spec,) = action_specs([{"id": "x", "label": "X", "style": "link"}])
    assert spec.style is discord.ButtonStyle.primary


def test_logbook_does_not_repeat_the_name():
    described = {}

    def describe(domain, event_type, fn):
        described[event_type] = fn

    async_describe_events(None, describe)
    entry = described[EVENT_ACTION](
        SimpleNamespace(
            data={"discord_user_name": "Andrew", "label": "Unlock front door"}
        )
    )
    assert entry["name"] == "Andrew"
    assert entry["message"] == "pressed Unlock front door"
