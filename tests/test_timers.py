import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.intent import (
    TimerEventType,
    TimerInfo,
    async_device_supports_timers,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_TIMER_MENTION,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    ConversationRouter,
    MessageContext,
)
from custom_components.discord_assist.timers import (
    TimerOrigin,
    TimerTracker,
    finished_message,
)

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"


def _timer(timer_id="timer-1", name="Kitchen", area_name=None):
    now = time.monotonic_ns()
    return TimerInfo(
        id=timer_id,
        name=name,
        seconds=300,
        device_id="dev-integration",
        start_hours=None,
        start_minutes=5,
        start_seconds=None,
        created_at=now,
        updated_at=now,
        language="en",
        area_name=area_name,
    )


def _tracker(poster, **kwargs):
    # object() has no async_create_background_task, so the tracker falls
    # back to scheduling on the running loop, which tests can drain.
    return TimerTracker(object(), poster, **kwargs)


async def _drain():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_finished_posts_to_origin_channel_without_mention():
    poster = AsyncMock()
    tracker = _tracker(poster)
    async with tracker.track_request(TimerOrigin(channel_id=111, discord_user_id=7)):
        tracker.handle_event(TimerEventType.STARTED, _timer())
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_awaited_once_with(111, "Kitchen timer done", None)


async def test_finished_mentions_setter_only_when_opted_in():
    poster = AsyncMock()
    tracker = _tracker(poster, timer_mention=True)
    async with tracker.track_request(TimerOrigin(channel_id=111, discord_user_id=7)):
        tracker.handle_event(TimerEventType.STARTED, _timer())
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_awaited_once_with(111, "Kitchen timer done", 7)


async def test_finished_without_origin_does_not_post():
    poster = AsyncMock()
    tracker = _tracker(poster)
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_not_awaited()


async def test_cancelled_timer_does_not_post_on_finish():
    poster = AsyncMock()
    tracker = _tracker(poster)
    async with tracker.track_request(TimerOrigin(channel_id=111, discord_user_id=7)):
        tracker.handle_event(TimerEventType.STARTED, _timer())
    tracker.handle_event(TimerEventType.CANCELLED, _timer())
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_not_awaited()


async def test_updated_timer_keeps_origin():
    poster = AsyncMock()
    tracker = _tracker(poster)
    async with tracker.track_request(TimerOrigin(channel_id=222, discord_user_id=8)):
        tracker.handle_event(TimerEventType.STARTED, _timer(name=None))
        tracker.handle_event(TimerEventType.UPDATED, _timer(name=None))
    tracker.handle_event(
        TimerEventType.FINISHED, _timer(name=None, area_name="kitchen")
    )
    await _drain()
    poster.assert_awaited_once_with(222, "Kitchen timer done", None)


def test_finished_message_fallbacks():
    assert finished_message(_timer(name="Pasta")) == "Pasta timer done"
    assert finished_message(_timer(name=None)) == "Timer done"
    assert finished_message(_timer(name=None, area_name="den")) == "Den timer done"


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
        integration_device_id="dev-integration",
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def _result(speech="ok"):
    return SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": speech}}),
        conversation_id="conv",
    )


def test_device_for_unmapped_channel_uses_integration_device():
    router = _router(room_map={111: "dev-den"})
    assert router.device_for(222, None) == "dev-integration"
    assert router.device_for(None, None) == "dev-integration"


def test_device_for_mapped_channel_and_thread_keep_room_device():
    router = _router(room_map={111: "dev-den"})
    assert router.device_for(111, None) == "dev-den"
    assert router.device_for(999, 111) == "dev-den"


async def test_process_passes_integration_device_for_unmapped_channel():
    router = _router()
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(
            text="set a 5 minute timer",
            discord_user_id=7,
            conversation_key="channel:222",
            where=MessageContext(channel_id=222),
        )
    assert converse.call_args.kwargs["device_id"] == "dev-integration"


async def test_process_passes_room_device_for_mapped_channel():
    router = _router(room_map={111: "dev-den"})
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(
            text="set a 5 minute timer",
            discord_user_id=7,
            conversation_key="channel:111",
            where=MessageContext(channel_id=111),
        )
    assert converse.call_args.kwargs["device_id"] == "dev-den"


async def test_process_correlates_timer_started_during_converse():
    """A timer the agent starts mid-converse is tied to channel + user."""
    poster = AsyncMock()
    tracker = _tracker(poster)
    router = _router(timer_tracker=tracker)

    async def converse(*args, **kwargs):
        tracker.handle_event(TimerEventType.STARTED, _timer())
        return _result("Timer started")

    with patch(_CONVERSE, new=converse):
        await router.process(
            text="set a kitchen timer",
            discord_user_id=7,
            conversation_key="channel:222",
            where=MessageContext(channel_id=222),
        )
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_awaited_once_with(222, "Kitchen timer done", None)


async def test_process_does_not_track_room_device_timers():
    """Room-device timers belong to that device's own handler, not ours."""
    poster = AsyncMock()
    tracker = _tracker(poster)
    router = _router(room_map={111: "dev-den"}, timer_tracker=tracker)

    async def converse(*args, **kwargs):
        tracker.handle_event(TimerEventType.STARTED, _timer())
        return _result()

    with patch(_CONVERSE, new=converse):
        await router.process(
            text="set a timer",
            discord_user_id=7,
            conversation_key="channel:111",
            where=MessageContext(channel_id=111),
        )
    # Nothing was pending for the integration device, so nothing is tracked.
    tracker.handle_event(TimerEventType.FINISHED, _timer())
    await _drain()
    poster.assert_not_awaited()


def test_from_entry_reads_timer_mention():
    entry = SimpleNamespace(
        options={CONF_AGENT_ID: "conversation.ollama", CONF_TIMER_MENTION: True}
    )
    router = ConversationRouter.from_entry(object(), entry)
    assert router.timer_mention is True
    entry = SimpleNamespace(options={CONF_AGENT_ID: "conversation.ollama"})
    assert ConversationRouter.from_entry(object(), entry).timer_mention is False


def _bot_client(monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=MagicMock(), status_signal="test_signal"
        )
    return client


async def test_send_timer_message_without_mention(monkeypatch):
    client = _bot_client(monkeypatch)
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(client, "get_channel", MagicMock(return_value=channel))
    await client.send_timer_message(111, "Kitchen timer done", None)
    channel.send.assert_awaited_once()
    args, kwargs = channel.send.await_args
    assert args[0] == "Kitchen timer done"
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False


async def test_send_timer_message_with_single_user_mention(monkeypatch):
    client = _bot_client(monkeypatch)
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(client, "get_channel", MagicMock(return_value=channel))
    await client.send_timer_message(111, "Kitchen timer done", 7)
    args, kwargs = channel.send.await_args
    assert args[0] == "<@7> Kitchen timer done"
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert [user.id for user in mentions.users] == [7]


async def test_send_timer_message_fetches_uncached_channel(monkeypatch):
    client = _bot_client(monkeypatch)
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(client, "get_channel", MagicMock(return_value=None))
    monkeypatch.setattr(
        client, "fetch_channel", AsyncMock(return_value=channel), raising=False
    )
    await client.send_timer_message(111, "Timer done", None)
    channel.send.assert_awaited_once()


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    yield


async def test_setup_registers_timer_handler_and_unload_unregisters(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "botid-1")}, connections=set()
    )
    assert device is not None
    assert entry.runtime_data.device_id == device.id
    assert entry.runtime_data.router.integration_device_id == device.id
    assert async_device_supports_timers(hass, device.id) is True

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert async_device_supports_timers(hass, device.id) is False
