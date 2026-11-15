"""Tests for retiring stale button messages (issue #27)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.button_store import ButtonStore
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    SERVICE_RETIRE_MESSAGE,
)
from custom_components.discord_assist.retire import (
    EXPIRED_OUTCOME,
    async_prune_expired,
    async_retire_taken,
)

CLIENT = "custom_components.discord_assist.DiscordAssistClient"


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]


class FakeChannel:
    def __init__(self, messages=None):
        self._messages = messages or {}

    async def fetch_message(self, message_id):
        try:
            return self._messages[message_id]
        except KeyError:
            response = MagicMock(status=404, reason="Not Found")
            raise discord.HTTPException(response, "Unknown Message") from None


class FakeClient:
    def __init__(self, channels=None):
        self._channels = channels or {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self._channels[channel_id]


async def _store(hass: HomeAssistant) -> ButtonStore:
    store = ButtonStore(hass, "entry-1")
    await store.async_load()
    return store


async def test_handled_message_loses_buttons_and_shows_outcome(hass: HomeAssistant):
    store = await _store(hass)
    await store.async_add(
        channel_id=111, message_id=222, headline="Front door was unlocked"
    )
    message = FakeMessage("Front door was unlocked\nPick an action:")
    client = FakeClient({111: FakeChannel({222: message})})

    assert await async_retire_taken(store, client, 222, "Unlocked by Andrew") is True

    assert len(message.edits) == 1
    assert message.edits[0]["view"] is None
    assert "~~Front door was unlocked~~" in message.content
    assert "Unlocked by Andrew" in message.content
    # The take is single-use: the record is gone and a retry edits nothing.
    assert store.get(222) is None
    assert await async_retire_taken(store, client, 222, "Again") is False
    assert len(message.edits) == 1


async def test_unknown_message_fails_cleanly(hass: HomeAssistant):
    store = await _store(hass)
    client = FakeClient({111: FakeChannel({})})

    assert await async_retire_taken(store, client, 999, "Unlocked by Andrew") is False


async def test_records_survive_reload(hass: HomeAssistant):
    store = await _store(hass)
    await store.async_add(channel_id=111, message_id=222, headline="Door alert")

    reloaded = await _store(hass)
    record = reloaded.get(222)
    assert record is not None
    assert record.channel_id == 111
    assert record.headline == "Door alert"


async def test_expired_message_retired_within_one_prune_pass(hass: HomeAssistant):
    store = await _store(hass)
    await store.async_add(
        channel_id=111,
        message_id=222,
        headline="Door alert",
        ttl=timedelta(seconds=-1),
    )
    await store.async_add(channel_id=111, message_id=333, headline="Fresh alert")
    expired_message = FakeMessage("Door alert")
    fresh_message = FakeMessage("Fresh alert")
    client = FakeClient({111: FakeChannel({222: expired_message, 333: fresh_message})})

    assert await async_prune_expired(store, client) == 1

    assert len(expired_message.edits) == 1
    assert expired_message.edits[0]["view"] is None
    assert "~~Door alert~~" in expired_message.content
    assert EXPIRED_OUTCOME in expired_message.content
    assert store.get(222) is None
    # Unexpired records and their messages are untouched.
    assert fresh_message.edits == []
    assert store.get(333) is not None


async def test_prune_cap_per_run_is_respected(hass: HomeAssistant):
    store = await _store(hass)
    messages = {}
    for index in range(12):
        message_id = 1000 + index
        await store.async_add(
            channel_id=111,
            message_id=message_id,
            headline=f"Alert {index}",
            ttl=timedelta(seconds=-1),
        )
        messages[message_id] = FakeMessage(f"Alert {index}")
    client = FakeClient({111: FakeChannel(messages)})

    assert await async_prune_expired(store, client) == 10

    edited = [m for m in messages.values() if m.edits]
    assert len(edited) == 10
    assert len(store) == 2
    # The remaining two are cleared by the next pass.
    assert await async_prune_expired(store, client) == 2
    assert len(store) == 0


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so the service registration runs."""
    yield


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


def _fake_gateway_client(channel):
    client = MagicMock()
    client.login = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=True)
    client.connected = False
    client.get_channel = MagicMock(return_value=channel)
    return client


async def test_retire_message_service_edits_target(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    message = FakeMessage("Door alert")
    gateway = _fake_gateway_client(FakeChannel({222: message}))

    with patch(CLIENT, return_value=gateway):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        store = entry.runtime_data.button_store
        await store.async_add(channel_id=111, message_id=222, headline="Door alert")

        await hass.services.async_call(
            DOMAIN,
            SERVICE_RETIRE_MESSAGE,
            {
                "config_entry_id": entry.entry_id,
                "channel_id": 111,
                "message_id": 222,
                "outcome": "Unlocked by Andrew",
            },
            blocking=True,
        )

    assert len(message.edits) == 1
    assert message.edits[0]["view"] is None
    assert "~~Door alert~~" in message.content
    assert "Unlocked by Andrew" in message.content
    assert store.get(222) is None


async def test_retire_message_service_unknown_record_fails_cleanly(
    hass: HomeAssistant,
):
    entry = _entry()
    entry.add_to_hass(hass)
    gateway = _fake_gateway_client(FakeChannel({}))

    with patch(CLIENT, return_value=gateway):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RETIRE_MESSAGE,
                {
                    "config_entry_id": entry.entry_id,
                    "channel_id": 111,
                    "message_id": 999,
                },
                blocking=True,
            )

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RETIRE_MESSAGE,
                {
                    "config_entry_id": "no-such-entry",
                    "channel_id": 111,
                    "message_id": 222,
                },
                blocking=True,
            )
