"""Re-read state before acting on a button press (issue #30).

A button posted minutes ago may act on a world that changed: "Close garage"
when someone already closed and reopened it. Each actionable button can
name the entities it acts on; their state is captured when the button is
posted and compared when it is pressed. Any difference refuses the press
and explains why.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant


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
    def from_dict(cls, data: Mapping[str, Any]) -> EntityExpectation:
        return cls(
            entity_id=str(data["entity_id"]),
            state=data.get("state"),
            last_changed=data.get("last_changed"),
        )


def capture_expectations(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> list[EntityExpectation]:
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
    return expectations


def find_state_changes(
    hass: HomeAssistant,
    expectations: Iterable[EntityExpectation],
    *,
    compare_last_changed: bool = True,
) -> list[str]:
    """Human-readable ways the world moved on since the button was posted.

    An empty list means every named entity is as it was. A changed state
    always counts. A changed ``last_changed`` with the same state (toggled
    and back) counts only when ``compare_last_changed`` is true: Home
    Assistant resets ``last_changed`` on restart, so for buttons posted
    before the current run only the state itself can be compared.
    """
    changes: list[str] = []
    for expected in expectations:
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
        elif (
            compare_last_changed
            and expected.last_changed is not None
            and current.last_changed != datetime.fromisoformat(expected.last_changed)
        ):
            changes.append(
                f"{expected.entity_id} changed in the meantime "
                f"(at {current.last_changed.isoformat(timespec='seconds')})"
            )
    return changes
