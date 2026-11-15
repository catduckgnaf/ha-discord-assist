"""Persistent action buttons that re-check entity state on press.

Roadmap suggestion 25 (issue #30): a button posted minutes ago may act on a
world that changed ("Close garage" after someone already closed and reopened
it). Each button record therefore stores the entity ids and the state (and
``last_changed``) it was posted against. On press, the dispatcher re-reads
those entities from ``hass.states`` before firing the action event; if
anything changed, the press is refused, no event fires, and the buttons are
retired from the message.

This is the smallest self-contained substrate for that behavior: a persistent
record store plus a press handler. The general button dispatcher (#13) and
the actionable-notification events (#24) are separate lanes; when they land,
their dispatcher should call :func:`find_state_changes` (or this handler)
before running an action, and records created there should carry the same
expectations this module stores.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_BUTTON_ACTION

if TYPE_CHECKING:
    from .conversation_router import ConversationRouter

_LOGGER = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "da:act:"
STORAGE_VERSION = 1
DEFAULT_TTL = timedelta(hours=1)

ALREADY_HANDLED_REPLY = "That button has already been used or has expired."
NOT_AUTHORIZED_REPLY = "You are not authorized to use this button."


@dataclass(frozen=True)
class EntityExpectation:
    """The state one entity was in when a button was posted."""

    entity_id: str
    state: str | None
    last_changed: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "last_changed": self.last_changed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityExpectation:
        return cls(
            entity_id=data["entity_id"],
            state=data.get("state"),
            last_changed=data.get("last_changed"),
        )


@dataclass
class ButtonRecord:
    """One posted action button and the world it was posted against."""

    record_id: str
    action: str
    config_entry_id: str
    channel_id: int | None = None
    message_id: int | None = None
    expectations: tuple[EntityExpectation, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "action": self.action,
            "config_entry_id": self.config_entry_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "expectations": [exp.to_dict() for exp in self.expectations],
            "data": self.data,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ButtonRecord:
        return cls(
            record_id=data["record_id"],
            action=data["action"],
            config_entry_id=data["config_entry_id"],
            channel_id=data.get("channel_id"),
            message_id=data.get("message_id"),
            expectations=tuple(
                EntityExpectation.from_dict(exp) for exp in data.get("expectations", [])
            ),
            data=dict(data.get("data") or {}),
            expires_at=data.get("expires_at"),
        )

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        expires = dt_util.parse_datetime(self.expires_at)
        return expires is not None and expires <= dt_util.utcnow()


def custom_id_for(record_id: str) -> str:
    """Discord custom_id carrying a button record id."""
    return f"{CUSTOM_ID_PREFIX}{record_id}"


def record_id_from_custom_id(custom_id: str | None) -> str | None:
    """Record id out of a custom_id, or None for other components."""
    if not custom_id or not custom_id.startswith(CUSTOM_ID_PREFIX):
        return None
    return custom_id[len(CUSTOM_ID_PREFIX) :] or None


def capture_expectations(
    hass: HomeAssistant, entity_ids: list[str]
) -> tuple[EntityExpectation, ...]:
    """Snapshot the current state of each entity a button will act on."""
    expectations = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        expectations.append(
            EntityExpectation(
                entity_id=entity_id,
                state=state.state if state is not None else None,
                last_changed=(
                    state.last_changed.isoformat() if state is not None else None
                ),
            )
        )
    return tuple(expectations)


def make_record(
    hass: HomeAssistant,
    *,
    action: str,
    config_entry_id: str,
    entity_ids: list[str],
    channel_id: int | None = None,
    message_id: int | None = None,
    data: dict[str, Any] | None = None,
    ttl: timedelta | None = DEFAULT_TTL,
) -> ButtonRecord:
    """Build a record for a button about to be posted, snapshotting state."""
    return ButtonRecord(
        record_id=uuid.uuid4().hex,
        action=action,
        config_entry_id=config_entry_id,
        channel_id=channel_id,
        message_id=message_id,
        expectations=capture_expectations(hass, entity_ids),
        data=dict(data or {}),
        expires_at=(dt_util.utcnow() + ttl).isoformat() if ttl else None,
    )


def find_state_changes(hass: HomeAssistant, record: ButtonRecord) -> list[str]:
    """Human-readable ways the world moved on since the button was posted.

    An empty list means every entity the record names is exactly as it was:
    same state string and same ``last_changed``. An entity that no longer
    exists counts as changed.
    """
    changes: list[str] = []
    for expected in record.expectations:
        current = hass.states.get(expected.entity_id)
        if current is None:
            if expected.state is not None:
                changes.append(f"{expected.entity_id} no longer exists")
            continue
        if expected.state is None:
            changes.append(f"{expected.entity_id} appeared (now {current.state})")
        elif current.state != expected.state:
            changes.append(
                f"{expected.entity_id} is now {current.state} (was {expected.state})"
            )
        elif current.last_changed.isoformat() != expected.last_changed:
            changes.append(
                f"{expected.entity_id} changed again at "
                f"{current.last_changed.isoformat()}"
            )
    return changes


class ButtonStore:
    """Persistent, single-use store for button records, one per config entry."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.buttons.{config_entry_id}"
        )
        self._records: dict[str, ButtonRecord] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load persisted records (buttons survive a restart until pressed)."""
        data = await self._store.async_load()
        self._records = {}
        for raw in (data or {}).get("records", []):
            try:
                record = ButtonRecord.from_dict(raw)
            except KeyError, TypeError, ValueError:
                _LOGGER.warning("Ignoring malformed button record: %r", raw)
                continue
            if not record.expired:
                self._records[record.record_id] = record
        self._loaded = True

    async def _async_ensure_loaded(self) -> None:
        if not self._loaded:
            await self.async_load()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"records": [record.to_dict() for record in self._records.values()]}
        )

    async def async_add(self, record: ButtonRecord) -> None:
        """Persist a record for a button that was just posted."""
        await self._async_ensure_loaded()
        self._records[record.record_id] = record
        await self._async_save()

    def get(self, record_id: str) -> ButtonRecord | None:
        """The record for an id, without consuming it."""
        return self._records.get(record_id)

    async def async_take(self, record_id: str) -> ButtonRecord | None:
        """Consume a record: it can be acted on exactly once.

        Returns None when the record is unknown or already expired; an
        expired record is still removed so it cannot be pressed later.
        """
        await self._async_ensure_loaded()
        record = self._records.pop(record_id, None)
        if record is None:
            return None
        await self._async_save()
        return None if record.expired else record


async def _ephemeral(interaction: Any, text: str) -> None:
    """Reply so only the presser sees it, before or after acknowledging."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _retire(interaction: Any) -> None:
    """Remove the buttons from the pressed message."""
    message = getattr(interaction, "message", None)
    if message is None:
        return
    try:
        await message.edit(view=None)
    except Exception:  # noqa: BLE001 - retiring is best-effort
        _LOGGER.warning("Failed to retire buttons from a message", exc_info=True)


