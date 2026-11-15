"""Tests for the slash commands in slash_commands.py (issue #19).

Interactions are fakes: a response object that records defer() calls and
whether it is done, a followup that records sends, and an
edit_original_response that records edits. That is enough to assert the
defer-first ordering, the ephemeral refusals, and what each handler did.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    AgentTimeout,
    ConversationRouter,
)
from custom_components.discord_assist.slash_commands import (
    REFUSAL,
    TIMEOUT_REPLY,
    NotesStore,
    exposed_entity_choices,
)

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"


class FakeResponse:
    """InteractionResponse stand-in: records defer and tracks done-ness."""

    def __init__(self, events):
        self.events = events
        self.defer_calls = []
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, thinking=False, ephemeral=False):
        self.events.append("defer")
        self.defer_calls.append({"thinking": thinking, "ephemeral": ephemeral})
        self._done = True

    async def send_message(self, content=None, **kwargs):
        self.events.append("send_message")
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, *, user_id=7, channel_id=111, is_dm=False, events=None):
        self.events = events if events is not None else []
        self.user = SimpleNamespace(id=user_id, display_name="Catduck")
        if is_dm:
            channel = MagicMock(spec=discord.DMChannel)
            self.guild = None
        else:
            channel = MagicMock(spec=discord.TextChannel)
            channel.name = "general"
            self.guild = SimpleNamespace(id=555)
        channel.id = channel_id
        self.channel = channel
        self.channel_id = channel_id
        self.response = FakeResponse(self.events)
        self.followup = FakeFollowup()
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


def _client(router, hass=None):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=hass if hass is not None else object(),
            router=router,
            status_signal="test_signal",
        )
    # discord.Client.__init__ was patched out, so stub what CommandTree
    # needs (an http session and connection state) before building the tree.
    client.http = MagicMock()
    client._connection = SimpleNamespace(_command_tree=None)
    return client


def _router(hass=None, **overrides):
    defaults = dict(
        hass=hass if hass is not None else object(),
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
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def _callback(client, name):
    return client.tree.get_command(name).callback


def _result(speech="Hi there", conversation_id="conv-1"):
    return SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": speech}}),
        conversation_id=conversation_id,
    )


def test_all_commands_registered():
    client = _client(_router())
    names = {command.name for command in client.tree.get_commands()}
    assert names == {
        "assist",
        "state",
        "reset",
        "remember",
        "notes",
        "forget",
        "whoami",
    }
    state = client.tree.get_command("state")
    assert state._params["entity"].autocomplete is not None


async def test_assist_defers_before_agent_and_shares_channel_conversation():
    events = []
    router = _router()

    async def converse(*args, **kwargs):
        events.append("process")
        return _result()

    interaction = FakeInteraction(events=events)
    client = _client(router)
    with patch(_CONVERSE, new=converse):
        await _callback(client, "assist")(interaction, text="turn on the light")
    # Defer happened before the agent ran.
    assert events == ["defer", "process"]
    assert interaction.response.defer_calls == [{"thinking": True, "ephemeral": False}]
    # The reply filled in the deferred (original) response.
    assert interaction.edits[0]["content"] == "Hi there"
    # Same conversation key a plain message in channel 111 would use.
    assert router.cache.get("channel:111") == "conv-1"


async def test_assist_timeout_edits_reply_with_timeout_text():
    router = MagicMock()
    router.ha_user_for.return_value = "ha-a"
    router.process = AsyncMock(side_effect=AgentTimeout())
    client = _client(router)
    interaction = FakeInteraction()
    await _callback(client, "assist")(interaction, text="slow please")
    assert interaction.edits[0]["content"] == TIMEOUT_REPLY


@pytest.mark.parametrize(
    "command,kwargs",
    [
        ("assist", {"text": "hi"}),
        ("state", {"entity": "light.kitchen"}),
        ("reset", {}),
        ("remember", {"note": "be brief"}),
        ("notes", {}),
        ("forget", {"number": 1}),
        ("whoami", {}),
    ],
)
async def test_unauthorized_user_gets_ephemeral_refusal_and_nothing_runs(
    command, kwargs
):
    router = MagicMock()
    router.ha_user_for.return_value = None
    router.process = AsyncMock()
    client = _client(router)
    interaction = FakeInteraction(user_id=99)
    await _callback(client, command)(interaction, **kwargs)
    # Defer still happened first, then exactly one ephemeral refusal.
    assert interaction.response.defer_calls, "handler must defer first"
    assert interaction.followup.sent == [(REFUSAL, {"ephemeral": True})]
    assert interaction.edits == []
    router.process.assert_not_awaited()
    # /reset must not have touched the cache for an unauthorized user.
    router.cache.clear.assert_not_called()


def _fake_hass_with_entities(entity_ids):
    return SimpleNamespace(
        states=SimpleNamespace(async_entity_ids=lambda: list(entity_ids))
    )


def test_autocomplete_caps_at_25_and_offers_exposed_only():
    entity_ids = [f"light.lamp_{index}" for index in range(40)]
    entity_ids += ["sensor.secret", "switch.hidden"]
    hass = _fake_hass_with_entities(entity_ids)
    seen_assistants = []

    def fake_expose(hass_arg, assistant, entity_id):
        seen_assistants.append(assistant)
        return entity_id.startswith("light.")

    with patch(
        "custom_components.discord_assist.slash_commands.async_should_expose",
        side_effect=fake_expose,
    ):
        choices = exposed_entity_choices(hass, "")
    assert len(choices) == 25
    assert all(choice.value.startswith("light.") for choice in choices)
    assert set(seen_assistants) == {"conversation"}


def test_autocomplete_filters_by_typed_prefix():
    hass = _fake_hass_with_entities(
        ["light.kitchen", "light.kids_room", "light.den", "sensor.kitchen_temp"]
    )
    with patch(
        "custom_components.discord_assist.slash_commands.async_should_expose",
        return_value=True,
    ):
        by_domain = exposed_entity_choices(hass, "light.k")
        by_object = exposed_entity_choices(hass, "kitchen")
    assert [c.value for c in by_domain] == ["light.kids_room", "light.kitchen"]
    assert [c.value for c in by_object] == [
        "light.kitchen",
        "sensor.kitchen_temp",
    ]


async def test_registered_autocomplete_uses_exposed_entities():
    hass = _fake_hass_with_entities(["light.kitchen", "light.den"])
    client = _client(_router(), hass=hass)
    autocomplete = client.tree.get_command("state")._params["entity"].autocomplete
    with patch(
        "custom_components.discord_assist.slash_commands.async_should_expose",
        return_value=True,
    ):
        choices = await autocomplete(FakeInteraction(), "light.k")
    assert [choice.value for choice in choices] == ["light.kitchen"]


def _state_hass(*, can_read, state_value="on"):
    user = SimpleNamespace(
        id="ha-a",
        name="Andrew",
        permissions=SimpleNamespace(check_entity=lambda entity_id, key: can_read),
    )
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
        states=SimpleNamespace(
            get=MagicMock(
                return_value=SimpleNamespace(
                    entity_id="light.kitchen",
                    state=state_value,
                    attributes={"friendly_name": "Kitchen light"},
                )
            )
        ),
    )
    return hass


async def test_state_reads_state_as_mapped_user():
    hass = _state_hass(can_read=True)
    client = _client(_router(), hass=hass)
    interaction = FakeInteraction()
    await _callback(client, "state")(interaction, entity="light.kitchen")
    assert interaction.response.defer_calls == [{"thinking": True, "ephemeral": True}]
    assert "Kitchen light (light.kitchen) is on." in interaction.edits[0]["content"]
    hass.states.get.assert_called_once_with("light.kitchen")


async def test_state_policy_refusal_reads_nothing():
    hass = _state_hass(can_read=False)
    client = _client(_router(), hass=hass)
    interaction = FakeInteraction()
    await _callback(client, "state")(interaction, entity="light.kitchen")
    assert len(interaction.followup.sent) == 1
    content, kwargs = interaction.followup.sent[0]
    assert "do not have permission" in content
    assert kwargs == {"ephemeral": True}
    assert interaction.edits == []
    hass.states.get.assert_not_called()


async def test_reset_clears_cached_conversation_for_channel():
    router = _router()
    router.cache.set("channel:111", "conv-1")
    client = _client(router)
    interaction = FakeInteraction()
    await _callback(client, "reset")(interaction)
    assert router.cache.get("channel:111") is None
    assert "reset" in interaction.edits[0]["content"].lower()


async def test_reset_clears_cached_conversation_for_dm():
    router = _router()
    router.cache.set("dm:7", "conv-dm")
    client = _client(router)
    interaction = FakeInteraction(is_dm=True, channel_id=999)
    await _callback(client, "reset")(interaction)
    assert router.cache.get("dm:7") is None


async def test_whoami_reports_mapped_ha_user():
    user = SimpleNamespace(id="ha-a", name="Andrew")
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    )
    client = _client(_router(), hass=hass)
    interaction = FakeInteraction()
    await _callback(client, "whoami")(interaction)
    assert interaction.response.defer_calls == [{"thinking": True, "ephemeral": True}]
    content = interaction.edits[0]["content"]
    assert "Andrew" in content
    assert "ha-a" in content


async def test_remember_notes_forget_flow():
    client = _client(_router())
    interaction = FakeInteraction()
    await _callback(client, "remember")(interaction, note="all lights means all")
    assert "Remembered: all lights means all" in interaction.edits[0]["content"]

    listing = FakeInteraction()
    await _callback(client, "notes")(listing)
    assert "1. all lights means all" in listing.edits[0]["content"]

    other = FakeInteraction(user_id=8)
    other_client_router = client.router
    # User 8 is not mapped on this router, so use a mapped second user.
    other_client_router.user_map[8] = "ha-b"
    await _callback(client, "notes")(other)
    assert "no saved notes" in other.edits[0]["content"]

    forgotten = FakeInteraction()
    await _callback(client, "forget")(forgotten, number=1)
    assert "Forgot: all lights means all" in forgotten.edits[0]["content"]
    assert client.notes.list(7) == []


async def test_remember_enforces_cap_with_ephemeral_message():
    client = _client(_router())
    for index in range(10):
        await _callback(client, "remember")(FakeInteraction(), note=f"note {index}")
    interaction = FakeInteraction()
    await _callback(client, "remember")(interaction, note="one too many")
    assert interaction.edits == []
    content, kwargs = interaction.followup.sent[0]
    assert "10 notes" in content
    assert kwargs == {"ephemeral": True}
    assert len(client.notes.list(7)) == 10


def test_notes_store_cleans_and_caps_notes():
    store = NotesStore()
    saved = store.add(7, "  be\nbrief\x00 please  ")
    assert saved == "be brief please"
    long_note = store.add(7, "x" * 500)
    assert len(long_note) == 200
    with pytest.raises(ValueError):
        store.add(7, "\x00\x01")
    with pytest.raises(ValueError):
        store.forget(7, 99)


async def test_on_ready_syncs_commands_to_each_guild(monkeypatch):
    client = _client(_router())
    guild_a = SimpleNamespace(id=1001)
    guild_b = SimpleNamespace(id=1002)
    monkeypatch.setattr(
        type(client), "guilds", property(lambda self: [guild_a, guild_b])
    )
    copied = []
    synced = []

    async def fake_sync(*, guild=None):
        synced.append(guild)

    client.tree.copy_global_to = lambda *, guild: copied.append(guild)
    client.tree.sync = fake_sync
    await client._sync_slash_commands()
    assert copied == [guild_a, guild_b]
    assert synced == [guild_a, guild_b]


async def test_sync_failure_in_one_guild_does_not_stop_others(monkeypatch):
    client = _client(_router())
    guild_a = SimpleNamespace(id=1001)
    guild_b = SimpleNamespace(id=1002)
    monkeypatch.setattr(
        type(client), "guilds", property(lambda self: [guild_a, guild_b])
    )
    synced = []

    async def fake_sync(*, guild=None):
        if guild is guild_a:
            raise RuntimeError("discord said no")
        synced.append(guild)

    client.tree.copy_global_to = lambda *, guild: None
    client.tree.sync = fake_sync
    await client._sync_slash_commands()
    assert synced == [guild_b]
