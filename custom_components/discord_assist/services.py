"""Service actions for Discord Assist (currently ``send_message``)."""

from __future__ import annotations

import io
import logging
import os
from typing import Any

import discord
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_SEND_MESSAGE
from .text_utils import chunk_message

_LOGGER = logging.getLogger(__name__)

# Friendly style names (plus the aliases Discord itself uses in its docs)
# mapped to discord.py button styles. "link" is deliberately absent: link
# buttons carry a URL instead of a custom_id, so they cannot report clicks.
_BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "blurple": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "green": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "red": discord.ButtonStyle.danger,
}

_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required("id"): cv.string,
        vol.Required("label"): cv.string,
        vol.Optional("style", default="primary"): cv.string,
    }
)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("target"): vol.Coerce(str),
        vol.Required("message"): cv.string,
        vol.Optional("image"): cv.string,
        vol.Optional("buttons"): [_BUTTON_SCHEMA],
    }
)


def _build_view(buttons: list[dict[str, Any]] | None) -> discord.ui.View | None:
    """Render the ``buttons`` field as a view with ``da:act:<id>`` custom ids.

    The view is self-contained on this branch (persistent, no timeout). The
    persistent dispatcher that routes clicks on these custom ids back into
    Home Assistant is a separate piece of work; until it lands, the buttons
    render and can be clicked, but no action is dispatched.
    """
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in buttons:
        style_name = str(button.get("style") or "primary").lower()
        style = _BUTTON_STYLES.get(style_name)
        if style is None:
            raise ServiceValidationError(
                f"Unknown button style: {button.get('style')}. "
                f"Use one of: {', '.join(sorted(set(_BUTTON_STYLES)))}"
            )
        view.add_item(
            discord.ui.Button(
                style=style,
                label=button["label"],
                custom_id=f"da:act:{button['id']}",
            )
        )
    return view


async def _async_discord_file(hass: HomeAssistant, image: str) -> discord.File:
    """Build a discord.File from a camera entity id or an allowed file path."""
    if image.startswith("camera.") and "/" not in image and "\\" not in image:
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
    if not await hass.async_add_executor_job(os.path.isfile, image):
        raise ServiceValidationError(f"Image file not found: {image}")
    return discord.File(image)


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
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            f"Discord Assist config entry is not loaded: {config_entry_id}"
        )
    runtime = getattr(entry, "runtime_data", None)
    client = getattr(runtime, "client", None)
    if client is None:
        raise ServiceValidationError(
            f"Discord Assist config entry is not loaded: {config_entry_id}"
        )

    message: str = data["message"]
    image: str | None = data.get("image")
    if not message.strip() and not image:
        raise ServiceValidationError("A message or an image is required")

    view = _build_view(data.get("buttons"))
    target_channel = await _resolve_target(client, str(data["target"]))

    discord_file = await _async_discord_file(hass, image) if image else None
    first_sent = None
    try:
        chunks = chunk_message(message) if message.strip() else [""]
        for index, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {
                "allowed_mentions": discord.AllowedMentions.none()
            }
            if index == len(chunks) - 1:
                if view is not None:
                    kwargs["view"] = view
                if discord_file is not None:
                    kwargs["file"] = discord_file
            try:
                sent = await target_channel.send(chunk, **kwargs)
            except discord.HTTPException as err:
                raise ServiceValidationError(
                    f"Failed to send the message to {data['target']}: {err}"
                ) from err
            if first_sent is None:
                first_sent = sent
    finally:
        if discord_file is not None:
            discord_file.close()

    return {"message_id": str(first_sent.id)}


def async_register_services(hass: HomeAssistant) -> None:
    """Register discord_assist.send_message once, however many entries load."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        return

    async def _handler(call: ServiceCall) -> ServiceResponse:
        return await _handle_send_message(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _handler,
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove discord_assist.send_message when the last entry unloads."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_MESSAGE)
