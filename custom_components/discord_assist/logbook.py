"""Describe discord_assist logbook events."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACTION


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""

    @callback
    def async_describe_logbook_event(event: Event) -> dict[str, str]:
        """Describe a press, e.g. 'Andrew pressed Unlock front door'."""
        name = event.data.get("discord_user_name") or "Someone"
        action = event.data.get("label") or event.data.get("action") or "a button"
        return {
            LOGBOOK_ENTRY_NAME: name,
            # The logbook shows the name first, so the message must not
            # repeat it ("Andrew pressed ...", not "Andrew Andrew pressed").
            LOGBOOK_ENTRY_MESSAGE: f"pressed {action}",
        }

    async_describe_event(DOMAIN, EVENT_ACTION, async_describe_logbook_event)
