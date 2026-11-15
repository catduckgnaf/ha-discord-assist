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
CONF_PREFER_LOCAL = "prefer_local"
CONF_AGENT_TIMEOUT = "agent_timeout"
CONF_ROOM_MAP = "room_map"
CONF_CONFIRM_DOMAINS = "confirm_domains"
CONF_APPROVERS = "approvers"

DEFAULT_RESPOND_DMS = True
DEFAULT_RESPOND_MENTIONS = True
DEFAULT_IDLE_MINUTES = 15
DEFAULT_SPEAKER_CONTEXT = True
DEFAULT_PREFER_LOCAL = False
DEFAULT_AGENT_TIMEOUT = 60

# Home Assistant's built-in intent agent, tried first when prefer_local is on.
LOCAL_AGENT_ID = "conversation.home_assistant"

# Seconds to wait before reloading the entry after the gateway task dies.
RECONNECT_DELAY = 60

# Approval buttons for sensitive actions (permissions phase C).
EVENT_APPROVAL = "discord_assist_approval"
SERVICE_REQUEST_APPROVAL = "request_approval"
DEFAULT_APPROVAL_TTL_MINUTES = 5

ISSUE_NO_AUTHORIZED_USERS = "no_authorized_users"
ISSUE_MESSAGE_CONTENT_INTENT = "message_content_intent"


def signal_connection(entry_id: str) -> str:
    """Dispatcher signal fired when the gateway connection state changes."""
    return f"{DOMAIN}_connection_{entry_id}"


def issue_id(kind: str, entry_id: str) -> str:
    """Per-entry repair issue id."""
    return f"{kind}_{entry_id}"
