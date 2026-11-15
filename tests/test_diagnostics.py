import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist import RuntimeData
from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_ALLOWLIST,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_FALLBACK_USER,
    CONF_ROOM_MAP,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
)
from custom_components.discord_assist.conversation_cache import (
    ConversationCache,
    TurnLocks,
)
from custom_components.discord_assist.conversation_router import ConversationRouter
from custom_components.discord_assist.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.discord_assist.recent_errors import (
    MAX_RECENT_ERRORS,
    RecentErrors,
)

SECRET_TOKEN = "secret-token-value-12345"
DISCORD_USER_ID = "987654321"
HA_USER_ID = "ha-user-id-abcdef"
HA_FALLBACK_ID = "ha-fallback-id-abcdef"
CHANNEL_ID = "555444333222"
DEVICE_ID = "device-id-abcdef"
BOT_ID = "bot-id-98765"
SECRET_MESSAGE = "turn on the lights, token is secret-token-value-12345"


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=BOT_ID,
        data={CONF_TOKEN: SECRET_TOKEN, CONF_BOT_ID: BOT_ID},
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {DISCORD_USER_ID: HA_USER_ID},
            CONF_ALLOWLIST: [DISCORD_USER_ID],
            CONF_FALLBACK_USER: HA_FALLBACK_ID,
            CONF_CHANNELS: [CHANNEL_ID],
            CONF_ROOM_MAP: {CHANNEL_ID: DEVICE_ID},
            "respond_dms": True,
        },
    )


def _router(hass):
    return ConversationRouter(
        hass=hass,
        agent_id="conversation.ollama",
        language=None,
        channels=set(),
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        turns=TurnLocks(),
    )


def _fake_client():
    intents = discord.Intents.default()
    intents.message_content = True
    channels = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    guilds = [
        SimpleNamespace(channels=channels[:2]),
        SimpleNamespace(channels=channels[2:]),
    ]
    return SimpleNamespace(
        connected=True,
        intents=intents,
        guilds=guilds,
        get_all_channels=lambda: iter(channels),
    )


