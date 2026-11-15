from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.actions import async_send_action_message
from custom_components.discord_assist.buttons import ButtonStore
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from tests.test_buttons import Clock, FakeStore


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so setup/unload run the real entry path."""
    yield


def _entry(unique_id="botid-1"):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={CONF_TOKEN: "tok", CONF_BOT_ID: unique_id},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


def _channel(message_id=4242):
    channel = MagicMock()
    channel.id = 111
    sent = MagicMock()
    sent.id = message_id
    channel.send = AsyncMock(return_value=sent)
    return channel


def _wire(client):
    """The conftest client is a MagicMock: give it the real send path."""
    client.button_store = ButtonStore(store=FakeStore(), ttl_seconds=600, now=Clock())

    async def send_to_channel(channel, content, **kwargs):
        return await channel.send(content, **kwargs)

    async def send_action(channel, content, buttons=(), **kwargs):
        return await async_send_action_message(
            client, channel, content, buttons, **kwargs
        )

    client.send_to_channel = send_to_channel
    client.async_send_action_message = send_action


async def _setup(hass: HomeAssistant, client):
    _wire(client)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.runtime_data.client is client
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE)
    return entry


async def _call(hass, entry, **overrides):
    data = {
        "config_entry_id": entry.entry_id,
        "target": "111",
        "message": "Hello from an automation",
    }
    data.update(overrides)
    return await hass.services.async_call(
        DOMAIN, SERVICE_SEND_MESSAGE, data, blocking=True, return_response=True
    )


async def test_posts_text_to_channel(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)

    await _call(hass, entry)

    channel.send.assert_awaited_once()
    args, kwargs = channel.send.await_args
    assert args[0] == "Hello from an automation"
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert "view" not in kwargs
    assert "file" not in kwargs


async def test_dm_target_opens_direct_message(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    dm_channel = _channel(message_id=555)
    user = MagicMock()
    user.create_dm = AsyncMock(return_value=dm_channel)
    fake_discord_client.get_channel = MagicMock(return_value=None)
    fake_discord_client.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown channel")
    )
    fake_discord_client.get_user = MagicMock(return_value=None)
    fake_discord_client.fetch_user = AsyncMock(return_value=user)

    result = await _call(hass, entry, target="777")

    fake_discord_client.fetch_user.assert_awaited_once_with(777)
    user.create_dm.assert_awaited_once()
    dm_channel.send.assert_awaited_once()
    assert result["message_id"] == "555"


async def test_image_from_allowed_path(
    hass: HomeAssistant, fake_discord_client, tmp_path
):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    image_path = tmp_path / "send_message_test.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n fake image bytes")
    assert hass.config.is_allowed_path(str(image_path))

    await _call(hass, entry, image=str(image_path))

    channel.send.assert_awaited_once()
    sent_file = channel.send.await_args.kwargs["file"]
    assert isinstance(sent_file, discord.File)
    assert sent_file.filename == "send_message_test.png"


async def test_camera_needs_read_permission_for_the_caller(
    hass: HomeAssistant, fake_discord_client
):
    """A user may only post a camera they may read; nothing is fetched."""
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)
    no_read = MagicMock()
    no_read.permissions.check_entity.return_value = False
    with (
        patch.object(hass.auth, "async_get_user", AsyncMock(return_value=no_read)),
        pytest.raises(Unauthorized),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "config_entry_id": entry.entry_id,
                "target": "111",
                "message": "look",
                "image": "camera.front_door",
            },
            blocking=True,
            return_response=True,
            context=Context(user_id="ha-limited"),
        )
    # Refused before the camera is touched (its import alone would fail in
    # this test environment) or anything is sent.
    channel.send.assert_not_awaited()


async def test_disallowed_path_rejected(
    hass: HomeAssistant, fake_discord_client, tmp_path
):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"nope")
    assert not hass.config.is_allowed_path(str(outside))

    with pytest.raises(ServiceValidationError):
        await _call(hass, entry, image=str(outside))
    channel.send.assert_not_awaited()


async def test_buttons_attached(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)

    await _call(
        hass,
        entry,
        buttons=[
            {"id": "approve", "label": "Approve", "style": "success"},
            {"id": "deny", "label": "Deny"},
        ],
    )

    channel.send.assert_awaited_once()
    view = channel.send.await_args.kwargs["view"]
    assert isinstance(view, discord.ui.View)
    assert len(view.children) == 2
    # Each button is a record in the shared store; the custom_id names the
    # record, and the automation's id travels in its payload.
    store = fake_discord_client.button_store
    records = [
        await store.peek(child.custom_id.split(":")[2]) for child in view.children
    ]
    assert all(child.custom_id.startswith("da:act:") for child in view.children)
    assert [r.payload["action_id"] for r in records] == ["approve", "deny"]
    assert view.children[0].label == "Approve"
    assert view.children[0].style is discord.ButtonStyle.success
    assert view.children[1].style is discord.ButtonStyle.primary
    # Stopped, so discord.py does not keep the view forever.
    assert view.is_finished()


async def test_bad_buttons_rejected(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    fake_discord_client.get_channel = MagicMock(return_value=_channel())
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            entry,
            buttons=[{"id": "same", "label": "A"}, {"id": "same", "label": "B"}],
        )
    with pytest.raises(vol.Invalid):
        await _call(
            hass, entry, buttons=[{"id": str(i), "label": "x"} for i in range(26)]
        )
    with pytest.raises(vol.Invalid):
        await _call(hass, entry, buttons=[{"id": "x", "label": "x", "style": "link"}])


async def test_response_returns_message_id(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel(message_id=987654321)
    fake_discord_client.get_channel = MagicMock(return_value=channel)

    result = await _call(hass, entry)

    assert result == {
        "message_id": "987654321",
        "message_ids": ["987654321"],
        "channel_id": "111",
    }


async def test_unloaded_entry_rejected(hass: HomeAssistant, fake_discord_client):
    await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)

    not_loaded = _entry(unique_id="botid-2")
    not_loaded.add_to_hass(hass)

    with pytest.raises(ServiceValidationError):
        await _call(hass, not_loaded)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "config_entry_id": "does-not-exist",
                "target": "111",
                "message": "hi",
            },
            blocking=True,
            return_response=True,
        )
    channel.send.assert_not_awaited()


async def test_unreachable_target_rejected(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    fake_discord_client.get_channel = MagicMock(return_value=None)
    fake_discord_client.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown channel")
    )
    fake_discord_client.get_user = MagicMock(return_value=None)
    fake_discord_client.fetch_user = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown user")
    )

    with pytest.raises(ServiceValidationError):
        await _call(hass, entry, target="999")


async def test_long_message_is_chunked(hass: HomeAssistant, fake_discord_client):
    entry = await _setup(hass, fake_discord_client)
    channel = _channel()
    fake_discord_client.get_channel = MagicMock(return_value=channel)

    await _call(hass, entry, message="word\n" * 1000)

    assert channel.send.await_count > 1
    for call in channel.send.await_args_list:
        assert len(call.args[0]) <= 2000


async def test_action_stays_registered_and_rejects_when_unloaded(
    hass: HomeAssistant, fake_discord_client
):
    entry = await _setup(hass, fake_discord_client)

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()

    # Registered in async_setup: it exists and fails cleanly.
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE)
    with pytest.raises(ServiceValidationError):
        await _call(hass, entry)
