"""Decision logic + HA conversation-agent dispatch for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import time as dt_time
from datetime import timedelta

from homeassistant.components.conversation import ConversationResult, async_converse
from homeassistant.components.logbook import async_log_entry as logbook_async_log_entry
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.intent import IntentResponseErrorCode, IntentResponseType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGENT_ID,
    CONF_AGENT_TIMEOUT,
    CONF_ALLOWLIST,
    CONF_CHANNELS,
    CONF_FALLBACK_USER,
    CONF_IDLE_MINUTES,
    CONF_LANGUAGE,
    CONF_PREFER_LOCAL,
    CONF_PROFILE_ROLE_MAP,
    CONF_PROFILE_USER_MAP,
    CONF_PROFILES,
    CONF_RESPOND_DMS,
    CONF_RESPOND_MENTIONS,
    CONF_ROOM_MAP,
    CONF_SPEAKER_CONTEXT,
    CONF_USER_MAP,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PREFER_LOCAL,
    DEFAULT_RESPOND_DMS,
    DEFAULT_RESPOND_MENTIONS,
    DEFAULT_SPEAKER_CONTEXT,
    DOMAIN,
    EVENT_REQUEST,
    LOCAL_AGENT_ID,
)
from .conversation_cache import ConversationCache, TurnLocks

_LOGGER = logging.getLogger(__name__)

# Local intent results that mean "I did not understand", so the LLM may try.
# Anything else (including a failed action) is final: retrying with the LLM
# could run the action twice.
_FALL_BACK_ON = frozenset(
    {IntentResponseErrorCode.NO_INTENT_MATCH, IntentResponseErrorCode.NO_VALID_TARGETS}
)
_MAX_NAME_LENGTH = 64


class AgentTimeout(Exception):
    """The conversation agent did not answer in time."""


@dataclass(frozen=True)
class MessageContext:
    """Where a message came from, beyond who sent it."""

    display_name: str | None = None
    channel_name: str | None = None
    channel_id: int | None = None
    parent_id: int | None = None


def _parse_hhmm(value: str) -> dt_time | None:
    try:
        hour, minute = (int(part) for part in value.strip().split(":", 1))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt_time(hour, minute)


def parse_quiet_hours(value: str | None) -> tuple[dt_time, dt_time] | None:
    """Parse a "21:00-07:00" quiet-hours window, or None if unset/invalid."""
    if not value:
        return None
    parts = str(value).split("-", 1)
    if len(parts) != 2:
        _LOGGER.warning("Ignoring invalid quiet_hours value: %r", value)
        return None
    start, end = _parse_hhmm(parts[0]), _parse_hhmm(parts[1])
    if start is None or end is None:
        _LOGGER.warning("Ignoring invalid quiet_hours value: %r", value)
        return None
    return (start, end)


@dataclass(frozen=True)
class Profile:
    """An access profile: who a Discord user acts as and where/when.

    Phase A enforces identity (``ha_user``), gatekeeping (``channels``,
    ``dms``, ``quiet_hours``, ``rate_limit``) and agent choice (``agent``).
    ``areas``, ``domains`` and ``confirm`` are stored now but only enforced
    in phases B and C (scoped tools and approval buttons); nothing in this
    phase reads them.

    A profile with ``ha_user`` set to None is a deny profile: anyone mapped
    to it is ignored, and a deny mapping beats any allow mapping.
    """

    name: str
    ha_user: str | None = None
    channels: frozenset[int] = frozenset()
    dms: bool = True
    quiet_hours: str | None = None
    rate_limit: int | None = None
    agent: str | None = None
    areas: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    confirm: tuple[str, ...] = ()

    @property
    def is_deny(self) -> bool:
        """A profile with no HA user denies access."""
        return self.ha_user is None

    def in_quiet_hours(self, at: dt_time) -> bool:
        """Whether a local time of day falls in the quiet window.

        The window may wrap past midnight ("21:00-07:00").
        """
        window = parse_quiet_hours(self.quiet_hours)
        if window is None:
            return False
        start, end = window
        if start <= end:
            return start <= at < end
        return at >= start or at < end


def _profile_from_dict(name: str, data: Mapping) -> Profile:
    rate_limit = data.get("rate_limit")
    try:
        rate_limit = int(rate_limit) if rate_limit not in (None, "") else None
    except TypeError, ValueError:
        _LOGGER.warning("Ignoring invalid rate_limit for profile %s", name)
        rate_limit = None
    return Profile(
        name=str(name),
        ha_user=data.get("ha_user") or None,
        channels=frozenset(
            _coerce_int_set(data.get("channels") or [], "profile channel")
        ),
        dms=bool(data.get("dms", True)),
        quiet_hours=data.get("quiet_hours") or None,
        rate_limit=rate_limit,
        agent=data.get("agent") or None,
        areas=tuple(str(v) for v in (data.get("areas") or [])),
        domains=tuple(str(v) for v in (data.get("domains") or [])),
        confirm=tuple(str(v) for v in (data.get("confirm") or [])),
    )


def profiles_from_options(raw) -> dict[str, Profile]:
    """Build Profile objects from the stored ``profiles`` option."""
    profiles: dict[str, Profile] = {}
    for name, data in (raw or {}).items():
        if isinstance(data, Mapping):
            profiles[str(name)] = _profile_from_dict(str(name), data)
        else:
            _LOGGER.warning("Ignoring non-mapping profile: %r", name)
    return profiles


def _coerce_int_set(values, field_name):
    """Coerce an iterable to a set of ints, logging and skipping non-numeric values."""
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except TypeError, ValueError:
            _LOGGER.warning("Ignoring non-numeric %s value: %r", field_name, value)
    return result


def _coerce_int_keys(mapping, field_name) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in (mapping or {}).items():
        try:
            result[int(key)] = value
        except TypeError, ValueError:
            _LOGGER.warning("Ignoring non-numeric %s key: %r", field_name, key)
    return result


def _clean_name(value: str | None) -> str | None:
    """Discord names are user-controlled: one line, no control chars, capped."""
    if not value:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return cleaned[:_MAX_NAME_LENGTH] or None


def make_conversation_key(*, is_dm: bool, channel_id: int, author_id: int) -> str:
    """One conversation per DM (by user) or per channel."""
    return f"dm:{author_id}" if is_dm else f"channel:{channel_id}"


def resolve_ha_user_id(
    user_map: dict[int, str], fallback_user: str | None, discord_user_id: int
) -> str | None:
    """Mapped HA user, else fallback HA user, else None (no identity)."""
    return user_map.get(discord_user_id) or fallback_user


def _speech(result: ConversationResult) -> str:
    speech = result.response.speech or {}
    return (speech.get("plain") or {}).get("speech") or ""


def _local_needs_fallback(result: ConversationResult) -> bool:
    response = result.response
    return (
        getattr(response, "response_type", None) == IntentResponseType.ERROR
        and getattr(response, "error_code", None) in _FALL_BACK_ON
    )


@dataclass
class ConversationRouter:
    """Filters Discord messages and dispatches them to an HA conversation agent."""

    hass: HomeAssistant
    agent_id: str | None
    language: str | None
    channels: set[int]
    respond_dms: bool
    respond_mentions: bool
    allowlist: set[int]
    user_map: dict[int, str]
    fallback_user: str | None
    cache: ConversationCache
    room_map: dict[int, str] = field(default_factory=dict)
    speaker_context: bool = DEFAULT_SPEAKER_CONTEXT
    prefer_local: bool = DEFAULT_PREFER_LOCAL
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT
    turns: TurnLocks = field(default_factory=TurnLocks)
    profiles: dict[str, Profile] = field(default_factory=dict)
    profile_user_map: dict[int, str] = field(default_factory=dict)
    profile_role_map: dict[int, str] = field(default_factory=dict)
    _rate_windows: dict[int, list[float]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def has_authorized_users(self) -> bool:
        """Whether anyone at all can talk to the bot with this configuration."""
        if bool(self.user_map) or (
            bool(self.allowlist) and self.fallback_user is not None
        ):
            return True
        assigned = list(self.profile_user_map.values()) + list(
            self.profile_role_map.values()
        )
        return any(
            (profile := self.profiles.get(name)) is not None and not profile.is_deny
            for name in assigned
        )

    def _legacy_profile(self, discord_user_id: int) -> Profile | None:
        """Implicit default profile synthesized from the legacy options.

        Entries with only ``user_map`` / ``allowlist`` / ``fallback_ha_user``
        behave exactly as before profiles existed: the profile carries the
        resolved HA user and no per-profile restrictions (empty channels
        means the global channel and mention rules apply, and ``dms``
        mirrors the global respond-to-DMs setting).
        """
        if (
            discord_user_id not in self.user_map
            and discord_user_id not in self.allowlist
        ):
            return None
        ha_user = resolve_ha_user_id(self.user_map, self.fallback_user, discord_user_id)
        if ha_user is None:
            return None
        return Profile(name="default", ha_user=ha_user, dms=self.respond_dms)

    def profile_for(
        self, discord_user_id: int, role_ids: Iterable[int] = ()
    ) -> Profile | None:
        """Profile a Discord user gets, or None if they may not use the bot.

        Assignment is by Discord user id first, then by Discord role id;
        a user mapping beats a role mapping. A deny profile (one with no
        HA user) beats any allow mapping, whether it is reached by user id
        or by role id. No match means no profile: the request is ignored
        (fail closed, as with the legacy options).
        """
        role_ids = list(role_ids or ())
        user_name = self.profile_user_map.get(discord_user_id)
        role_names = [
            self.profile_role_map[role_id]
            for role_id in role_ids
            if role_id in self.profile_role_map
        ]
        # Deny beats allow, from either kind of mapping.
        for name in [user_name, *role_names]:
            if name is not None:
                profile = self.profiles.get(name)
                if profile is not None and profile.is_deny:
                    return profile
        if user_name is not None:
            # A user mapping beats role mappings, even a dangling one.
            return self.profiles.get(user_name)
        for name in role_names:
            if name in self.profiles:
                return self.profiles[name]
        if role_names:
            return None
        return self._legacy_profile(discord_user_id)

    def ha_user_for(
        self, discord_user_id: int, role_ids: Iterable[int] = ()
    ) -> str | None:
        """HA user a Discord user acts as, or None if they may not use the bot.

        Kept for existing callers; the decision now goes through
        ``profile_for``. Fails closed: nobody ever gets an identity-less
        Context, which Home Assistant treats as unrestricted.
        """
        profile = self.profile_for(discord_user_id, role_ids)
        if profile is None or profile.is_deny:
            return None
        return profile.ha_user

    def _local_time(self) -> dt_time:
        """Current local time of day (split out so tests can pin it)."""
        return dt_util.now().time()

    def _rate_limit_exceeded(self, profile: Profile, discord_user_id: int) -> bool:
        """Whether the user is over the profile's per-minute rate limit."""
        if not profile.rate_limit:
            return False
        window = self._rate_windows.get(discord_user_id, [])
        cutoff = time.monotonic() - 60
        return len([stamp for stamp in window if stamp > cutoff]) >= profile.rate_limit

    def _record_rate_use(self, profile: Profile, discord_user_id: int) -> None:
        if not profile.rate_limit:
            return
        now = time.monotonic()
        window = [
            stamp
            for stamp in self._rate_windows.get(discord_user_id, [])
            if stamp > now - 60
        ]
        window.append(now)
        self._rate_windows[discord_user_id] = window

    def _audit(
        self,
        *,
        discord_user_id: int,
        profile: Profile | None,
        where: MessageContext,
        agent_id: str | None,
        outcome: str,
        context: Context | None,
    ) -> None:
        """Fire the audit event and, with a Context, write a logbook entry."""
        hass = self.hass
        if hass is None or not hasattr(hass, "bus"):
            return
        profile_name = profile.name if profile is not None else None
        try:
            hass.bus.async_fire(
                EVENT_REQUEST,
                {
                    "discord_user_id": discord_user_id,
                    "profile": profile_name,
                    "channel_id": where.channel_id,
                    "agent_id": agent_id,
                    "outcome": outcome,
                },
                context=context,
            )
            if context is not None:
                logbook_async_log_entry(
                    hass,
                    "Discord Assist",
                    f"Discord user {discord_user_id} (profile {profile_name}) "
                    f"in channel {where.channel_id}: {outcome}",
                    domain=DOMAIN,
                    context=context,
                )
        except Exception:  # noqa: BLE001 - audit must never break a request
            _LOGGER.exception("Failed to write Discord Assist audit entry")

    def device_for(self, channel_id: int | None, parent_id: int | None) -> str | None:
        """Device standing in for the room a channel (or its thread) represents."""
        if channel_id in self.room_map:
            return self.room_map[channel_id]
        if parent_id is not None:
            return self.room_map.get(parent_id)
        return None

    def should_respond(
        self,
        *,
        is_dm: bool,
        was_mentioned: bool,
        channel_id: int,
        author_id: int,
        parent_id: int | None = None,
        role_ids: Iterable[int] = (),
    ) -> bool:
        profile = self.profile_for(author_id, role_ids)
        if profile is None or profile.is_deny:
            return False
        # Gatekeeping (quiet hours, rate limit) runs before any agent call.
        # Blocked requests stay silent rather than posting a refusal.
        if profile.in_quiet_hours(self._local_time()):
            return False
        if self._rate_limit_exceeded(profile, author_id):
            return False
        if is_dm:
            return bool(profile.dms)
        if profile.channels:
            # A per-profile channel allowlist replaces the global one.
            return channel_id in profile.channels or parent_id in profile.channels
        # A thread inherits its parent channel's listening setting.
        if channel_id in self.channels or parent_id in self.channels:
            return True
        if was_mentioned:
            return self.respond_mentions
        return False

    async def process(
        self,
        *,
        text: str,
        discord_user_id: int,
        conversation_key: str,
        where: MessageContext | None = None,
        role_ids: Iterable[int] = (),
        is_dm: bool = False,
    ) -> str:
        where = where or MessageContext()
        profile = self.profile_for(discord_user_id, role_ids)
        if profile is None or profile.is_deny or profile.ha_user is None:
            self._audit(
                discord_user_id=discord_user_id,
                profile=profile,
                where=where,
                agent_id=self.agent_id,
                outcome="denied",
                context=None,
            )
            raise PermissionError(f"Discord user {discord_user_id} is not authorized")
        context = Context(user_id=profile.ha_user)
        agent_id = profile.agent or self.agent_id
        blocked = self._blocked_reason(profile, discord_user_id, where, is_dm)
        if blocked is not None:
            self._audit(
                discord_user_id=discord_user_id,
                profile=profile,
                where=where,
                agent_id=agent_id,
                outcome=blocked,
                context=context,
            )
            raise PermissionError(
                f"Discord user {discord_user_id} blocked by profile "
                f"{profile.name}: {blocked}"
            )
        self._record_rate_use(profile, discord_user_id)
        device_id = self.device_for(where.channel_id, where.parent_id)

        async with self.turns.hold(conversation_key):
            try:
                async with asyncio.timeout(self.agent_timeout):
                    reply = await self._converse(
                        text,
                        conversation_key,
                        context,
                        device_id,
                        where,
                        agent_id=agent_id,
                    )
            except TimeoutError as err:
                self._audit(
                    discord_user_id=discord_user_id,
                    profile=profile,
                    where=where,
                    agent_id=agent_id,
                    outcome="timeout",
                    context=context,
                )
                raise AgentTimeout from err
            except Exception:
                self._audit(
                    discord_user_id=discord_user_id,
                    profile=profile,
                    where=where,
                    agent_id=agent_id,
                    outcome="error",
                    context=context,
                )
                raise
        self._audit(
            discord_user_id=discord_user_id,
            profile=profile,
            where=where,
            agent_id=agent_id,
            outcome="answered",
            context=context,
        )
        return reply

    def _blocked_reason(
        self,
        profile: Profile,
        discord_user_id: int,
        where: MessageContext,
        is_dm: bool,
    ) -> str | None:
        """Gatekeeping outcome blocking this request, or None to allow it.

        Every check here runs before any agent call.
        """
        if is_dm and not profile.dms:
            return "dms_disabled"
        if (
            not is_dm
            and profile.channels
            and where.channel_id is not None
            and where.channel_id not in profile.channels
            and where.parent_id not in profile.channels
        ):
            return "channel_not_allowed"
        if profile.in_quiet_hours(self._local_time()):
            return "quiet_hours"
        if self._rate_limit_exceeded(profile, discord_user_id):
            return "rate_limit"
        return None

    async def _converse(
        self,
        text: str,
        conversation_key: str,
        context: Context,
        device_id: str | None,
        where: MessageContext,
        agent_id: str | None = None,
    ) -> str:
        agent_id = agent_id or self.agent_id
        if self.prefer_local and agent_id != LOCAL_AGENT_ID:
            # No conversation_id: the local agent is stateless here, and the
            # LLM's history is left alone.
            local = await async_converse(
                self.hass,
                text,
                None,
                context,
                language=self.language,
                agent_id=LOCAL_AGENT_ID,
                device_id=device_id,
            )
            if not _local_needs_fallback(local):
                return _speech(local)

        extra = (
            await self._speaker_prompt(context.user_id, device_id, where)
            if self.speaker_context
            else None
        )
        result = await async_converse(
            self.hass,
            text,
            self.cache.get(conversation_key),
            context,
            language=self.language,
            agent_id=agent_id,
            device_id=device_id,
            extra_system_prompt=extra,
        )
        self.cache.set(conversation_key, result.conversation_id)
        return _speech(result)

    async def _speaker_prompt(
        self, ha_user_id: str | None, device_id: str | None, where: MessageContext
    ) -> str:
        """Who is talking, from where, and when. Plain facts, no instructions."""
        lines = ["This request came from Discord."]
        speaker = _clean_name(where.display_name)
        ha_name = None
        if ha_user_id:
            user = await self.hass.auth.async_get_user(ha_user_id)
            ha_name = _clean_name(user.name) if user else None
        if speaker and ha_name:
            lines.append(f"Speaker: {speaker} (Home Assistant user {ha_name}).")
        elif speaker or ha_name:
            lines.append(f"Speaker: {speaker or ha_name}.")
        channel = _clean_name(where.channel_name)
        lines.append(f"Channel: #{channel}." if channel else "Channel: direct message.")
        area = self._area_name(device_id)
        if area:
            lines.append(
                f"The speaker is in the {area}. Requests that name no room or "
                f"device refer to the {area}."
            )
        lines.append(f"Local time: {dt_util.now().strftime('%A %Y-%m-%d %H:%M')}.")
        return "\n".join(lines)

    def _area_name(self, device_id: str | None) -> str | None:
        if not device_id:
            return None
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area else None

    @classmethod
    def from_entry(cls, hass: HomeAssistant, entry: ConfigEntry) -> ConversationRouter:
        opts = entry.options
        return cls(
            hass=hass,
            agent_id=opts.get(CONF_AGENT_ID),
            language=opts.get(CONF_LANGUAGE) or None,
            channels=_coerce_int_set(opts.get(CONF_CHANNELS, []), "channel"),
            respond_dms=opts.get(CONF_RESPOND_DMS, DEFAULT_RESPOND_DMS),
            respond_mentions=opts.get(CONF_RESPOND_MENTIONS, DEFAULT_RESPOND_MENTIONS),
            allowlist=_coerce_int_set(opts.get(CONF_ALLOWLIST, []), "allowlist id"),
            user_map=_coerce_int_keys(opts.get(CONF_USER_MAP), "Discord user id"),
            fallback_user=opts.get(CONF_FALLBACK_USER) or None,
            cache=ConversationCache(
                timedelta(minutes=opts.get(CONF_IDLE_MINUTES, DEFAULT_IDLE_MINUTES))
            ),
            room_map=_coerce_int_keys(opts.get(CONF_ROOM_MAP), "room channel id"),
            speaker_context=opts.get(CONF_SPEAKER_CONTEXT, DEFAULT_SPEAKER_CONTEXT),
            prefer_local=opts.get(CONF_PREFER_LOCAL, DEFAULT_PREFER_LOCAL),
            agent_timeout=float(opts.get(CONF_AGENT_TIMEOUT, DEFAULT_AGENT_TIMEOUT)),
            profiles=profiles_from_options(opts.get(CONF_PROFILES)),
            profile_user_map=_coerce_int_keys(
                opts.get(CONF_PROFILE_USER_MAP), "profile Discord user id"
            ),
            profile_role_map=_coerce_int_keys(
                opts.get(CONF_PROFILE_ROLE_MAP), "profile Discord role id"
            ),
        )
