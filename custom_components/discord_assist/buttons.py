"""Actionable notification buttons: persistent records, dispatch, sending.

This is the self-contained substrate for #24. The general button dispatcher
(#13) and the ``discord_assist.send_message`` service (#23) are separate
lanes; this module implements only what actionable notifications need, in
the shapes those lanes proposed, so a later rebase can converge them:

- Buttons carry a stateless ``custom_id`` of ``da:act:<record_id>``.
- The record behind the id is persisted in a Home Assistant ``Store`` (per
  config entry), so a button posted before a restart still works after it.
- ``ButtonRecordStore.async_take`` claims a record atomically: a single-use
  record (the default) is removed when claimed, so a second press gets
  nothing. A record created with ``multi_use=True`` survives ``take()``.
- ``ActionButtonDispatcher`` turns a press into exactly one
  ``discord_assist_action`` event, or refuses the press (no event) when the
  presser may not use the button.
- ``async_send_action_message`` is the minimal internal send helper: it
  records the buttons, builds a view, and posts the message. The
  ``send_message`` service from #23 is the intended caller; it only needs
  to resolve a channel and hand it to this helper.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import discord
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import ACTION_CUSTOM_ID_PREFIX, DOMAIN, EVENT_ACTION

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1

# Discord caps a component custom_id at 100 characters.
_MAX_CUSTOM_ID_LENGTH = 100

_BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}


def make_custom_id(record_id: str) -> str:
    """custom_id for an actionable button record."""
    return f"{ACTION_CUSTOM_ID_PREFIX}{record_id}"


def parse_custom_id(custom_id: str | None) -> str | None:
    """Record id from a ``da:act:<record_id>`` custom_id, else None.

    Anything without our prefix belongs to another feature or bot and is
    ignored by the dispatcher.
    """
    if not custom_id or not custom_id.startswith(ACTION_CUSTOM_ID_PREFIX):
        return None
    record_id = custom_id[len(ACTION_CUSTOM_ID_PREFIX) :]
    return record_id or None


@dataclass(frozen=True)
class ButtonRecord:
    """What an actionable button does and who may press it."""

    record_id: str
    action: str
    config_entry_id: str
    label: str | None = None
    allowed_discord_user_ids: tuple[int, ...] = ()
    multi_use: bool = False
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "action": self.action,
            "config_entry_id": self.config_entry_id,
            "label": self.label,
            "allowed_discord_user_ids": list(self.allowed_discord_user_ids),
            "multi_use": self.multi_use,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ButtonRecord:
        return cls(
            record_id=str(data["record_id"]),
            action=str(data["action"]),
            config_entry_id=str(data["config_entry_id"]),
            label=data.get("label"),
            allowed_discord_user_ids=tuple(
                int(value) for value in data.get("allowed_discord_user_ids") or ()
            ),
            multi_use=bool(data.get("multi_use", False)),
            created_at=float(data.get("created_at") or time.time()),
            expires_at=data.get("expires_at"),
        )


class ButtonRecordStore:
    """Persistent store of actionable button records for one config entry."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str) -> None:
        self.hass = hass
        self.config_entry_id = config_entry_id
        self._store: Store = Store(
            hass, _STORE_VERSION, f"{DOMAIN}.buttons.{config_entry_id}"
        )
        self._records: dict[str, ButtonRecord] | None = None
        self._lock = asyncio.Lock()

    async def _async_ensure_loaded(self) -> dict[str, ButtonRecord]:
        """Load persisted records once. Caller must hold the lock."""
        if self._records is None:
            raw = await self._store.async_load() or {}
            records: dict[str, ButtonRecord] = {}
            for item in raw.get("records") or []:
                try:
                    record = ButtonRecord.from_dict(item)
                except KeyError, TypeError, ValueError:
                    _LOGGER.warning("Skipping malformed button record: %r", item)
                    continue
                if not record.is_expired():
                    records[record.record_id] = record
            self._records = records
        return self._records

    async def _async_save(self) -> None:
        assert self._records is not None
        await self._store.async_save(
            {"records": [record.to_dict() for record in self._records.values()]}
        )

    def _prune_expired(self) -> bool:
        assert self._records is not None
        expired = [rid for rid, rec in self._records.items() if rec.is_expired()]
        for rid in expired:
            del self._records[rid]
        return bool(expired)

    async def async_record(
        self,
        action: str,
        *,
        label: str | None = None,
        allowed_discord_user_ids: tuple[int, ...] | list[int] = (),
        multi_use: bool = False,
        record_id: str | None = None,
        ttl_seconds: float | None = None,
    ) -> ButtonRecord:
        """Create and persist a button record; return it."""
        record_id = record_id or secrets.token_urlsafe(8)
        custom_id = make_custom_id(record_id)
        if len(custom_id) > _MAX_CUSTOM_ID_LENGTH:
            raise ValueError(
                f"custom_id for record {record_id!r} exceeds "
                f"{_MAX_CUSTOM_ID_LENGTH} characters"
            )
        now = time.time()
        record = ButtonRecord(
            record_id=record_id,
            action=action,
            config_entry_id=self.config_entry_id,
            label=label,
            allowed_discord_user_ids=tuple(
                int(value) for value in allowed_discord_user_ids
            ),
            multi_use=multi_use,
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds is not None else None,
        )
        async with self._lock:
            records = await self._async_ensure_loaded()
            self._prune_expired()
            records[record.record_id] = record
            await self._async_save()
        return record

    async def async_peek(self, record_id: str) -> ButtonRecord | None:
        """Record without claiming it, or None if unknown or expired."""
        async with self._lock:
            records = await self._async_ensure_loaded()
            record = records.get(record_id)
            if record is not None and record.is_expired():
                del records[record_id]
                await self._async_save()
                return None
            return record

    async def async_take(self, record_id: str) -> ButtonRecord | None:
        """Claim a record for one press.

        Single-use records are removed and persisted, so a second claim (a
        second press, even after a restart) returns None. Multi-use records
        are returned without being removed. Expired records are pruned and
        return None.
        """
        async with self._lock:
            records = await self._async_ensure_loaded()
            record = records.get(record_id)
            if record is None:
                return None
            if record.is_expired():
                del records[record_id]
                await self._async_save()
                return None
            if not record.multi_use:
                del records[record_id]
                await self._async_save()
            return record


