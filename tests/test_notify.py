"""Tests for the per-channel notify entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    signal_connection,
)
from custom_components.discord_assist.notify import (
    DiscordChannelNotifyEntity,
    async_setup_entry,
)

CLIENT = "custom_components.discord_assist.DiscordAssistClient"


class _StubChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.send = AsyncMock()


class _StubClient:
    def __init__(self, channels=(), *, connected=True):
        self.connected = connected
        self.guilds = []
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


def _entry(client, channels):
    router = SimpleNamespace(channels=set(channels))
    return SimpleNamespace(
        runtime_data=SimpleNamespace(client=client, router=router),
        entry_id="entry-1",
        unique_id="botid-1",
        title="Mock Title",
    )


def _capture_entities():
    captured = []
    return captured, captured.extend


async def test_entity_per_configured_channel():
    general = _StubChannel(111, "general")
    alerts = _StubChannel(222, "alerts")
    client = _StubClient([general, alerts])
    captured, add = _capture_entities()

    await async_setup_entry(MagicMock(), _entry(client, {222, 111}), add)

    assert [entity.channel_id for entity in captured] == [111, 222]
    assert [entity._attr_name for entity in captured] == ["general", "alerts"]
    assert [entity._attr_unique_id for entity in captured] == [
        "botid-1_notify_111",
        "botid-1_notify_222",
    ]
    # One device per entry, shared with the gateway sensor.
    for entity in captured:
        assert entity._attr_device_info["identifiers"] == {(DOMAIN, "botid-1")}
        assert entity._attr_has_entity_name is True


async def test_no_channels_no_entities():
    captured, add = _capture_entities()
    await async_setup_entry(MagicMock(), _entry(_StubClient(), set()), add)
    assert captured == []


async def test_channel_name_falls_back_to_id_when_unknown():
    client = _StubClient()  # cache is empty, as before the gateway connects
    captured, add = _capture_entities()
    await async_setup_entry(MagicMock(), _entry(client, {999}), add)
    assert captured[0]._attr_name == "999"


async def test_channel_name_from_guild_cache():
    channel = _StubChannel(555, "from-guild")
    client = _StubClient()
    client.guilds = [SimpleNamespace(channels=[channel], get_channel=lambda cid: None)]
    entity = DiscordChannelNotifyEntity(_entry(client, {555}), 555)
    assert entity._attr_name == "from-guild"


async def test_send_message_posts_with_title_line():
    channel = _StubChannel(111, "general")
    entity = DiscordChannelNotifyEntity(_entry(_StubClient([channel]), {111}), 111)

    await entity.async_send_message("Dinner is ready", title="Kitchen")

    channel.send.assert_awaited_once()
    call = channel.send.await_args
    assert call.args == ("**Kitchen**\nDinner is ready",)
    mentions = call.kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


async def test_send_message_without_title_has_no_bold_line():
    channel = _StubChannel(111, "general")
    entity = DiscordChannelNotifyEntity(_entry(_StubClient([channel]), {111}), 111)

    await entity.async_send_message("Plain message")

    channel.send.assert_awaited_once()
    call = channel.send.await_args
    assert call.args == ("Plain message",)
    # AllowedMentions has no __eq__, so compare the flags it carries.
    mentions = call.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


async def test_send_message_chunks_long_text():
    channel = _StubChannel(111, "general")
    entity = DiscordChannelNotifyEntity(_entry(_StubClient([channel]), {111}), 111)

    await entity.async_send_message("word\n" * 1000, title="Long")

    calls = channel.send.await_args_list
    assert len(calls) > 1
    assert calls[0].args[0].startswith("**Long**\n")
    assert all(len(call.args[0]) <= 2000 for call in calls)
    assert all(call.kwargs["allowed_mentions"].everyone is False for call in calls)


async def test_send_message_unknown_channel_raises_and_logs(caplog):
    entity = DiscordChannelNotifyEntity(_entry(_StubClient(), {4242}), 4242)

    with pytest.raises(HomeAssistantError, match="4242"):
        await entity.async_send_message("hello")
    assert "4242" in caplog.text


async def test_send_message_http_failure_raises():
    channel = _StubChannel(111, "general")
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "missing access")
    )
    entity = DiscordChannelNotifyEntity(_entry(_StubClient([channel]), {111}), 111)

    with pytest.raises(HomeAssistantError, match="111"):
        await entity.async_send_message("hello")


async def test_available_follows_connection():
    client = _StubClient(connected=False)
    entity = DiscordChannelNotifyEntity(_entry(client, {111}), 111)
    assert entity.available is False

    client.connected = True
    assert entity.available is True


# --- Integration: entities through a real config entry setup ---


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so setup runs the real entry path."""
    yield


