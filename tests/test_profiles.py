"""Tests for access profiles (issue #12, permissions phase A)."""

from datetime import time as dt_time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.discord_assist.const import (
    CONF_PROFILE_ROLE_MAP,
    CONF_PROFILE_USER_MAP,
    CONF_PROFILES,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import (
    ConversationRouter,
    MessageContext,
    Profile,
)

_CONVERSE = "custom_components.discord_assist.conversation_router.async_converse"

FAMILY = Profile(name="family", ha_user="ha-family")
KIDS = Profile(
    name="kids",
    ha_user="ha-kids",
    channels=frozenset({222}),
    agent="conversation.home_assistant",
)
DENY = Profile(name="deny", ha_user=None)


def _router(**overrides):
    defaults = dict(
        hass=object(),
        agent_id="conversation.ollama",
        language=None,
        channels={111},
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        speaker_context=False,
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def _result(speech="ok"):
    return SimpleNamespace(
        response=SimpleNamespace(speech={"plain": {"speech": speech}}),
        conversation_id="conv",
    )


def _profile_router(**overrides):
    defaults = dict(
        profiles={"family": FAMILY, "kids": KIDS, "deny": DENY},
        profile_user_map={1: "family"},
        profile_role_map={50: "kids", 60: "deny"},
    )
    defaults.update(overrides)
    return _router(**defaults)


# --- Assignment -----------------------------------------------------------


def test_user_mapping_beats_role_mapping():
    router = _profile_router()
    profile = router.profile_for(1, role_ids=[50])
    assert profile is not None
    assert profile.name == "family"
    assert router.ha_user_for(1, role_ids=[50]) == "ha-family"


def test_role_mapping_applies_without_user_mapping():
    router = _profile_router()
    profile = router.profile_for(2, role_ids=[50])
    assert profile is not None
    assert profile.name == "kids"
    # In DMs there are no roles, so a role-only user gets nothing.
    assert router.profile_for(2, role_ids=()) is None


def test_deny_role_beats_allow_user_mapping():
    router = _profile_router()
    profile = router.profile_for(1, role_ids=[60])
    assert profile is not None
    assert profile.name == "deny"
    assert router.ha_user_for(1, role_ids=[60]) is None
    assert (
        router.should_respond(
            is_dm=True,
            was_mentioned=False,
            channel_id=0,
            author_id=1,
            role_ids=[60],
        )
        is False
    )


def test_deny_user_mapping_beats_allow_role():
    router = _profile_router(profile_user_map={1: "deny"})
    profile = router.profile_for(1, role_ids=[50])
    assert profile is not None and profile.is_deny


def test_unmatched_user_is_ignored():
    router = _profile_router()
    assert router.profile_for(99, role_ids=[999]) is None
    assert router.ha_user_for(99) is None
    assert (
        router.should_respond(
            is_dm=True, was_mentioned=False, channel_id=0, author_id=99
        )
        is False
    )


async def test_unmatched_process_raises_and_agent_not_called():
    router = _profile_router()
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        with pytest.raises(PermissionError):
            await router.process(
                text="hi", discord_user_id=99, conversation_key="dm:99"
            )
    converse.assert_not_called()


async def test_deny_process_raises_and_agent_not_called():
    router = _profile_router()
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        with pytest.raises(PermissionError):
            await router.process(
                text="hi",
                discord_user_id=1,
                conversation_key="dm:1",
                role_ids=[60],
            )
    converse.assert_not_called()


# --- Gatekeeping ----------------------------------------------------------


def test_profile_channel_allowlist():
    router = _profile_router()
    base = dict(is_dm=False, was_mentioned=False, author_id=2, role_ids=[50])
    assert router.should_respond(channel_id=222, **base) is True
    assert router.should_respond(channel_id=111, **base) is False
    # A thread in an allowed channel counts.
    assert router.should_respond(channel_id=999, parent_id=222, **base) is True


async def test_process_blocks_wrong_channel_before_agent():
    router = _profile_router()
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        with pytest.raises(PermissionError):
            await router.process(
                text="hi",
                discord_user_id=2,
                conversation_key="channel:111",
                where=MessageContext(channel_id=111),
                role_ids=[50],
            )
    converse.assert_not_called()


def test_profile_dms_disabled():
    router = _profile_router(
        profiles={"family": Profile(name="family", ha_user="ha-f", dms=False)},
        profile_user_map={1: "family"},
        profile_role_map={},
    )
    assert (
        router.should_respond(
            is_dm=True, was_mentioned=False, channel_id=0, author_id=1
        )
        is False
    )


async def test_process_blocks_dm_before_agent():
    router = _profile_router(
        profiles={"family": Profile(name="family", ha_user="ha-f", dms=False)},
        profile_user_map={1: "family"},
        profile_role_map={},
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        with pytest.raises(PermissionError):
            await router.process(
                text="hi", discord_user_id=1, conversation_key="dm:1", is_dm=True
            )
    converse.assert_not_called()


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (dt_time(21, 0), True),
        (dt_time(23, 30), True),
        (dt_time(3, 0), True),  # window wraps past midnight
        (dt_time(6, 59), True),
        (dt_time(7, 0), False),
        (dt_time(12, 0), False),
        (dt_time(20, 59), False),
    ],
)
def test_quiet_hours_wrap_past_midnight(at, expected):
    profile = Profile(name="kids", ha_user="ha-k", quiet_hours="21:00-07:00")
    assert profile.in_quiet_hours(at) is expected


def test_quiet_hours_same_day_window():
    profile = Profile(name="p", ha_user="ha", quiet_hours="01:00-05:00")
    assert profile.in_quiet_hours(dt_time(3, 0)) is True
    assert profile.in_quiet_hours(dt_time(12, 0)) is False
    assert Profile(name="p", ha_user="ha").in_quiet_hours(dt_time(3, 0)) is False
    broken = Profile(name="p", ha_user="ha", quiet_hours="whenever")
    assert broken.in_quiet_hours(dt_time(3, 0)) is False


async def test_quiet_hours_block_before_agent():
    router = _profile_router(
        profiles={
            "kids": Profile(name="kids", ha_user="ha-k", quiet_hours="21:00-07:00")
        },
        profile_user_map={2: "kids"},
        profile_role_map={},
    )
    with (
        patch.object(ConversationRouter, "_local_time", lambda self: dt_time(22, 0)),
        patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse,
    ):
        assert (
            router.should_respond(
                is_dm=True, was_mentioned=False, channel_id=0, author_id=2
            )
            is False
        )
        with pytest.raises(PermissionError):
            await router.process(text="hi", discord_user_id=2, conversation_key="dm:2")
    converse.assert_not_called()


async def test_per_profile_agent_is_used():
    router = _profile_router()
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(
            text="hi",
            discord_user_id=2,
            conversation_key="channel:222",
            where=MessageContext(channel_id=222),
            role_ids=[50],
        )
    assert converse.call_args.kwargs["agent_id"] == "conversation.home_assistant"
    ctx = converse.call_args.args[3]
    assert ctx.user_id == "ha-kids"


async def test_rate_limit_blocks_after_quota():
    router = _profile_router(
        profiles={"family": Profile(name="family", ha_user="ha-f", rate_limit=1)},
        profile_user_map={1: "family"},
        profile_role_map={},
    )
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse:
        await router.process(text="hi", discord_user_id=1, conversation_key="dm:1")
        # Peeking in should_respond sees the used quota without consuming more.
        assert (
            router.should_respond(
                is_dm=True, was_mentioned=False, channel_id=0, author_id=1
            )
            is False
        )
        with pytest.raises(PermissionError):
            await router.process(text="hi", discord_user_id=1, conversation_key="dm:1")
    assert converse.await_count == 1


def test_profile_stores_phase_b_c_fields_unenforced():
    profile = Profile(
        name="family",
        ha_user="ha-f",
        areas=("kitchen",),
        domains=("lock",),
        confirm=("cover",),
    )
    assert profile.areas == ("kitchen",)
    assert profile.domains == ("lock",)
    assert profile.confirm == ("cover",)


# --- Legacy behaviour -----------------------------------------------------


def test_legacy_entry_synthesizes_default_profile():
    router = _router(user_map={7: "ha-a"}, allowlist={42}, fallback_user="ha-fb")
    mapped = router.profile_for(7)
    assert mapped is not None
    assert mapped.name == "default"
    assert mapped.ha_user == "ha-a"
    listed = router.profile_for(42)
    assert listed is not None and listed.ha_user == "ha-fb"
    assert router.profile_for(9) is None
    assert router.has_authorized_users is True


def test_has_authorized_users_from_profiles():
    assert _profile_router().has_authorized_users is True
    deny_only = _profile_router(profile_user_map={1: "deny"}, profile_role_map={})
    assert deny_only.has_authorized_users is False
    unassigned = _profile_router(profile_user_map={}, profile_role_map={})
    assert unassigned.has_authorized_users is False


def test_from_entry_reads_profiles():
    entry = SimpleNamespace(
        options={
            CONF_PROFILES: {
                "kids": {
                    "ha_user": "ha-kids",
                    "channels": ["222", "bad"],
                    "dms": False,
                    "quiet_hours": "21:00-07:00",
                    "rate_limit": 5,
                    "agent": "conversation.home_assistant",
                    "areas": ["playroom"],
                    "domains": ["light"],
                    "confirm": ["cover"],
                },
                "deny": {"ha_user": None},
                "broken": "not-a-mapping",
            },
            CONF_PROFILE_USER_MAP: {"1": "kids", "bad": "x"},
            CONF_PROFILE_ROLE_MAP: {"50": "kids"},
        }
    )
    router = ConversationRouter.from_entry(object(), entry)
    kids = router.profiles["kids"]
    assert kids.ha_user == "ha-kids"
    assert kids.channels == frozenset({222})
    assert kids.dms is False
    assert kids.quiet_hours == "21:00-07:00"
    assert kids.rate_limit == 5
    assert kids.agent == "conversation.home_assistant"
    assert kids.areas == ("playroom",)
    assert kids.domains == ("light",)
    assert kids.confirm == ("cover",)
    assert router.profiles["deny"].is_deny
    assert "broken" not in router.profiles
    assert router.profile_user_map == {1: "kids"}
    assert router.profile_role_map == {50: "kids"}
    assert router.profile_for(1) is kids


# --- Audit ----------------------------------------------------------------


async def test_audit_event_and_logbook_on_handled_request(hass: HomeAssistant):
    router = _profile_router(hass=hass)
    request_events = async_capture_events(hass, "discord_assist_request")
    logbook_events = async_capture_events(hass, "logbook_entry")
    with patch(_CONVERSE, new=AsyncMock(return_value=_result())):
        await router.process(
            text="hi",
            discord_user_id=1,
            conversation_key="channel:111",
            where=MessageContext(channel_id=111),
        )
    await hass.async_block_till_done()
    assert len(request_events) == 1
    data = request_events[0].data
    assert data["discord_user_id"] == 1
    assert data["profile"] == "family"
    assert data["channel_id"] == 111
    assert data["agent_id"] == "conversation.ollama"
    assert data["outcome"] == "answered"
    assert len(logbook_events) == 1
    # The logbook entry carries the request Context (the HA identity).
    assert logbook_events[0].context.user_id == "ha-family"
    assert "family" in logbook_events[0].data["message"]


async def test_audit_event_on_quiet_blocked_request(hass: HomeAssistant):
    router = _profile_router(
        hass=hass,
        profiles={
            "kids": Profile(name="kids", ha_user="ha-k", quiet_hours="21:00-07:00")
        },
        profile_user_map={2: "kids"},
        profile_role_map={},
    )
    request_events = async_capture_events(hass, "discord_assist_request")
    logbook_events = async_capture_events(hass, "logbook_entry")
    with (
        patch.object(ConversationRouter, "_local_time", lambda self: dt_time(23, 0)),
        patch(_CONVERSE, new=AsyncMock(return_value=_result())) as converse,
        pytest.raises(PermissionError),
    ):
        await router.process(text="hi", discord_user_id=2, conversation_key="dm:2")
    await hass.async_block_till_done()
    converse.assert_not_called()
    assert len(request_events) == 1
    assert request_events[0].data["outcome"] == "quiet_hours"
    assert request_events[0].data["profile"] == "kids"
    assert len(logbook_events) == 1
    assert logbook_events[0].context.user_id == "ha-k"
