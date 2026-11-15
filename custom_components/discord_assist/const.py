"""Constants for the Discord Assist integration."""

DOMAIN = "discord_assist"

# The upstream integration this hard fork replaces; its entries can be imported.
LEGACY_DOMAIN = "discord_conversation"

# Entry data (secret / identity)
CONF_TOKEN = "token"
CONF_BOT_ID = "bot_id"

# Entry options (editable)
CONF_AGENT_ID = "agent_id"
CONF_CHANNELS = "channels"
CONF_RESPOND_DMS = "respond_dms"
CONF_RESPOND_MENTIONS = "respond_mentions"
CONF_IDLE_MINUTES = "idle_minutes"
CONF_LANGUAGE = "language"
CONF_ALLOWLIST = "allowlist"
CONF_USER_MAP = "user_map"
CONF_FALLBACK_USER = "fallback_ha_user"
CONF_SPEAKER_CONTEXT = "speaker_context"
CONF_EVENT_EXCLUDE_TEXT = "event_exclude_text"
CONF_PREFER_LOCAL = "prefer_local"
CONF_AGENT_TIMEOUT = "agent_timeout"
CONF_PLACEHOLDER_DELAY = "placeholder_delay"
CONF_ROOM_MAP = "room_map"
# Per-HA-user tool scopes for the scoped LLM API (permissions phase B).
# Shape: {ha_user_id: {"areas": [...], "domains": [...], "entities": [...],
# "deny_entities": [...], "read_only": bool}} - see llm_api.resolve_scope.
CONF_LLM_SCOPES = "llm_scopes"

# The LLM API this integration registers for Discord-facing agents.
LLM_API_ID = "discord_assist"
LLM_API_NAME = "Discord Assist"
CONF_TIMER_MENTION = "timer_mention"
CONF_CHANNEL_IDLE_MINUTES = "channel_idle_minutes"
CONF_CONVERSATION_SCOPE = "conversation_scope"

# Conversation keying in channels: one shared conversation per channel, or
# one per user in each channel. DMs are always keyed per user.
CONVERSATION_SCOPE_CHANNEL = "channel"
CONVERSATION_SCOPE_USER = "user"
CONVERSATION_SCOPES = (CONVERSATION_SCOPE_CHANNEL, CONVERSATION_SCOPE_USER)
CONF_AGENT_MAP_USER = "agent_map_user"
CONF_AGENT_MAP_CHANNEL = "agent_map_channel"
CONF_RATE_LIMIT_PER_MINUTE = "rate_limit_per_minute"
CONF_RATE_LIMIT_BURST = "rate_limit_burst"

DEFAULT_RESPOND_DMS = True
DEFAULT_RESPOND_MENTIONS = True
DEFAULT_IDLE_MINUTES = 15
DEFAULT_SPEAKER_CONTEXT = True
DEFAULT_EVENT_EXCLUDE_TEXT = False
DEFAULT_PREFER_LOCAL = False

# Event fired on the HA bus once per accepted Discord message.
EVENT_MESSAGE = f"{DOMAIN}_message"
DEFAULT_AGENT_TIMEOUT = 60
DEFAULT_TIMER_MENTION = False
DEFAULT_CONVERSATION_SCOPE = CONVERSATION_SCOPE_CHANNEL
DEFAULT_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_RATE_LIMIT_BURST = 3
DEFAULT_PLACEHOLDER_DELAY = 3.0

# Home Assistant's built-in intent agent, tried first when prefer_local is on.
LOCAL_AGENT_ID = "conversation.home_assistant"

# Seconds to wait before reloading the entry after the gateway task dies.
RECONNECT_DELAY = 60

SERVICE_SEND_MESSAGE = "send_message"

ISSUE_NO_AUTHORIZED_USERS = "no_authorized_users"
ISSUE_MESSAGE_CONTENT_INTENT = "message_content_intent"
ISSUE_DUPLICATE_TOKEN = "duplicate_token"

# Event fired when an actionable notification button is pressed (#24).
EVENT_ACTION = "discord_assist_action"


def signal_connection(entry_id: str) -> str:
    """Dispatcher signal fired when the gateway connection state changes."""
    return f"{DOMAIN}_connection_{entry_id}"


def issue_id(kind: str, entry_id: str) -> str:
    """Per-entry repair issue id."""
    return f"{kind}_{entry_id}"
