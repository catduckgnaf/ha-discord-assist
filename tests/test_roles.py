"""Role resolution (#37): payload first, fetch fallback, fail closed."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.const import (
    CONF_ROLE_MAP,
    DOMAIN,
    ISSUE_ROLE_RESOLUTION,
    issue_id,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import ConversationRouter
from custom_components.discord_assist.roles import RoleResolver


def _member(user_id, role_ids):
    return SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=r) for r in role_ids])


def _guild(guild_id=55, fetch=None):
    return SimpleNamespace(
        id=guild_id, fetch_member=fetch or AsyncMock(return_value=_member(7, []))
    )


def _router(hass, **overrides):
    defaults = dict(
        hass=hass,
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


async def test_payload_roles_used_without_fetch():
    fetch = AsyncMock(side_effect=AssertionError("fetch must not be called"))
    resolver = RoleResolver()
    result = await resolver.resolve(
        author=_member(7, [10, 20]), guild=_guild(fetch=fetch)
    )
    assert result.role_ids == frozenset({10, 20})
    assert result.resolved is True
    fetch.assert_not_awaited()


async def test_fetch_fallback_when_author_carries_no_roles():
    fetch = AsyncMock(return_value=_member(7, [30]))
    resolver = RoleResolver()
    author = SimpleNamespace(id=7)  # plain User: no roles attribute
    result = await resolver.resolve(author=author, guild=_guild(fetch=fetch))
    assert result.role_ids == frozenset({30})
    assert result.resolved is True
    fetch.assert_awaited_once_with(7)


async def test_fetch_fallback_when_payload_roles_missing():
    fetch = AsyncMock(return_value=_member(7, [31]))
    resolver = RoleResolver()
    author = SimpleNamespace(id=7, roles=None)  # partial member, no role list
    result = await resolver.resolve(author=author, guild=_guild(fetch=fetch))
    assert result.role_ids == frozenset({31})
    fetch.assert_awaited_once_with(7)


async def test_cache_prevents_second_fetch_within_ttl():
    fetch = AsyncMock(return_value=_member(7, [30]))
    resolver = RoleResolver()
    guild = _guild(fetch=fetch)
    author = SimpleNamespace(id=7)
    first = await resolver.resolve(author=author, guild=guild)
    second = await resolver.resolve(author=author, guild=guild)
    assert first.role_ids == second.role_ids == frozenset({30})
    fetch.assert_awaited_once_with(7)


async def test_fetch_failure_fails_closed():
    fetch = AsyncMock(side_effect=RuntimeError("discord down"))
    resolver = RoleResolver()
    result = await resolver.resolve(
        author=SimpleNamespace(id=7), guild=_guild(fetch=fetch)
    )
    assert result.role_ids == frozenset()
    assert result.resolved is False


async def test_dm_has_no_roles_and_is_not_a_failure():
    fetch = AsyncMock(side_effect=AssertionError("no guild to fetch from"))
    resolver = RoleResolver()
    result = await resolver.resolve(author=SimpleNamespace(id=7), guild=None)
    assert result.role_ids == frozenset()
    assert result.resolved is True
    fetch.assert_not_awaited()


async def test_repair_issue_when_mapping_configured_and_resolution_fails(
    hass: HomeAssistant,
):
    router = _router(hass, role_map={10: "ha-a"}, entry_id="entry-1")
    guild = _guild(fetch=AsyncMock(side_effect=RuntimeError("discord down")))
    result = await router.role_ids_for(author=SimpleNamespace(id=7), guild=guild)
    assert result == frozenset()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_ROLE_RESOLUTION, "entry-1")
    )
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


async def test_no_repair_issue_without_role_mapping(hass: HomeAssistant):
    router = _router(hass, entry_id="entry-1")
    guild = _guild(fetch=AsyncMock(side_effect=RuntimeError("discord down")))
    result = await router.role_ids_for(author=SimpleNamespace(id=7), guild=guild)
    assert result == frozenset()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id(ISSUE_ROLE_RESOLUTION, "entry-1")
    )
    assert issue is None


async def test_repair_issue_cleared_by_later_success(hass: HomeAssistant):
    fetch = AsyncMock(side_effect=[RuntimeError("discord down"), _member(7, [10])])
    router = _router(hass, role_map={10: "ha-a"}, entry_id="entry-1")
    guild = _guild(fetch=fetch)
    author = SimpleNamespace(id=7)
    await router.role_ids_for(author=author, guild=guild)
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, issue_id(ISSUE_ROLE_RESOLUTION, "entry-1"))
        is not None
    )
    result = await router.role_ids_for(author=author, guild=guild)
    assert result == frozenset({10})
    assert (
        registry.async_get_issue(DOMAIN, issue_id(ISSUE_ROLE_RESOLUTION, "entry-1"))
        is None
    )


def test_ha_user_for_role_mapping_precedence():
    router = _router(
        object(),
        user_map={1: "ha-a"},
        role_map={10: "ha-role"},
        allowlist={2},
        fallback_user="ha-fb",
    )
    # User mapping beats role mapping.
    assert router.ha_user_for(1, role_ids={10}) == "ha-a"
    # Role mapping applies when there is no user mapping.
    assert router.ha_user_for(9, role_ids={10}) == "ha-role"
    # Unmatched roles do not authorize.
    assert router.ha_user_for(9, role_ids={99}) is None
    assert router.ha_user_for(9) is None
    # Allowlist plus fallback still works as before.
    assert router.ha_user_for(2, role_ids={99}) == "ha-fb"


def test_from_entry_parses_role_map(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_ROLE_MAP: {"10": "ha-a", "bad": "ha-b"}}
    )
    router = ConversationRouter.from_entry(hass, entry)
    assert router.role_map == {10: "ha-a"}
    assert router.role_mapping_configured is True
    assert router.entry_id == entry.entry_id
