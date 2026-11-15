"""Tests for acknowledge-first interaction handling (issue #14).

There is no fake Discord kit on main yet, so this file carries a small
local fake: its response object mimics discord.py closely enough that a
second ``defer()`` raises ``InteractionResponded``, exactly like the real
``InteractionResponse``. Event lists record call order across the fake
interaction and the fake router, which is what proves defer happens
before any Home Assistant / agent call.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from custom_components.discord_assist.bot import (
    ERROR_REPLY,
    TIMEOUT_REPLY,
    UNAUTHORIZED_REPLY,
    DiscordAssistClient,
    acknowledged,
    interaction_text,
)


class FakeResponse:
    """Stand-in for discord.InteractionResponse."""

    def __init__(self, events, *, done=False):
        self._events = events
        self._done = done
        self.defer_calls = []

    def is_done(self):
        return self._done

    async def defer(self, *, thinking=False, ephemeral=False):
        if self._done:
            raise discord.InteractionResponded(MagicMock())
        self._done = True
        self.defer_calls.append({"thinking": thinking, "ephemeral": ephemeral})
        self._events.append("defer")


class FakeFollowup:
    def __init__(self, events):
        self._events = events
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, content, **kwargs):
        self._events.append("followup")


def _interaction(
    events,
    *,
    interaction_type=discord.InteractionType.application_command,
    text="turn on the kitchen light",
    done=False,
    channel_id=111,
    guild_id=555,
):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = "general"
    data = {}
    if text is not None:
        data = {"name": "assist", "options": [{"name": "text", "value": text}]}
    interaction = SimpleNamespace(
        id=4242,
        type=interaction_type,
        data=data,
        guild_id=guild_id,
        channel_id=channel_id,
        channel=channel,
        user=SimpleNamespace(id=7, display_name="Catduck", bot=False),
        response=FakeResponse(events, done=done),
        followup=FakeFollowup(events),
    )

    async def _edit(*, content=None, allowed_mentions=None, **kwargs):
        events.append("edit")
        interaction.edited_content = content
        interaction.edited_allowed_mentions = allowed_mentions

    interaction.edit_original_response = AsyncMock(side_effect=_edit)
    return interaction


def _client(router, monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=router, status_signal="test_signal"
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client


def _router(events, *, answer="Done.", should_respond=True):
    router = MagicMock()
    router.should_respond.return_value = should_respond

    async def _process(**kwargs):
        events.append("agent")
        router.process_kwargs = kwargs
        return answer

    router.process = AsyncMock(side_effect=_process)
    return router


async def test_defer_happens_before_any_agent_call(monkeypatch):
    events = []
    router = _router(events)
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    await client.on_interaction(interaction)

    assert events == ["defer", "agent", "edit"]
    assert interaction.response.defer_calls == [{"thinking": True, "ephemeral": False}]
    assert router.process_kwargs["text"] == "turn on the kitchen light"
    assert router.process_kwargs["discord_user_id"] == 7
    assert interaction.edited_content == "Done."
    # Agent output is untrusted: the edit must not allow mentions.
    assert interaction.edited_allowed_mentions.everyone is False
    assert interaction.edited_allowed_mentions.roles is False
    assert interaction.edited_allowed_mentions.users is False


async def test_slow_answer_still_lands_via_edit_original_response(monkeypatch):
    """A 20 second answer lands because defer already secured the token.

    The elapsed time is simulated with a fake clock advanced by the fake
    agent, so the test does not really wait 20 seconds.
    """
    events = []
    clock = {"elapsed": 0.0}
    router = MagicMock()
    router.should_respond.return_value = True

    async def _slow_process(**kwargs):
        events.append("agent")
        clock["elapsed"] += 20.0
        return "Slow answer."

    router.process = AsyncMock(side_effect=_slow_process)
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    await client.on_interaction(interaction)

    assert clock["elapsed"] == 20.0
    # Well past Discord's 3 second initial-response window, yet the edit
    # still works because defer happened first, before the agent ran.
    assert events == ["defer", "agent", "edit"]
    interaction.edit_original_response.assert_awaited_once()
    assert interaction.edited_content == "Slow answer."


async def test_timeout_path_edits_response_with_timeout_text(monkeypatch):
    from custom_components.discord_assist.conversation_router import AgentTimeout

    events = []
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(side_effect=AgentTimeout())
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    await client.on_interaction(interaction)

    assert events[0] == "defer"
    interaction.edit_original_response.assert_awaited_once()
    assert interaction.edited_content == TIMEOUT_REPLY


async def test_error_path_edits_response_with_error_text(monkeypatch):
    events = []
    router = _router(events)
    router.process = AsyncMock(side_effect=RuntimeError("boom"))
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    # Must not raise: the gateway handler never dies on an agent error.
    await client.on_interaction(interaction)

    assert interaction.edited_content == ERROR_REPLY


@pytest.mark.parametrize(
    "interaction_type",
    [
        discord.InteractionType.component,
        discord.InteractionType.autocomplete,
        discord.InteractionType.modal_submit,
    ],
)
async def test_non_command_interactions_are_left_alone(monkeypatch, interaction_type):
    """Components, modals and autocomplete never defer here or reach the agent.

    Views answer their own components, and autocomplete fires on every
    keystroke of half-typed text.
    """
    events = []
    router = _router(events)
    client = _client(router, monkeypatch)
    interaction = _interaction(events, interaction_type=interaction_type)

    await client.on_interaction(interaction)

    assert interaction.response.defer_calls == []
    assert events == []
    router.process.assert_not_called()
    interaction.edit_original_response.assert_not_called()


async def test_unauthorized_interaction_never_calls_agent(monkeypatch):
    events = []
    router = _router(events, should_respond=False)
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    await client.on_interaction(interaction)

    assert events[0] == "defer"
    router.process.assert_not_called()
    assert interaction.edited_content == UNAUTHORIZED_REPLY


async def test_acknowledged_skips_defer_when_response_already_done():
    events = []
    interaction = _interaction(events, done=True)

    async with acknowledged(interaction, thinking=True) as yielded:
        events.append("work")

    assert yielded is interaction
    assert interaction.response.defer_calls == []
    assert events == ["work"]


async def test_acknowledged_forwards_thinking_and_ephemeral():
    events = []
    interaction = _interaction(events)

    async with acknowledged(interaction, thinking=True, ephemeral=True):
        events.append("work")
        await interaction.edit_original_response(content="hi")

    assert interaction.response.defer_calls == [{"thinking": True, "ephemeral": True}]
    assert events == ["defer", "work", "edit"]


async def test_acknowledged_handler_can_follow_up():
    """The pattern button and slash handlers use for extra output."""
    events = []
    interaction = _interaction(events)

    async with acknowledged(interaction):
        await interaction.edit_original_response(content="first")
        await interaction.followup.send("second")

    assert events == ["defer", "edit", "followup"]


async def test_long_answer_edits_original_then_follows_up(monkeypatch):
    events = []
    router = _router(events, answer="word\n" * 1000)
    client = _client(router, monkeypatch)
    interaction = _interaction(events)

    await client.on_interaction(interaction)

    interaction.edit_original_response.assert_awaited_once()
    assert interaction.followup.send.await_count >= 1
    followup_mentions = interaction.followup.send.await_args.kwargs["allowed_mentions"]
    assert followup_mentions.everyone is False


def test_interaction_text_reads_prompt_option():
    events = []
    interaction = _interaction(events, text=None)
    interaction.data = {
        "name": "assist",
        "options": [{"name": "text", "type": 3, "value": "  hello  "}],
    }
    assert interaction_text(interaction) == "hello"


def test_interaction_text_reads_nested_subcommand_option():
    events = []
    interaction = _interaction(events, text=None)
    interaction.data = {
        "name": "assist",
        "options": [
            {
                "name": "ask",
                "options": [{"name": "prompt", "value": "nested question"}],
            }
        ],
    }
    assert interaction_text(interaction) == "nested question"


def test_interaction_text_none_for_component_data():
    events = []
    interaction = _interaction(events, text=None)
    interaction.data = {"custom_id": "assist:confirm:1"}
    assert interaction_text(interaction) is None


@pytest.mark.parametrize("done", [True])
async def test_on_interaction_does_not_double_defer(monkeypatch, done):
    events = []
    router = _router(events)
    client = _client(router, monkeypatch)
    interaction = _interaction(events, done=done)

    await client.on_interaction(interaction)

    assert interaction.response.defer_calls == []
    assert events == ["agent", "edit"]
