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

DEFAULT_RESPOND_DMS = True
DEFAULT_RESPOND_MENTIONS = True
DEFAULT_IDLE_MINUTES = 15

# Seconds to wait before reloading the entry after the gateway task dies.
RECONNECT_DELAY = 60

ISSUE_NO_AUTHORIZED_USERS = "no_authorized_users"
ISSUE_MESSAGE_CONTENT_INTENT = "message_content_intent"


def signal_connection(entry_id: str) -> str:
    """Dispatcher signal fired when the gateway connection state changes."""
    return f"{DOMAIN}_connection_{entry_id}"


def issue_id(kind: str, entry_id: str) -> str:
    """Per-entry repair issue id."""
    return f"{kind}_{entry_id}"
