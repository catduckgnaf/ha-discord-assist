"""Assist timer support for Discord channels.

Home Assistant's built-in timer intents only work for a device that has a
registered timer handler. Discord messages have no such device, so the
integration registers a handler for its own per-entry device (see
``__init__.py``) and the router passes that device for channels that are not
mapped to a room. Room-mapped channels keep their room device instead, so
room preference still applies there; if that device has its own timer
handler (for example a voice satellite), it handles its own timers.

The handler only knows a timer id and the device it was started on, never
which Discord channel or user asked for it. The router therefore records the
channel and user behind the in-flight request for the integration device
(``track_request``); when the handler reports the timer started, that origin
is stored under the timer id, and when the timer finishes the tracker posts
in the origin channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from homeassistant.components.intent import TimerEventType, TimerInfo
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_MAX_LABEL_LENGTH = 64

# channel_id, message text, Discord user id to mention (or None)
TimerPoster = Callable[[int, str, int | None], Awaitable[None]]


@dataclass(frozen=True)
class TimerOrigin:
    """The Discord channel and user a timer request came from."""

    channel_id: int
    discord_user_id: int


def _clean_label(value: str | None) -> str | None:
    """Timer names are user-controlled: one line, no control chars, capped."""
    if not value:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return cleaned[:_MAX_LABEL_LENGTH] or None


def finished_message(timer: TimerInfo) -> str:
    """Announcement for a finished timer, named after the timer or its area."""
    name = _clean_label(timer.name)
    if name:
        return f"{name} timer done"
    area = _clean_label(timer.area_name)
    if area:
        return f"{area[:1].upper() + area[1:]} timer done"
    return "Timer done"


class TimerTracker:
    """Correlates Assist timers with the Discord requests that started them."""

    def __init__(
        self, hass: HomeAssistant, poster: TimerPoster, *, timer_mention: bool = False
    ) -> None:
        self._hass = hass
        self._poster = poster
        self.timer_mention = timer_mention
        # Timer id -> where it was started from.
        self._timers: dict[str, TimerOrigin] = {}
        # Origins of requests currently being conversed, most recent last.
        # The timer handler fires synchronously while a request is in flight,
        # so the top of this stack is the request that started the timer.
        self._pending: list[TimerOrigin] = []

    @asynccontextmanager
    async def track_request(self, origin: TimerOrigin) -> AsyncIterator[None]:
        """Mark ``origin`` as the request in flight for timer correlation."""
        self._pending.append(origin)
        try:
            yield
        finally:
            try:
                self._pending.remove(origin)
            except ValueError:
                pass

    def handle_event(self, event_type: TimerEventType, timer: TimerInfo) -> None:
        """Timer handler registered with Home Assistant for the entry device."""
        if event_type in (TimerEventType.STARTED, TimerEventType.UPDATED):
            if timer.id not in self._timers and self._pending:
                self._timers[timer.id] = self._pending[-1]
        elif event_type == TimerEventType.CANCELLED:
            self._timers.pop(timer.id, None)
        elif event_type == TimerEventType.FINISHED:
            origin = self._timers.pop(timer.id, None)
            if origin is None:
                _LOGGER.debug(
                    "Timer %s finished with no Discord origin; not announcing",
                    timer.id,
                )
                return
            mention = origin.discord_user_id if self.timer_mention else None
            self._schedule(origin, finished_message(timer), mention)

    def _schedule(self, origin: TimerOrigin, text: str, mention: int | None) -> None:
        coro = self._post(origin, text, mention)
        create_task = getattr(self._hass, "async_create_background_task", None)
        if callable(create_task):
            create_task(coro, f"discord_assist-timer-{origin.channel_id}")
        else:
            asyncio.ensure_future(coro)

    async def _post(self, origin: TimerOrigin, text: str, mention: int | None) -> None:
        try:
            await self._poster(origin.channel_id, text, mention)
        except Exception:  # noqa: BLE001 - a failed announcement must not crash HA
            _LOGGER.exception(
                "Failed to announce finished timer in channel %s", origin.channel_id
            )