async def test_diagnostics_shape_and_counts(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    router = _router(hass)
    router.cache.set("channel:1", "conv-1")
    router.cache.set("channel:2", "conv-2")
    async with router.turns.hold("channel:1"):
        pass
    errors = RecentErrors()
    errors.record(RuntimeError("boom"), source="gateway")
    entry.runtime_data = RuntimeData(
        client=_fake_client(), router=router, recent_errors=errors
    )

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert set(payload) == {"entry", "connection", "conversation", "recent_errors"}
    assert payload["entry"]["options"]["respond_dms"] is True
    assert payload["entry"]["options"][CONF_AGENT_ID] == "conversation.ollama"
    connection = payload["connection"]
    assert connection["connected"] is True
    assert connection["discord_py_version"] == discord.__version__
    assert "message_content" in connection["intents"]
    assert "guilds" in connection["intents"]
    assert connection["guild_count"] == 2
    assert connection["channel_count"] == 3
    assert payload["conversation"] == {"cache_size": 2, "turn_lock_count": 1}
    assert len(payload["recent_errors"]) == 1
    assert payload["recent_errors"][0]["error_type"] == "RuntimeError"
    assert payload["recent_errors"][0]["source"] == "gateway"


async def test_diagnostics_never_leaks_token_or_ids(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    entry.runtime_data = RuntimeData(
        client=_fake_client(), router=_router(hass), recent_errors=RecentErrors()
    )

    payload = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(payload)

    for secret in (
        SECRET_TOKEN,
        DISCORD_USER_ID,
        HA_USER_ID,
        HA_FALLBACK_ID,
        CHANNEL_ID,
        DEVICE_ID,
        BOT_ID,
    ):
        assert secret not in dumped
    assert payload["entry"]["data"][CONF_TOKEN] == REDACTED
    assert payload["entry"]["data"][CONF_BOT_ID] == REDACTED
    assert payload["entry"]["options"][CONF_USER_MAP] == REDACTED
    assert payload["entry"]["options"][CONF_ALLOWLIST] == REDACTED
    assert payload["entry"]["options"][CONF_FALLBACK_USER] == REDACTED
    assert payload["entry"]["options"][CONF_CHANNELS] == REDACTED
    assert payload["entry"]["options"][CONF_ROOM_MAP] == REDACTED


async def test_recent_errors_carry_no_message_content(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    errors = RecentErrors()
    errors.record(RuntimeError(SECRET_MESSAGE), source="on_message")
    entry.runtime_data = RuntimeData(
        client=_fake_client(), router=_router(hass), recent_errors=errors
    )

    payload = await async_get_config_entry_diagnostics(hass, entry)

    (recorded,) = payload["recent_errors"]
    assert set(recorded) == {"timestamp", "error_type", "source"}
    assert recorded["error_type"] == "RuntimeError"
    assert recorded["source"] == "on_message"
    assert recorded["timestamp"]
    dumped = json.dumps(payload)
    assert SECRET_MESSAGE not in dumped
    assert "turn on the lights" not in dumped
    assert SECRET_TOKEN not in dumped


def test_recent_errors_ring_buffer_is_bounded():
    errors = RecentErrors()
    for index in range(MAX_RECENT_ERRORS + 5):
        errors.record(ValueError(f"message {index}"), source=f"s{index}")
    snapshot = errors.snapshot()
    assert len(errors) == MAX_RECENT_ERRORS
    assert len(snapshot) == MAX_RECENT_ERRORS
    assert snapshot[0]["source"] == "s5"
    assert snapshot[-1]["source"] == f"s{MAX_RECENT_ERRORS + 4}"
    assert all(set(item) == {"timestamp", "error_type", "source"} for item in snapshot)


async def test_diagnostics_without_runtime_data(hass: HomeAssistant):
    """An entry that is not loaded still downloads, redacted and empty."""
    entry = _entry()
    entry.add_to_hass(hass)

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["connection"]["connected"] is False
    assert payload["connection"]["guild_count"] == 0
    assert payload["connection"]["channel_count"] == 0
    assert payload["conversation"] == {"cache_size": 0, "turn_lock_count": 0}
    assert payload["recent_errors"] == []
    assert SECRET_TOKEN not in json.dumps(payload)


@pytest.fixture(autouse=True)
async def _load_integration(enable_custom_integrations, setup_core_components):
    yield


async def test_diagnostics_after_real_setup(hass: HomeAssistant, fake_discord_client):
    entry = _entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert payload["connection"]["connected"] is False
    assert payload["connection"]["discord_py_version"] == discord.__version__
    assert payload["recent_errors"] == []
    assert SECRET_TOKEN not in json.dumps(payload)


async def test_gateway_failure_is_recorded(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.login = AsyncMock()
    client.connect = AsyncMock(side_effect=RuntimeError(SECRET_MESSAGE))
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=False)
    client.connected = False

    with (
        patch(
            "custom_components.discord_assist.DiscordAssistClient",
            return_value=client,
        ),
        patch("custom_components.discord_assist.RECONNECT_DELAY", 0),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    (recorded,) = entry.runtime_data.recent_errors.snapshot()
    assert recorded["error_type"] == "RuntimeError"
    assert recorded["source"] == "gateway"
    assert SECRET_MESSAGE not in json.dumps(recorded)


async def test_on_message_error_is_recorded_without_message_text(monkeypatch):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(side_effect=RuntimeError(SECRET_MESSAGE))
    errors = RecentErrors()
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
            router=router,
            status_signal="test_signal",
            recent_errors=errors,
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 111
    channel.send = AsyncMock()
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
        content="<@999> turn on the lights",
        channel=channel,
        author=SimpleNamespace(id=7, bot=False, display_name="Catduck"),
        mentions=[SimpleNamespace(id=999)],
    )
    msg.to_reference = MagicMock(return_value=SimpleNamespace(message_id=1))

    await client.on_message(msg)

    (recorded,) = errors.snapshot()
    assert recorded["error_type"] == "RuntimeError"
    assert recorded["source"] == "on_message"
    assert SECRET_MESSAGE not in json.dumps(recorded)
    assert "turn on the lights" not in json.dumps(recorded)
