"""Bounded ring buffer of recent errors, safe to expose in diagnostics.

Only the exception type, a source label, and a timestamp are stored. The
exception message is deliberately never kept: it can contain the text of a
user's Discord message, a token, or other content that must not leak into a
diagnostics download.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.util import dt as dt_util

MAX_RECENT_ERRORS = 20


@dataclass(frozen=True)
class RecentError:
    """One recorded error: when, what kind, and where it happened."""

    timestamp: datetime
    error_type: str
    source: str

    def as_dict(self) -> dict[str, str]:
        """Serialize for diagnostics (no message text, by construction)."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "error_type": self.error_type,
            "source": self.source,
        }


class RecentErrors:
    """Last ``maxlen`` errors, oldest first; older ones fall off the end."""

    def __init__(
        self,
        maxlen: int = MAX_RECENT_ERRORS,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._errors: deque[RecentError] = deque(maxlen=maxlen)
        self._now = now

    def record(self, err: BaseException, *, source: str) -> None:
        """Remember that ``err`` happened at ``source``; never its message."""
        self._errors.append(
            RecentError(
                timestamp=self._now(),
                error_type=type(err).__name__,
                source=source,
            )
        )

    def __len__(self) -> int:
        return len(self._errors)

    def snapshot(self) -> list[dict[str, str]]:
        """All buffered errors as plain dicts, oldest first."""
        return [error.as_dict() for error in self._errors]
