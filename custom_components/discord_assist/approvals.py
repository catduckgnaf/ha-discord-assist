"""Approval buttons for sensitive actions (permissions phase C).

A request to run a sensitive service call does not execute. It posts
Approve / Deny buttons in a Discord channel and waits. Only the requester
or a named approver can press; approval runs the stored service call with
``Context(user_id=<requester's HA user>)`` so Home Assistant permissions
and the audit trail point at the requester, never at the approver.

Seam: on main, what a conversation agent does inside a turn is not
observable here (the router only gets speech text back from
``async_converse``), so the gate cannot hook a tool call directly yet.
The machinery is therefore exposed as the ``discord_assist.request_approval``
service: a caller (today an automation, later the scoped tools of phase B)
hands over the service call it wants gated, and this module owns the
record, the buttons, the press rules, and the audit event. When phase B
lands, its confirm-domain tools call the same manager instead of executing.

Scope resolution is deliberately minimal until profiles land (phase A):
the ``confirm_domains`` and ``approvers`` entry options stand in for the
per-profile ``confirm_domains`` / approver list, and ``ApprovalPolicy``
is where the profile fields plug in.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import discord
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPROVERS,
    CONF_CONFIRM_DOMAINS,
    DEFAULT_APPROVAL_TTL_MINUTES,
    DOMAIN,
    EVENT_APPROVAL,
)
from .conversation_router import ConversationRouter, _clean_name

_LOGGER = logging.getLogger(__name__)

APPROVE_PREFIX = "da:approve:"
DENY_PREFIX = "da:deny:"

# Domains where the requester may not confirm their own request by
# default: a second person has to press Approve. Mirrors the issue's
# profile flag (default no for locks and alarms); a request can override
# it explicitly, and profiles will own the flag once phase A lands.
NO_SELF_APPROVE_DOMAINS = frozenset({"lock", "alarm_control_panel"})

OUTCOME_REQUESTED = "requested"
OUTCOME_APPROVED = "approved"
OUTCOME_DENIED = "denied"
OUTCOME_EXPIRED = "expired"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

_STORAGE_VERSION = 1


def _coerce_int_set(values, field_name) -> set[int]:
    """Coerce an iterable to a set of ints, logging and skipping bad values."""
    result = set()
    for value in values or []:
        try:
            result.add(int(value))
        except TypeError, ValueError:
            _LOGGER.warning("Ignoring non-numeric %s value: %r", field_name, value)
    return result


@dataclass(frozen=True)
class ApprovalPolicy:
    """Which domains need confirmation, and who may approve.

    Minimal stand-in for the per-profile scope: built from the entry
    options today, from the speaker's profile once phase A lands.
    """

    confirm_domains: frozenset[str] = frozenset()
    approvers: frozenset[int] = frozenset()

    @classmethod
    def from_options(cls, options) -> ApprovalPolicy:
        domains = frozenset(
            str(domain).strip().lower()
            for domain in options.get(CONF_CONFIRM_DOMAINS) or []
            if str(domain).strip()
        )
        return cls(
            confirm_domains=domains,
            approvers=frozenset(
                _coerce_int_set(options.get(CONF_APPROVERS), "approver")
            ),
        )

    def requires_confirmation(self, domain: str) -> bool:
        """Whether a service call in this domain must wait for a press."""
        return domain.lower() in self.confirm_domains

    def self_approve_default(self, domain: str) -> bool:
        """Whether the requester may confirm their own request by default."""
        return domain.lower() not in NO_SELF_APPROVE_DOMAINS


@dataclass
class ApprovalRecord:
    """One pending approval: the gated call, who asked, who may confirm."""

    record_id: str
    channel_id: int
    domain: str
    service: str
    service_data: dict[str, Any]
    description: str
    content: str
    requester_discord_id: int
    requester_ha_user_id: str
    approver_discord_ids: frozenset[int]
    self_approve: bool
    created_at: datetime
    expires_at: datetime
    message_id: int | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "channel_id": self.channel_id,
            "domain": self.domain,
            "service": self.service,
            "service_data": self.service_data,
            "description": self.description,
            "content": self.content,
            "requester_discord_id": self.requester_discord_id,
            "requester_ha_user_id": self.requester_ha_user_id,
            "approver_discord_ids": sorted(self.approver_discord_ids),
            "self_approve": self.self_approve,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        return cls(
            record_id=data["record_id"],
            channel_id=int(data["channel_id"]),
            domain=data["domain"],
            service=data["service"],
            service_data=dict(data.get("service_data") or {}),
            description=data.get("description") or "",
            content=data.get("content") or "",
            requester_discord_id=int(data["requester_discord_id"]),
            requester_ha_user_id=data["requester_ha_user_id"],
            approver_discord_ids=frozenset(
                int(user_id) for user_id in data.get("approver_discord_ids") or []
            ),
            self_approve=bool(data.get("self_approve", True)),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            message_id=data.get("message_id"),
        )


class ApprovalStore:
    """Persistent, single-use approval records for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}.approvals.{entry_id}"
        )
        self._records: dict[str, ApprovalRecord] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        now = dt_util.utcnow()
        for raw in (data or {}).get("records", []):
            record = ApprovalRecord.from_dict(raw)
            # A restart never resurrects a request whose window has closed.
            if not record.is_expired(now):
                self._records[record.record_id] = record

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"records": [record.to_dict() for record in self._records.values()]}
        )

    def get(self, record_id: str) -> ApprovalRecord | None:
        return self._records.get(record_id)

    async def async_add(self, record: ApprovalRecord) -> None:
        self._records[record.record_id] = record
        await self._async_save()

    async def async_update(self, record: ApprovalRecord) -> None:
        self._records[record.record_id] = record
        await self._async_save()

    async def async_take(self, record_id: str) -> ApprovalRecord | None:
        """Remove and return a record. Single use: a second take gets None."""
        record = self._records.pop(record_id, None)
        if record is not None:
            await self._async_save()
        return record

    async def async_pop_expired(self, now: datetime) -> list[ApprovalRecord]:
        expired = [
            record for record in self._records.values() if record.is_expired(now)
        ]
        for record in expired:
            del self._records[record.record_id]
        if expired:
            await self._async_save()
        return expired


