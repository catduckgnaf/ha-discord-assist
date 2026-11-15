"""Duplicate bot token detection (issue #29)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_BOT_ID,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    ISSUE_DUPLICATE_TOKEN,
    LEGACY_DOMAIN,
    issue_id,
)

BOT_ID = 999
CHANNEL_ID = 111
ENTRY_ID = "entry-1"

CLIENT = "custom_components.discord_assist.DiscordAssistClient"


def _router(*, channels=None, respond_dms=True):
    router = MagicMock()
    router.channels = {CHANNEL_ID} if channels is None else channels
    router.respond_dms = respond_dms
    router.config_entry_id = ENTRY_ID
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="The light is on.")
    return router


def _client(router, monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
            router=router,
            status_signal="test_signal",
            entry_title="Mock Title",
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=BOT_ID))
    )
    return client


def _message(message_id, *, author_id=BOT_ID, channel_id=CHANNEL_ID, is_dm=False):
    if is_dm:
        channel = MagicMock(spec=discord.DMChannel)
    else:
        channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock(return_value=SimpleNamespace(id=5000))
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
        id=message_id,
        content="hello",
        channel=channel,
        author=SimpleNamespace(
            id=author_id, bot=author_id == BOT_ID, display_name="Bot"
        ),
        mentions=[],
    )
    msg.to_reference = MagicMock(return_value=SimpleNamespace(message_id=1))
    return msg


def _create_issue_calls(mock_create):
    return [
        call
        for call in mock_create.call_args_list
        if len(call.args) >= 3
        and call.args[2] == issue_id(ISSUE_DUPLICATE_TOKEN, ENTRY_ID)
    ]


async def test_foreign_own_bot_message_raises_repair_issue(monkeypatch):
    client = _client(_router(), monkeypatch)
    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(_message(4242))
        await client.on_message(_message(4243))
    calls = _create_issue_calls(mock_create)
    # Raised once even though two foreign messages arrived (log/issue once).
    assert len(calls) == 1
    call = calls[0]
    assert call.args[1] == DOMAIN
    assert call.kwargs["translation_key"] == ISSUE_DUPLICATE_TOKEN
    assert call.kwargs["translation_placeholders"] == {"entry_title": "Mock Title"}
    client.router.process.assert_not_called()


async def test_foreign_own_bot_message_in_unserved_channel_does_not_raise(
    monkeypatch,
):
    client = _client(_router(), monkeypatch)
    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(_message(4242, channel_id=222))
    assert _create_issue_calls(mock_create) == []


async def test_our_own_reply_does_not_raise(monkeypatch):
    router = _router()
    client = _client(router, monkeypatch)
    incoming = _message(10, author_id=7)
    incoming.author = SimpleNamespace(id=7, bot=False, display_name="Catduck")
    incoming.content = "<@999> hi"
    incoming.mentions = [SimpleNamespace(id=BOT_ID)]
    # The reply this process sends gets id 5000 from _message's send mock.
    await client.on_message(incoming)
    assert 5000 in client._sent_message_ids

    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(_message(5000))
    assert _create_issue_calls(mock_create) == []


async def test_chunked_reply_pieces_do_not_raise(monkeypatch):
    router = _router()
    router.process = AsyncMock(return_value="word\n" * 1000)
    client = _client(router, monkeypatch)
    incoming = _message(10, author_id=7)
    incoming.author = SimpleNamespace(id=7, bot=False, display_name="Catduck")
    incoming.content = "<@999> essay"
    incoming.mentions = [SimpleNamespace(id=BOT_ID)]
    sent_ids = iter([6001, 6002, 6003, 6004, 6005, 6006, 6007, 6008])

    async def _fake_send(*args, **kwargs):
        return SimpleNamespace(id=next(sent_ids))

    incoming.channel.send = AsyncMock(side_effect=_fake_send)
    await client.on_message(incoming)
    assert len(client._sent_message_ids) > 1

    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        for sent_id in sorted(client._sent_message_ids):
            await client.on_message(_message(sent_id))
    assert _create_issue_calls(mock_create) == []


async def test_edited_and_interaction_sent_messages_do_not_raise(monkeypatch):
    """Edits keep the sent id; interaction responses record theirs centrally."""
    client = _client(_router(), monkeypatch)
    # A message we sent and later edited keeps id 7001.
    client.record_sent_message(SimpleNamespace(id=7001))
    # An interaction response sent by this process records id 7002 through
    # the same central recording used by _send_to_channel.
    client.record_sent_message(SimpleNamespace(id=7002))
    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(_message(7001))
        await client.on_message(_message(7002))
    assert _create_issue_calls(mock_create) == []


async def test_send_to_channel_records_sent_message(monkeypatch):
    client = _client(_router(), monkeypatch)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=8001))
    sent = await client._send_to_channel(channel, "hi")
    assert sent.id == 8001
    assert 8001 in client._sent_message_ids


# --- Setup: legacy Discord Conversation entry with the same token ---


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    yield


def _entry(token="tok"):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: token, CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {"7": "ha-a"}},
    )


def _legacy(token):
    return MockConfigEntry(
        domain=LEGACY_DOMAIN,
        data={CONF_TOKEN: token},
    )


def _fake_client():
    client = MagicMock()
    client.login = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=True)
    client.connected = False
    return client


async def test_legacy_same_token_raises_repair_issue(hass: HomeAssistant):
    entry = _entry(token="tok")
    entry.add_to_hass(hass)
    _legacy("tok").add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_DUPLICATE_TOKEN, entry.entry_id)
    )
    assert issue is not None
    assert issue.translation_key == ISSUE_DUPLICATE_TOKEN


async def test_legacy_different_token_does_not_raise(hass: HomeAssistant):
    entry = _entry(token="tok")
    entry.add_to_hass(hass)
    _legacy("other-token").add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_DUPLICATE_TOKEN, entry.entry_id)
    )
    assert issue is None


async def test_no_legacy_entry_does_not_raise(hass: HomeAssistant):
    entry = _entry(token="tok")
    entry.add_to_hass(hass)

    with patch(CLIENT, return_value=_fake_client()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_DUPLICATE_TOKEN, entry.entry_id)
    )
    assert issue is None


async def test_echo_before_send_returns_is_matched_by_nonce(monkeypatch):
    """The gateway echo can beat send()'s response; the nonce covers it."""
    router = _router()
    client = _client(router, monkeypatch)
    incoming = _message(10, author_id=7)
    incoming.author = SimpleNamespace(id=7, bot=False, display_name="Catduck")
    incoming.content = "<@999> hi"
    incoming.mentions = [SimpleNamespace(id=BOT_ID)]

    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:

        async def send_with_early_echo(content, **kwargs):
            # Deliver our own echo while the REST call is still "in flight".
            echo = _message(6000)
            echo.nonce = kwargs["nonce"]
            await client.on_message(echo)
            return SimpleNamespace(id=6000)

        incoming.channel.send = AsyncMock(side_effect=send_with_early_echo)
        await client.on_message(incoming)
    assert _create_issue_calls(mock_create) == []
    assert incoming.channel.send.await_args.kwargs["nonce"]


@pytest.mark.parametrize("field", ["interaction_metadata", "webhook_id"])
async def test_interaction_and_webhook_messages_do_not_raise(monkeypatch, field):
    client = _client(_router(), monkeypatch)
    msg = _message(7000)
    setattr(msg, field, SimpleNamespace(id=1) if field != "webhook_id" else 123)
    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(msg)
    assert _create_issue_calls(mock_create) == []


async def test_message_signal_is_a_warning(monkeypatch):
    client = _client(_router(), monkeypatch)
    with patch(
        "custom_components.discord_assist.bot.ir.async_create_issue"
    ) as mock_create:
        await client.on_message(_message(4242))
    (call,) = _create_issue_calls(mock_create)
    assert call.kwargs["severity"] == ir.IssueSeverity.WARNING
