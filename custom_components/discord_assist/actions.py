"""Actionable notification buttons (issue #24).

A message can carry buttons that, when pressed, fire one
``discord_assist_action`` event, like the mobile app's actionable
notifications. The buttons are ordinary records in the shared button store
(``buttons.py``) with the ``act`` action; the client's dispatcher has
already authorized the presser (an HA identity, and named on the record if
it names anyone) and claimed the record before :func:`async_handle_action`
runs, so a refused press never fires anything and never burns the button.

A button may name the entities it acts on (``entity_ids``): their state is
captured when it is posted and re-read on press (``state_check.py``), and a
press against a world that moved on is refused and explained instead.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import discord
from homeassistant.core import Context, HomeAssistant

from .buttons import ButtonOutcome, ButtonRecord, ButtonSpec, async_send_with_buttons
from .const import EVENT_ACTION
from .state_check import (
    EntityExpectation,
    capture_expectations,
    find_state_changes,
)

if TYPE_CHECKING:
    from .bot import DiscordAssistClient

ACTION = "act"

# Link buttons need a URL and no custom_id; Discord rejects the whole
# message otherwise, so they are not offered.
BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def action_specs(
    buttons: Iterable[Mapping[str, Any]], hass: HomeAssistant | None = None
) -> list[ButtonSpec]:
    """Button specs for actionable buttons described as plain mappings.

    Each mapping has ``id`` (the action automations match on), ``label``,
    and optionally ``style``, ``allowed_discord_user_ids``, ``multi_use``
    and ``entity_ids`` (whose state is captured now, with ``hass``, and
    re-checked on press).
    """
    specs: list[ButtonSpec] = []
    for button in buttons:
        action_id = str(button["id"])
        label = str(button.get("label") or action_id)
        payload: dict[str, Any] = {"action_id": action_id, "label": label}
        entity_ids = [str(e) for e in button.get("entity_ids", ())]
        if entity_ids and hass is not None:
            payload["expectations"] = [
                e.to_dict() for e in capture_expectations(hass, entity_ids)
            ]
        specs.append(
            ButtonSpec(
                label=label,
                action=ACTION,
                payload=payload,
                style=BUTTON_STYLES.get(
                    str(button.get("style", "primary")), discord.ButtonStyle.primary
                ),
                allowed_user_ids=tuple(
                    int(value) for value in button.get("allowed_discord_user_ids", ())
                ),
                multi_use=bool(button.get("multi_use", False)),
            )
        )
    return specs


async def async_send_action_message(
    client: DiscordAssistClient,
    channel: Any,
    content: str,
    buttons: Iterable[Mapping[str, Any]] = (),
    **send_kwargs: Any,
) -> list[Any]:
    """Post a message with actionable buttons; returns the sent messages.

    The buttons ride on the last message. Sends go through the client's
    recording send path, so the duplicate-token check knows them as ours.
    """
    if client.button_store is None:
        raise RuntimeError("This client has no button store configured")

    async def send(part: str, **kwargs: Any) -> Any:
        return await client.send_to_channel(channel, part, **kwargs)

    return await async_send_with_buttons(
        client.button_store,
        channel,
        content,
        action_specs(buttons, client.hass),
        send=send,
        **send_kwargs,
    )


def make_action_handler(
    hass: HomeAssistant, client: DiscordAssistClient, entry_id: str
):
    """The ``act`` button handler: re-check state, then fire as the presser."""
    # Buttons recorded before this run predate the last restart, which
    # resets last_changed, so for them only the state can be compared.
    started = time.time()

    async def handle(record: ButtonRecord, interaction: Any) -> ButtonOutcome:
        expectations = [
            EntityExpectation.from_dict(raw)
            for raw in record.payload.get("expectations", ())
        ]
        changes = find_state_changes(
            hass, expectations, compare_last_changed=record.created_at >= started
        )
        if changes:
            return ButtonOutcome(
                ephemeral="Nothing changed: "
                + "; ".join(changes)
                + ". The state already moved on, so nothing was done.",
                outcome="Not done: the state changed since this was posted.",
            )
        user = interaction.user
        # The dispatcher only runs handlers for pressers with an identity.
        ha_user_id = client.router.ha_user_for(user.id)
        message = getattr(interaction, "message", None)
        channel_id = getattr(interaction, "channel_id", None) or getattr(
            getattr(message, "channel", None), "id", None
        )
        name = getattr(user, "display_name", None) or getattr(user, "name", None)
        label = record.payload.get("label")
        message_id = getattr(message, "id", None)
        data: dict[str, Any] = {
            "action": record.payload.get("action_id"),
            # Snowflakes as strings, like discord_assist_message, so the
            # frontend cannot round them.
            "message_id": None if message_id is None else str(message_id),
            "channel_id": None if channel_id is None else str(channel_id),
            "discord_user_id": str(user.id),
            "discord_user_name": name,
            "ha_user_id": ha_user_id,
            "config_entry_id": entry_id,
        }
        if label:
            data["label"] = label
        hass.bus.async_fire(EVENT_ACTION, data, context=Context(user_id=ha_user_id))
        if record.multi_use:
            return ButtonOutcome(ephemeral=f"Sent: {label}", retire=False)
        return ButtonOutcome(outcome=f"{name or 'Someone'} pressed {label}")

    return handle
