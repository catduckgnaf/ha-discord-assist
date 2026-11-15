"""Retiring stale button messages (issue #27) on the shared button store."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.buttons import (
    ButtonSpec,
    ButtonStore,
    async_send_with_buttons,
    retired_content,
)
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
)
from tests.test_buttons import Clock, FakeStore


class FakeMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]


class FakeChannel:
    def __init__(self, channel_id=111):
        self.id = channel_id
        self.messages = {}

    async def send(self, content, **kwargs):
        message = FakeMessage(9000 + len(self.messages), content)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        try:
            return self.messages[message_id]
        except KeyError:
            response = MagicMock(status=404, reason="Not Found")
            raise discord.HTTPException(response, "Unknown Message") from None


class FakeClient:
    def __init__(self, *channels):
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self._channels[channel_id]


HEADLINE = "Doorbell: someone is at the door"


async def _post(store, channel, text=f"{HEADLINE}\nSnapshot"):
    (message,) = await async_send_with_buttons(
        store,
        channel,
        text,
        [ButtonSpec("Unlock", "act"), ButtonSpec("Ignore", "act")],
    )
    return message


def test_retired_content_strikes_headline_and_appends_outcome():
    assert (
        retired_content("Doorbell\nSnapshot", "Unlocked by Andrew", "Doorbell")
        == "~~Doorbell~~\nSnapshot\nUnlocked by Andrew"
    )
    # Headline edited away: prepend it struck.
    assert retired_content("Snapshot", None, "Doorbell") == "~~Doorbell~~\nSnapshot"
    # Idempotent.
    once = retired_content("Doorbell", "Done", "Doorbell")
    assert retired_content(once, "Done", "Doorbell") == once


async def test_expired_message_retired_within_one_pass():
    clock = Clock()
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=clock)
    channel = FakeChannel()
    message = await _post(store, channel)
    clock.now += 61
    assert await async_prune_expired(store, FakeClient(channel)) == 1
    edit = message.edits[-1]
    assert edit["view"] is None
    assert edit["content"] == f"~~{HEADLINE}~~\nSnapshot\n{EXPIRED_OUTCOME}"
    # Both buttons on the message were one retirement; nothing is left.
    assert await async_prune_expired(store, FakeClient(channel)) == 0
    assert len(store) == 0


async def test_prune_cap_per_run_is_respected():
    clock = Clock()
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=clock)
    channel = FakeChannel()
    for _ in range(5):
        await _post(store, channel)
    clock.now += 61
    client = FakeClient(channel)
    assert await async_prune_expired(store, client, cap=2) == 2
    assert await async_prune_expired(store, client, cap=2) == 2
    assert await async_prune_expired(store, client, cap=2) == 1
    assert await async_prune_expired(store, client, cap=2) == 0


async def test_deleted_message_is_dropped_not_retried():
    clock = Clock()
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=clock)
    channel = FakeChannel()
    message = await _post(store, channel)
    del channel.messages[message.id]
    clock.now += 61
    assert await async_prune_expired(store, FakeClient(channel)) == 0
    assert await store.async_pop_expired(10) == []


async def test_expired_hand_off_survives_restart():
    clock = Clock()
    backend = FakeStore()
    store = ButtonStore(store=backend, ttl_seconds=60, now=clock)
    await _post(store, FakeChannel())
    clock.now += 61
    await store.prune()
    restarted = ButtonStore(store=backend, ttl_seconds=60, now=clock)
    assert len(await restarted.async_pop_expired(10)) == 1


async def test_pressed_message_is_retired_with_struck_headline():
    """The dispatcher's own retire strikes the headline too."""
    store = ButtonStore(store=FakeStore(), ttl_seconds=60, now=Clock())
    message = await _post(store, FakeChannel())
    router = MagicMock()
    router.ha_user_for.return_value = "ha-a"
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=router, status_signal="s", button_store=store
        )
    client.register_button_handler("act", AsyncMock(return_value="Unlocked."))
    record = next(iter(store._records.values()))
    interaction = SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": record.custom_id},
        user=SimpleNamespace(id=7),
        message=message,
        response=SimpleNamespace(
            is_done=MagicMock(return_value=True), defer=AsyncMock()
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await client.on_interaction(interaction)
    assert message.edits[-1]["content"] == f"~~{HEADLINE}~~\nSnapshot\nUnlocked."


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant,
    enable_custom_integrations,
    fake_discord_client,
    setup_core_components,
):
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_retire_message_action_edits_target(hass: HomeAssistant, loaded):
    store = loaded.runtime_data.button_store
    channel = FakeChannel()
    message = await _post(store, channel)
    loaded.runtime_data.client.get_channel = MagicMock(return_value=channel)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RETIRE_MESSAGE,
        {
            "config_entry_id": loaded.entry_id,
            "channel_id": str(channel.id),
            "message_id": str(message.id),
            "outcome": "Handled elsewhere",
        },
        blocking=True,
    )
    assert message.edits[-1]["view"] is None
    assert message.edits[-1]["content"].endswith("Handled elsewhere")
    assert len(store) == 0


async def test_retire_message_unknown_message_fails_cleanly(
    hass: HomeAssistant, loaded
):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RETIRE_MESSAGE,
            {
                "config_entry_id": loaded.entry_id,
                "channel_id": "111",
                "message_id": "424242",
            },
            blocking=True,
        )