async def async_handle_button_press(
    hass: HomeAssistant,
    *,
    store: ButtonStore,
    router: ConversationRouter,
    interaction: Any,
) -> None:
    """Handle one action-button press, re-checking state before firing.

    The record is consumed either way (single-use). When the state moved on,
    no event fires: the presser is told ephemerally that nothing changed and
    the buttons are retired from the message.
    """
    record_id = record_id_from_custom_id(
        (getattr(interaction, "data", None) or {}).get("custom_id")
    )
    if record_id is None:
        return
    record = await store.async_take(record_id)
    if record is None:
        await _ephemeral(interaction, ALREADY_HANDLED_REPLY)
        return

    presser = interaction.user
    ha_user_id = router.ha_user_for(presser.id)
    if ha_user_id is None:
        # Fail closed: an unmapped presser never fires an action.
        await _ephemeral(interaction, NOT_AUTHORIZED_REPLY)
        return

    changes = find_state_changes(hass, record)
    if changes:
        await _retire(interaction)
        await _ephemeral(
            interaction,
            "Nothing changed: "
            + "; ".join(changes)
            + ". The state already moved on, so nothing was done "
            "and the buttons were retired.",
        )
        return

    # Acknowledge first (the 3 second interaction deadline), then fire.
    if not interaction.response.is_done():
        await interaction.response.defer()
    event_data = {
        **record.data,
        "action": record.action,
        "message_id": record.message_id,
        "channel_id": record.channel_id,
        "discord_user_id": presser.id,
        "discord_user_name": getattr(presser, "display_name", None)
        or getattr(presser, "name", None),
        "ha_user_id": ha_user_id,
        "config_entry_id": record.config_entry_id,
    }
    hass.bus.async_fire(
        EVENT_BUTTON_ACTION, event_data, context=Context(user_id=ha_user_id)
    )
    await _retire(interaction)
