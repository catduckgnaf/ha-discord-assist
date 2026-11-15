"""Deterministic camera snapshot command.

A snapshot is only ever attached when this code fetched it, in this turn,
for a camera that is exposed to the conversation assistant and that the
mapped Home Assistant user may read. Text from the conversation agent is
never inspected for attachment instructions, file paths, or URLs, so model
output cannot cause any file to be attached.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

ASSISTANT = "conversation"
SNAPSHOT_TIMEOUT = 10
SNAPSHOT_FILENAME = "snapshot.jpg"
# Discord rejects very large uploads. Above this size, ask the camera for a
# downscaled image instead of trying to upload the original.
LARGE_IMAGE_BYTES = 8 * 1024 * 1024
DOWNSCALE_WIDTH = 1280

_SNAPSHOT_RE = re.compile(r"^snapshot\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_SHOW_ME_RE = re.compile(
    r"^show\s+me\s+(?:the\s+|a\s+|an\s+)?(.*)$", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class SnapshotResult:
    """Text reply plus, only on success, image bytes this code fetched.

    ``matched`` says the query matched at least one allowed camera (or
    ambiguously matched several). The bot uses it for ``show me``: a
    show-me request that matches no camera is not a snapshot request at
    all, so the bot lets the conversation agent answer it instead.
    """

    text: str
    image_content: bytes | None = None
    filename: str = SNAPSHOT_FILENAME
    matched: bool = False


def _parse(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.match(text.strip())
    if match:
        return match.group(1).strip().strip(".!?").strip()
    return None


def parse_snapshot_command(text: str) -> str | None:
    """Return the camera query for ``snapshot <camera>``, or None.

    An empty query is returned as ``""`` so the caller can refuse it as
    a snapshot attempt instead of sending it to the conversation agent.
    """
    return _parse(_SNAPSHOT_RE, text)


def parse_show_me_command(text: str) -> str | None:
    """Return the camera query for ``show me <camera>``, or None."""
    return _parse(_SHOW_ME_RE, text)


def _normalize(value: str) -> str:
    """Normalize a camera name or query for comparison."""
    normalized = re.sub(r"[_\-]+", " ", value.lower())
    normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if normalized.startswith("the "):
        normalized = normalized[4:]
    return normalized


def _aliases(state: State) -> set[str]:
    """Normalized names a camera may be referred to by."""
    aliases: set[str] = set()
    object_id = state.entity_id.partition(".")[2]
    candidates = [state.entity_id, object_id, state.name]
    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize(candidate)
        if not normalized:
            continue
        aliases.add(normalized)
        # "driveway" should match a camera named "Driveway Camera".
        if normalized.endswith(" camera"):
            aliases.add(normalized[: -len(" camera")])
    return aliases


def _allowed_cameras(hass: HomeAssistant, user) -> list[State]:
    """Camera states exposed to Assist that the user may read.

    Fails closed: a camera is left out when its exposure cannot be
    confirmed or when the user lacks POLICY_READ on it.
    """
    allowed: list[State] = []
    for state in hass.states.async_all("camera"):
        try:
            if not async_should_expose(hass, ASSISTANT, state.entity_id):
                continue
        except Exception:  # noqa: BLE001 - exposure unknown: fail closed
            _LOGGER.debug(
                "Could not confirm exposure for %s; skipping", state.entity_id
            )
            continue
        try:
            if not user.permissions.check_entity(state.entity_id, POLICY_READ):
                continue
        except Exception:  # noqa: BLE001 - permission unknown: fail closed
            continue
        allowed.append(state)
    return allowed


@dataclass(frozen=True)
class _Resolution:
    state: State | None
    refusal: str | None
    matched: bool


def _resolve(hass: HomeAssistant, user, query: str) -> _Resolution:
    cameras = _allowed_cameras(hass, user)
    normalized = _normalize(query)
    if not normalized:
        return _Resolution(None, "Which camera? Try: snapshot <camera name>", False)
    # An exact entity id is unambiguous even if names overlap.
    for state in cameras:
        if query.strip().lower() == state.entity_id:
            return _Resolution(state, None, True)
    exact = [s for s in cameras if normalized in _aliases(s)]
    matches = exact or [
        s
        for s in cameras
        if any(normalized in alias or alias in normalized for alias in _aliases(s))
    ]
    if len(matches) == 1:
        return _Resolution(matches[0], None, True)
    if len(matches) > 1:
        names = ", ".join(sorted(s.name for s in matches))
        return _Resolution(
            None,
            f"More than one camera matches '{query}': {names}. Be more specific.",
            True,
        )
    return _Resolution(
        None,
        f"I could not find a camera matching '{query}' that is exposed "
        "to Assist and that you are allowed to view.",
        False,
    )


def resolve_camera(hass: HomeAssistant, user, query: str) -> State | str:
    """Resolve a query to one allowed camera state, or a refusal string.

    Cameras that are not exposed or not readable are indistinguishable
    from cameras that do not exist, so their existence is never revealed.
    """
    resolution = _resolve(hass, user, query)
    return resolution.state if resolution.state is not None else resolution.refusal


async def _fetch_image(
    hass: HomeAssistant,
    entity_id: str,
    *,
    timeout: int,
    width: int | None,
    height: int | None,
):
    """Fetch one camera image.

    The camera component is imported lazily: it is not a dependency of
    this integration, and importing it at module load would pull in its
    own requirements (for example turbojpeg) even for users and tests
    with no cameras at all.
    """
    from homeassistant.components.camera import async_get_image

    return await async_get_image(
        hass, entity_id, timeout=timeout, width=width, height=height
    )


async def fetch_snapshot(hass: HomeAssistant, user, query: str) -> SnapshotResult:
    """Resolve, authorize, and fetch one snapshot. Never raises for refusals."""
    resolution = _resolve(hass, user, query)
    if resolution.state is None:
        return SnapshotResult(
            text=resolution.refusal or "Which camera?", matched=resolution.matched
        )
    resolved = resolution.state
    entity_id = resolved.entity_id
    try:
        image = await _fetch_image(
            hass, entity_id, timeout=SNAPSHOT_TIMEOUT, width=None, height=None
        )
        content = image.content
        if content and len(content) > LARGE_IMAGE_BYTES:
            try:
                smaller = await _fetch_image(
                    hass,
                    entity_id,
                    timeout=SNAPSHOT_TIMEOUT,
                    width=DOWNSCALE_WIDTH,
                    height=None,
                )
                if smaller.content:
                    content = smaller.content
            except Exception:  # noqa: BLE001 - keep the original image
                _LOGGER.debug("Downscaled snapshot fetch failed for %s", entity_id)
    except Exception as err:  # noqa: BLE001 - a failed camera must not crash the bot
        _LOGGER.warning("Snapshot failed for %s: %s", entity_id, err)
        return SnapshotResult(
            text=f"Sorry, I could not get a snapshot from {resolved.name}.",
            matched=True,
        )
    if not content:
        return SnapshotResult(
            text=f"Sorry, I could not get a snapshot from {resolved.name}.",
            matched=True,
        )
    return SnapshotResult(
        text=f"Snapshot from {resolved.name}.", image_content=content, matched=True
    )