def _ha_entry(channels):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {"7": "ha-a"},
            CONF_CHANNELS: [str(channel) for channel in channels],
        },
    )


def _fake_client(**overrides):
    client = MagicMock()
    client.login = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=True)
    client.connected = False
    # Empty channel cache: entity names fall back to the channel id.
    client.get_channel = MagicMock(return_value=None)
    client.guilds = []
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


async def test_setup_creates_notify_entity_per_channel(hass: HomeAssistant):
    entry = _ha_entry([111, 222])
    entry.add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    assert hass.states.get("notify.mock_title_111") is not None
    assert hass.states.get("notify.mock_title_222") is not None


async def test_notify_entity_unavailable_while_disconnected(hass: HomeAssistant):
    entry = _ha_entry([111])
    entry.add_to_hass(hass)
    fake_client = _fake_client()

    with patch(CLIENT, return_value=fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        state = hass.states.get("notify.mock_title_111")
        assert state is not None
        assert state.state == "unavailable"

        fake_client.connected = True
        async_dispatcher_send(hass, signal_connection(entry.entry_id))
        await hass.async_block_till_done()
        assert hass.states.get("notify.mock_title_111").state != "unavailable"


async def test_options_change_adds_and_removes_entities(hass: HomeAssistant):
    entry = _ha_entry([111, 222])
    entry.add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        assert hass.states.get("notify.mock_title_222") is not None

        # The options flow reloads the entry on save (OptionsFlowWithReload);
        # mirror that here: update the options, then reload.
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_AGENT_ID: "conversation.ollama",
                CONF_USER_MAP: {"7": "ha-a"},
                CONF_CHANNELS: ["111", "333"],
            },
        )
        assert await hass.config_entries.async_reload(entry.entry_id) is True
        await hass.async_block_till_done()

    assert hass.states.get("notify.mock_title_111") is not None
    assert hass.states.get("notify.mock_title_333") is not None
    # The 222 entity is gone from the platform. NotifyEntity is a
    # RestoreEntity, so its last state lingers as a restored placeholder
    # rather than disappearing from the state machine entirely.
    from homeassistant.components.notify import DATA_COMPONENT

    live_ids = {entity.entity_id for entity in hass.data[DATA_COMPONENT].entities}
    assert "notify.mock_title_111" in live_ids
    assert "notify.mock_title_333" in live_ids
    assert "notify.mock_title_222" not in live_ids
    removed = hass.states.get("notify.mock_title_222")
    assert removed is None or removed.attributes.get("restored") is True


async def test_notify_service_posts_to_channel(hass: HomeAssistant):
    entry = _ha_entry([111])
    entry.add_to_hass(hass)
    channel = _StubChannel(111, "general")
    fake_client = _fake_client(
        connected=True, get_channel=MagicMock(return_value=channel)
    )

    with patch(CLIENT, return_value=fake_client):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        await hass.services.async_call(
            "notify",
            "send_message",
            {"message": "Hello from an automation", "title": "Alert"},
            target={"entity_id": "notify.mock_title_general"},
            blocking=True,
        )

    channel.send.assert_awaited_once()
    call = channel.send.await_args
    assert call.args == ("**Alert**\nHello from an automation",)
    assert call.kwargs["allowed_mentions"].everyone is False