@dataclass(frozen=True)
class ButtonSpec:
    """One button an automation wants on an outbound message."""

    action: str
    label: str
    style: str = "primary"
    allowed_discord_user_ids: tuple[int, ...] = ()
    multi_use: bool = False


class ActionButtonDispatcher:
    """Turns actionable button presses into ``discord_assist_action`` events.

    Authorization fails closed, in this order:

    - Unknown, expired or already-claimed records are refused.
    - If the record names allowed pressers, anyone else is refused and the
      record is NOT claimed, so an allowed presser can still press it. A
      presser the record names explicitly is allowed even without a Home
      Assistant mapping; their event then carries ``ha_user_id: None``.
    - Otherwise (no named pressers) the presser must resolve to a Home
      Assistant user via ``ha_user_for`` (the router's mapping plus
      allowlist/fallback). Unmapped pressers are refused by default and the
      record is not claimed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: ButtonRecordStore,
        *,
        config_entry_id: str,
        ha_user_for: Callable[[int], str | None],
    ) -> None:
        self.hass = hass
        self.store = store
        self.config_entry_id = config_entry_id
        self._ha_user_for = ha_user_for

    async def async_handle_press(
        self,
        *,
        record_id: str,
        discord_user_id: int,
        discord_user_name: str | None,
        message_id: int | None,
        channel_id: int | None,
    ) -> bool:
        """Handle one press. True when exactly one event was fired."""
        record = await self.store.async_peek(record_id)
        if record is None:
            _LOGGER.debug("Ignoring press for unknown button record %s", record_id)
            return False

        ha_user_id = self._ha_user_for(discord_user_id)
        if record.allowed_discord_user_ids:
            if discord_user_id not in record.allowed_discord_user_ids:
                _LOGGER.warning(
                    "Discord user %s pressed button %s they are not allowed to use",
                    discord_user_id,
                    record.action,
                )
                return False
        elif ha_user_id is None:
            _LOGGER.warning(
                "Discord user %s pressed button %s but has no Home Assistant "
                "user mapping; refusing",
                discord_user_id,
                record.action,
            )
            return False

        # Claim only after authorization, so a refused press never burns a
        # single-use button. async_take is atomic, so a concurrent second
        # press on a single-use record gets None here and fires nothing.
        claimed = await self.store.async_take(record_id)
        if claimed is None:
            return False

        data: dict = {
            "action": claimed.action,
            "message_id": message_id,
            "channel_id": channel_id,
            "discord_user_id": discord_user_id,
            "discord_user_name": discord_user_name,
            "ha_user_id": ha_user_id,
            "config_entry_id": claimed.config_entry_id or self.config_entry_id,
        }
        if claimed.label:
            data["label"] = claimed.label
        context = Context(user_id=ha_user_id) if ha_user_id else Context()
        self.hass.bus.async_fire(EVENT_ACTION, data, context=context)
        return True

    async def async_handle_interaction(self, interaction: discord.Interaction) -> bool:
        """Handle a discord.py interaction; ignore non-action custom_ids."""
        data = interaction.data if isinstance(interaction.data, dict) else {}
        record_id = parse_custom_id(data.get("custom_id"))
        if record_id is None:
            return False
        user = interaction.user
        message = getattr(interaction, "message", None)
        fired = await self.async_handle_press(
            record_id=record_id,
            discord_user_id=user.id,
            discord_user_name=getattr(user, "display_name", None)
            or getattr(user, "name", None),
            message_id=getattr(message, "id", None),
            channel_id=getattr(interaction, "channel_id", None)
            or (message.channel.id if message is not None else None),
        )
        await _async_acknowledge(interaction, fired=fired)
        return fired


async def _async_acknowledge(interaction: discord.Interaction, *, fired: bool) -> None:
    """Best-effort interaction response; a failed ack never undoes a press.

    Firing the event is a fast local call, well inside Discord's response
    window. A handled press defers (the button message stays as-is); a
    refused press gets an ephemeral explanation. The acknowledge-first
    helper from #14 can replace this once that lane lands.
    """
    try:
        response = interaction.response
        if response.is_done():
            return
        if fired:
            await response.defer()
        else:
            await response.send_message(
                "This button is no longer available, or it is not for you.",
                ephemeral=True,
            )
    except Exception:  # noqa: BLE001 - acking must never raise into the gateway
        _LOGGER.debug("Failed to acknowledge button interaction", exc_info=True)


async def async_build_view(
    store: ButtonRecordStore, buttons: list[ButtonSpec]
) -> discord.ui.View:
    """Record each button and build a persistent view carrying their ids."""
    view = discord.ui.View(timeout=None)
    for spec in buttons:
        record = await store.async_record(
            spec.action,
            label=spec.label,
            allowed_discord_user_ids=spec.allowed_discord_user_ids,
            multi_use=spec.multi_use,
        )
        view.add_item(
            discord.ui.Button(
                label=spec.label,
                custom_id=make_custom_id(record.record_id),
                style=_BUTTON_STYLES.get(spec.style, discord.ButtonStyle.primary),
            )
        )
    return view


async def async_send_action_message(
    store: ButtonRecordStore,
    channel,
    content: str,
    buttons: list[ButtonSpec] | None = None,
):
    """Post a message, optionally with actionable buttons, to a channel.

    Minimal internal send helper for #24: the ``send_message`` service
    (#23) resolves the target channel and calls this, then reads the id of
    the returned message. Agent/automation text is untrusted, so mentions
    are always suppressed here.
    """
    view = await async_build_view(store, buttons) if buttons else None
    return await channel.send(
        content, view=view, allowed_mentions=discord.AllowedMentions.none()
    )
