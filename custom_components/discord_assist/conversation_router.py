"""Decision logic + HA conversation-agent dispatch for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.components.conversation import ConversationResult, async_converse
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.intent import IntentResponseErrorCode, IntentResponseType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGENT_ID,
    CONF_AGENT_MAP_CHANNEL,
    CONF_AGENT_MAP_USER,
    CONF_AGENT_TIMEOUT,
    CONF_ALLOWLIST,
    CONF_CHANNELS,
    CONF_CONVERSATION_SCOPE,
    CONF_EVENT_EXCLUDE_TEXT,
    CONF_FALLBACK_USER,
    CONF_IDLE_MINUTES,
    CONF_LANGUAGE,
    CONF_PREFER_LOCAL,
    CONF_RATE_LIMIT_BURST,
    CONF_RATE_LIMIT_PER_MINUTE,
    CONF_RESPOND_DMS,
    CONF_RESPOND_MENTIONS,
    CONF_ROOM_MAP,
    CONF_SPEAKER_CONTEXT,
    CONF_USER_MAP,
    CONVERSATION_SCOPE_USER,
    CONVERSATION_SCOPES,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_CONVERSATION_SCOPE,
    DEFAULT_EVENT_EXCLUDE_TEXT,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PREFER_LOCAL,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RESPOND_DMS,
    DEFAULT_RESPOND_MENTIONS,
    DEFAULT_SPEAKER_CONTEXT,
    LOCAL_AGENT_ID,
)
from .conversation_cache import (
    ConversationCache,
    RateLimitDecision,
    RateLimiter,
    TurnLocks,
)

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


class RateLimited(Exception):
    """A request was refused by the per-user rate limit before the agent ran.

    ``should_notify`` is True only for the first refusal in a throttle
    window: the bot sends one "Slow down" reply for that refusal and
    stays silent for the rest.
    """

    def __init__(self, should_notify: bool = False) -> None:
        super().__init__("Rate limit exceeded")
        self.should_notify = should_notify


@dataclass(frozen=True)
class MessageContext:
    """Where a message came from, beyond who sent it."""

    display_name: str | None = None
    channel_name: str | None = None
    channel_id: int | None = None
    parent_id: int | None = None


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


def _coerce_int_option(opts, key: str, default: int) -> int:
    try:
        return int(opts.get(key, default))
    except TypeError, ValueError:
        _LOGGER.warning("Ignoring non-numeric %s value: %r", key, opts.get(key))
        return default


def _clean_name(value: str | None) -> str | None:
    """Discord names are user-controlled: one line, no control chars, capped."""
    if not value:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return cleaned[:_MAX_NAME_LENGTH] or None


def make_conversation_key(
    *,
    is_dm: bool,
    channel_id: int,
    author_id: int,
    agent_id: str | None = None,
    scope: str = DEFAULT_CONVERSATION_SCOPE,
) -> str:
    """One conversation per DM (by user) or per channel, per agent.

    DMs are always per user. In a channel the scope decides: "channel"
    (the default) shares one conversation per channel, "user" gives each
    author their own conversation in the channel. The agent segment
    matters too: a conversation_id belongs to the agent that created it,
    so switching agents must never reuse another agent's id. Without an
    agent the key is the plain location key.
    """
    if is_dm:
        base = f"dm:{author_id}"
    elif scope == CONVERSATION_SCOPE_USER:
        base = f"channel:{channel_id}:user:{author_id}"
    else:
        base = f"channel:{channel_id}"
    return f"{base}:agent:{agent_id}" if agent_id else base


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
    agent_map_user: dict[int, str] = field(default_factory=dict)
    agent_map_channel: dict[int, str] = field(default_factory=dict)
    speaker_context: bool = DEFAULT_SPEAKER_CONTEXT
    prefer_local: bool = DEFAULT_PREFER_LOCAL
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT
    conversation_scope: str = DEFAULT_CONVERSATION_SCOPE
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST
    rate_limiter: RateLimiter | None = None
    turns: TurnLocks = field(default_factory=TurnLocks)
    config_entry_id: str | None = None
    event_exclude_text: bool = DEFAULT_EVENT_EXCLUDE_TEXT

    def __post_init__(self) -> None:
        if self.rate_limiter is None:
            self.rate_limiter = RateLimiter(
                self.rate_limit_per_minute, self.rate_limit_burst
            )

    @property
    def has_authorized_users(self) -> bool:
        """Whether anyone at all can talk to the bot with this configuration."""
        return bool(self.user_map) or (
            bool(self.allowlist) and self.fallback_user is not None
        )

    def ha_user_for(self, discord_user_id: int) -> str | None:
        """HA user a Discord user acts as, or None if they may not use the bot.

        Fails closed: a Discord user must be mapped or allowlisted, and must
        resolve to a real HA user. Nobody ever gets an identity-less Context,
        which Home Assistant treats as unrestricted.
        """
        if (
            discord_user_id not in self.user_map
            and discord_user_id not in self.allowlist
        ):
            return None
        return resolve_ha_user_id(self.user_map, self.fallback_user, discord_user_id)

    def device_for(self, channel_id: int | None, parent_id: int | None) -> str | None:
        """Device standing in for the room a channel (or its thread) represents."""
        if channel_id in self.room_map:
            return self.room_map[channel_id]
        if parent_id is not None:
            return self.room_map.get(parent_id)
        return None

    def agent_for(
        self,
        discord_user_id: int,
        channel_id: int | None = None,
        parent_id: int | None = None,
    ) -> str | None:
        """Conversation agent for a message: user, then channel, then default.

        A thread inherits its parent channel's agent, the same way it
        inherits the parent's room and listening setting. This is the single
        resolution point: permissions phase A (issue #12) should reuse it
        for its per-profile agent instead of duplicating the precedence.
        """
        if discord_user_id in self.agent_map_user:
            return self.agent_map_user[discord_user_id]
        if channel_id in self.agent_map_channel:
            return self.agent_map_channel[channel_id]
        if parent_id is not None:
            return self.agent_map_channel.get(parent_id, self.agent_id)
        return self.agent_id

    def should_respond(
        self,
        *,
        is_dm: bool,
        was_mentioned: bool,
        channel_id: int,
        author_id: int,
        parent_id: int | None = None,
    ) -> bool:
        if self.ha_user_for(author_id) is None:
            return False
        if is_dm:
            return self.respond_dms
        # A thread inherits its parent channel's listening setting.
        if channel_id in self.channels or parent_id in self.channels:
            return True
        if was_mentioned:
            return self.respond_mentions
        return False

    def conversation_key(
        self,
        *,
        is_dm: bool,
        channel_id: int,
        author_id: int,
        parent_id: int | None = None,
    ) -> str:
        """The conversation key for one request, carrying its resolved agent.

        Every entry point (messages, interactions) builds its key here, so
        the key always names the agent ``process`` will actually use and
        follows the configured conversation scope.
        """
        return make_conversation_key(
            is_dm=is_dm,
            channel_id=channel_id,
            author_id=author_id,
            agent_id=self.agent_for(author_id, channel_id, parent_id),
            scope=self.conversation_scope,
        )

    def check_rate_limit(self, discord_user_id: int) -> RateLimitDecision:
        """Consume one rate-limit token for a Discord user.

        Testable at router level: the decision says whether the request
        may proceed and, when it may not, whether this refusal is the
        one that should get a "Slow down" notice.
        """
        assert self.rate_limiter is not None  # set in __post_init__
        return self.rate_limiter.check(str(discord_user_id))

    async def process(
        self,
        *,
        text: str,
        discord_user_id: int,
        conversation_key: str,
        where: MessageContext | None = None,
        rate_checked: bool = False,
    ) -> str:
        """Run one authorized turn through the conversation agent.

        ``rate_checked`` is True when the caller already consumed this
        request's rate-limit token (the message path throttles before it
        starts typing); otherwise the limit is enforced here.
        """
        ha_user_id = self.ha_user_for(discord_user_id)
        if ha_user_id is None:
            raise PermissionError(f"Discord user {discord_user_id} is not authorized")
        if not rate_checked:
            decision = self.check_rate_limit(discord_user_id)
            if not decision.allowed:
                raise RateLimited(decision.should_notify)
        where = where or MessageContext()
        context = Context(user_id=ha_user_id)
        device_id = self.device_for(where.channel_id, where.parent_id)
        agent_id = self.agent_for(discord_user_id, where.channel_id, where.parent_id)

        async with self.turns.hold(conversation_key):
            try:
                async with asyncio.timeout(self.agent_timeout):
                    return await self._converse(
                        text, conversation_key, context, device_id, where, agent_id
                    )
            except TimeoutError as err:
                raise AgentTimeout from err

    async def _converse(
        self,
        text: str,
        conversation_key: str,
        context: Context,
        device_id: str | None,
        where: MessageContext,
        agent_id: str | None,
    ) -> str:
        # Prefer-local applies on top of the resolved agent: the local agent
        # is tried first unless the resolved agent already is the local one.
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
        scope = opts.get(CONF_CONVERSATION_SCOPE, DEFAULT_CONVERSATION_SCOPE)
        if scope not in CONVERSATION_SCOPES:
            _LOGGER.warning(
                "Ignoring unknown conversation scope %r; using %r",
                scope,
                DEFAULT_CONVERSATION_SCOPE,
            )
            scope = DEFAULT_CONVERSATION_SCOPE
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
            agent_map_user=_coerce_int_keys(
                opts.get(CONF_AGENT_MAP_USER), "agent Discord user id"
            ),
            agent_map_channel=_coerce_int_keys(
                opts.get(CONF_AGENT_MAP_CHANNEL), "agent channel id"
            ),
            speaker_context=opts.get(CONF_SPEAKER_CONTEXT, DEFAULT_SPEAKER_CONTEXT),
            prefer_local=opts.get(CONF_PREFER_LOCAL, DEFAULT_PREFER_LOCAL),
            agent_timeout=float(opts.get(CONF_AGENT_TIMEOUT, DEFAULT_AGENT_TIMEOUT)),
            conversation_scope=scope,
            config_entry_id=getattr(entry, "entry_id", None),
            event_exclude_text=bool(
                opts.get(CONF_EVENT_EXCLUDE_TEXT, DEFAULT_EVENT_EXCLUDE_TEXT)
            ),
            rate_limit_per_minute=_coerce_int_option(
                opts, CONF_RATE_LIMIT_PER_MINUTE, DEFAULT_RATE_LIMIT_PER_MINUTE
            ),
            rate_limit_burst=_coerce_int_option(
                opts, CONF_RATE_LIMIT_BURST, DEFAULT_RATE_LIMIT_BURST
            ),
        )