def build_approval_view(record: ApprovalRecord) -> discord.ui.View:
    """The Approve / Deny buttons for a record (persistent, no timeout)."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Approve",
            custom_id=f"{APPROVE_PREFIX}{record.record_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Deny",
            custom_id=f"{DENY_PREFIX}{record.record_id}",
        )
    )
    return view


class ApprovalManager:
    """Owns the approval lifecycle for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        router: ConversationRouter,
        store: ApprovalStore,
        policy: ApprovalPolicy,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.router = router
        self.store = store
        self.policy = policy
        self._client = None

    def bind_client(self, client) -> None:
        """Attach the Discord client used to post and retire messages."""
        self._client = client

    async def async_request_approval(
        self,
        *,
        channel_id: int,
        requester_discord_id: int,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        description: str | None = None,
        approvers: list[int] | None = None,
        self_approve: bool | None = None,
        ttl_minutes: float = DEFAULT_APPROVAL_TTL_MINUTES,
    ) -> ApprovalRecord:
        """Store a gated service call and post its Approve / Deny buttons.

        Fails closed: a requester who does not resolve to a Home Assistant
        user gets no record, no message, and no way to execute anything.
        """
        ha_user_id = self.router.ha_user_for(requester_discord_id)
        if ha_user_id is None:
            raise ServiceValidationError(
                f"Discord user {requester_discord_id} is not authorized to "
                "request approvals"
            )
        now = dt_util.utcnow()
        description = (description or "").strip()
        headline = description or f"Run {domain}.{service}?"
        record = ApprovalRecord(
            record_id=uuid.uuid4().hex,
            channel_id=channel_id,
            domain=domain,
            service=service,
            service_data=dict(service_data or {}),
            description=description,
            content=(
                f"{headline}\n\nWaiting for approval. Only the requester or "
                "an approver can confirm this action."
            ),
            requester_discord_id=requester_discord_id,
            requester_ha_user_id=ha_user_id,
            approver_discord_ids=self.policy.approvers
            | _coerce_int_set(approvers, "approver"),
            self_approve=self.policy.self_approve_default(domain)
            if self_approve is None
            else self_approve,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        await self.store.async_add(record)
        try:
            message = await self._post(record)
        except Exception:
            # No buttons, no pending request: leave nothing behind.
            await self.store.async_take(record.record_id)
            raise
        record.message_id = message.id
        await self.store.async_update(record)
        self._fire_audit(record, OUTCOME_REQUESTED, presser_discord_id=None)
        return record

    async def _post(self, record: ApprovalRecord):
        if self._client is None:
            raise ServiceValidationError("The Discord client is not available")
        channel = self._client.get_channel(record.channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(record.channel_id)
        return await channel.send(
            record.content,
            view=build_approval_view(record),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def async_handle_interaction(self, interaction) -> bool:
        """Handle one component interaction. True if it was an approval."""
        data = getattr(interaction, "data", None)
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str):
            return False
        if custom_id.startswith(APPROVE_PREFIX):
            action = "approve"
            record_id = custom_id[len(APPROVE_PREFIX) :]
        elif custom_id.startswith(DENY_PREFIX):
            action = "deny"
            record_id = custom_id[len(DENY_PREFIX) :]
        else:
            return False

        presser_id = interaction.user.id
        record = self.store.get(record_id)
        if record is None:
            await interaction.response.send_message(
                "This approval request has already been handled.", ephemeral=True
            )
            return True

        if record.is_expired(dt_util.utcnow()):
            await self.store.async_take(record_id)
            self._fire_audit(record, OUTCOME_EXPIRED, presser_discord_id=presser_id)
            await interaction.response.edit_message(
                content=f"{record.content}\n\nExpired without approval.",
                view=None,
            )
            return True

        if not self._may_press(record, presser_id):
            # A refused press never consumes the record.
            self._fire_audit(record, OUTCOME_REFUSED, presser_discord_id=presser_id)
            await interaction.response.send_message(
                self._refusal_text(record, presser_id), ephemeral=True
            )
            return True

        taken = await self.store.async_take(record_id)
        if taken is None:
            await interaction.response.send_message(
                "This approval request has already been handled.", ephemeral=True
            )
            return True

        presser_name = _clean_name(
            getattr(interaction.user, "display_name", None)
        ) or str(presser_id)
        if action == "deny":
            self._fire_audit(record, OUTCOME_DENIED, presser_discord_id=presser_id)
            await interaction.response.edit_message(
                content=f"{record.content}\n\nDenied by {presser_name}.",
                view=None,
            )
            return True

        await interaction.response.defer()
        try:
            await self.hass.services.async_call(
                record.domain,
                record.service,
                record.service_data,
                blocking=True,
                context=Context(user_id=record.requester_ha_user_id),
            )
        except Exception:  # noqa: BLE001 - report the failure, keep the audit
            _LOGGER.exception(
                "Approved action %s.%s failed", record.domain, record.service
            )
            self._fire_audit(record, OUTCOME_FAILED, presser_discord_id=presser_id)
            await interaction.edit_original_response(
                content=(
                    f"{record.content}\n\nApproved by {presser_name}, "
                    "but the action failed."
                ),
                view=None,
            )
            return True
        self._fire_audit(record, OUTCOME_APPROVED, presser_discord_id=presser_id)
        await interaction.edit_original_response(
            content=f"{record.content}\n\nApproved by {presser_name}.",
            view=None,
        )
        return True

    async def async_expire_sweep(self, _now: datetime | None = None) -> None:
        """Retire the buttons of every request whose window has closed."""
        expired = await self.store.async_pop_expired(dt_util.utcnow())
        for record in expired:
            self._fire_audit(record, OUTCOME_EXPIRED, presser_discord_id=None)
            await self._retire(record, "Expired without approval.")

    async def _retire(self, record: ApprovalRecord, outcome_line: str) -> None:
        """Best-effort edit removing a message's buttons.

        The record is already consumed, so a deleted message or a dead
        connection must not fail the sweep; the buttons are inert anyway.
        """
        if self._client is None or record.message_id is None:
            return
        try:
            channel = self._client.get_channel(record.channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(record.channel_id)
            message = await channel.fetch_message(record.message_id)
            await message.edit(
                content=f"{record.content}\n\n{outcome_line}",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not retire approval message %s", record.message_id)

    @staticmethod
    def _may_press(record: ApprovalRecord, presser_id: int) -> bool:
        if presser_id in record.approver_discord_ids:
            return True
        return presser_id == record.requester_discord_id and record.self_approve

    @staticmethod
    def _refusal_text(record: ApprovalRecord, presser_id: int) -> str:
        if presser_id == record.requester_discord_id:
            return (
                f"You cannot approve your own request in the {record.domain} "
                "domain; an approver has to confirm it."
            )
        return "Only the requester or an approver can respond to this request."

    def _fire_audit(
        self, record: ApprovalRecord, outcome: str, *, presser_discord_id: int | None
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_APPROVAL,
            {
                "config_entry_id": self.entry_id,
                "record_id": record.record_id,
                "domain": record.domain,
                "service": record.service,
                "channel_id": record.channel_id,
                "message_id": record.message_id,
                "requester_discord_id": record.requester_discord_id,
                "requester_ha_user_id": record.requester_ha_user_id,
                "presser_discord_id": presser_discord_id,
                "outcome": outcome,
            },
        )
