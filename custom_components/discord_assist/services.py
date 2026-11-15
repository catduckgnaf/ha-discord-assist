"""Service actions for Discord Assist (currently ``send_message``).

Actions are registered once in ``async_setup``, so they exist (and fail
with a clear error) even while no entry is loaded, and automations using
them validate at startup.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import discord
import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_SEND_MESSAGE

_LOGGER = logging.getLogger(__name__)

# Friendly style names (plus the aliases Discord itself uses in its docs).
# "link" is deliberately absent: link buttons carry a URL instead of a
# custom_id, so they cannot report clicks.
_BUTTON_STYLES = {
    "primary": "primary",
    "blurple": "primary",
    "secondary": "secondary",
    "grey": "secondary",
    "gray": "secondary",
    "success": "success",
    "green": "success",
    "danger": "danger",
    "red": "danger",
}

# Discord allows at most 25 buttons per message and 80 characters a label.
_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(cv.string, vol.Length(min=1, max=100)),
        vol.Required("label"): vol.All(cv.string, vol.Length(min=1, max=80)),
        vol.Optional("style", default="primary"): vol.All(
            cv.string, vol.Lower, vol.In(_BUTTON_STYLES)
        ),
        vol.Optional("allowed_discord_user_ids"): [vol.Coerce(int)],
        vol.Optional("multi_use", default=False): cv.boolean,
        vol.Optional("entity_ids"): cv.entity_ids,
    }
)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("target"): vol.Coerce(str),
        vol.Required("message"): cv.string,
        vol.Optional("image"): cv.string,
        vol.Optional("buttons"): vol.All([_BUTTON_SCHEMA], vol.Length(max=25)),
    }
)


async def _async_check_camera_access(
    hass: HomeAssistant, call: ServiceCall, entity_id: str
) -> None:
    """A user calling the action may only post cameras they may read."""
    user_id = call.context.user_id
    if user_id is None:
        return  # automations and scripts run as the system
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.permissions.check_entity(entity_id, POLICY_READ):
        raise Unauthorized(context=call.context, entity_id=entity_id)


async def _async_discord_file(
    hass: HomeAssistant, call: ServiceCall, image: str
) -> discord.File:
    """Build a discord.File from a camera entity id or an allowed file path."""
    if image.startswith("camera.") and "/" not in image and "\\" not in image:
        await _async_check_camera_access(hass, call, image)
        try:
            from homeassistant.components.camera import async_get_image
        except Exception as err:  # noqa: BLE001 - camera component unavailable
            raise ServiceValidationError(
                f"Could not load the camera integration to fetch {image}"
            ) from err
        try:
            camera_image = await async_get_image(hass, image)
        except Exception as err:  # noqa: BLE001 - surfaced as a service error
            raise ServiceValidationError(
                f"Could not get an image from {image}: {err}"
            ) from err
        content_type = getattr(camera_image, "content_type", "") or "image/jpeg"
        extension = content_type.split("/")[-1].split(";")[0].strip() or "jpg"
        if extension == "jpeg":
            extension = "jpg"
        return discord.File(
            io.BytesIO(camera_image.content), filename=f"camera.{extension}"
        )

    if not hass.config.is_allowed_path(image):
        raise ServiceValidationError(
            f"Image path is not in an allowed directory: {image}. "
            "Add the directory to allowlist_external_dirs to use it."
        )
    path = Path(image)

    def _read() -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    # Read off the event loop; discord.File(path) would open and read the
    # file inside it.
    content = await hass.async_add_executor_job(_read)
    if content is None:
        raise ServiceValidationError(f"Image file not found: {image}")
    return discord.File(io.BytesIO(content), filename=path.name)


async def _resolve_target(client: Any, target: str) -> Any:
    """Resolve a target id to a sendable channel, opening a DM for users."""
    if not target.isdigit():
        raise ServiceValidationError(
            f"Target must be a Discord channel ID or user ID (digits only): {target}"
        )
    target_id = int(target)

    channel = client.get_channel(target_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(target_id)
        except discord.NotFound:
            channel = None
        except discord.HTTPException as err:
            raise ServiceValidationError(
                f"Target {target} is not reachable: {err}"
            ) from err
    if channel is not None:
        if not callable(getattr(channel, "send", None)):
            raise ServiceValidationError(
                f"Target {target} is not a channel messages can be sent to"
            )
        return channel

    user = client.get_user(target_id)
    if user is None:
        try:
            user = await client.fetch_user(target_id)
        except discord.HTTPException as err:
            raise ServiceValidationError(
                f"Target {target} is not reachable: {err}"
            ) from err
    if user is None:
        raise ServiceValidationError(f"Target {target} is not reachable")
    try:
        return await user.create_dm()
    except discord.HTTPException as err:
        raise ServiceValidationError(
            f"Could not open a direct message with {target}: {err}"
        ) from err


async def _handle_send_message(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Handle one discord_assist.send_message call."""
    data = call.data
    config_entry_id: str = data["config_entry_id"]
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError(
            f"Discord Assist config entry is not loaded: {config_entry_id}"
        )
    client = getattr(getattr(entry, "runtime_data", None), "client", None)
    if client is None:
        raise ServiceValidationError(
            f"Discord Assist config entry is not loaded: {config_entry_id}"
        )

    message: str = data["message"]
    image: str | None = data.get("image")
    if not message.strip() and not image:
        raise ServiceValidationError("A message or an image is required")
    buttons = [
        {**button, "style": _BUTTON_STYLES[button["style"]]}
        for button in data.get("buttons") or ()
    ]
    if len({button["id"] for button in buttons}) != len(buttons):
        raise ServiceValidationError("Button ids must be unique within a message")

    target_channel = await _resolve_target(client, str(data["target"]))
    send_kwargs: dict[str, Any] = {}
    if image:
        send_kwargs["file"] = await _async_discord_file(hass, call, image)
    try:
        sent = await client.async_send_action_message(
            target_channel, message, buttons, **send_kwargs
        )
    except discord.HTTPException as err:
        raise ServiceValidationError(
            f"Failed to send the message to {data['target']}: {err}"
        ) from err
    finally:
        if "file" in send_kwargs:
            send_kwargs["file"].close()

    # The last message carries the buttons and the image, so it is the one
    # a later automation would edit or retire.
    return {
        "message_id": str(sent[-1].id),
        "message_ids": [str(message.id) for message in sent],
        "channel_id": str(target_channel.id),
    }


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's actions (once, from async_setup)."""

    async def _send(call: ServiceCall) -> ServiceResponse:
        return await _handle_send_message(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _send,
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
