"""Per-Discord-user standing preferences ("notes"), persisted with HA storage.

Notes are user-supplied text that is injected into the conversation agent's
extra system prompt, scoped to the speaker who wrote them. They are never
permissions and never change who a user may act as; access control stays in
``ConversationRouter.ha_user_for``.
"""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

MAX_NOTES_PER_USER = 10
MAX_NOTE_LENGTH = 200
STORE_VERSION = 1


class NoteLimitError(Exception):
    """The user already has the maximum number of notes."""


def clean_note(value: str | None) -> str | None:
    """Clean a note the way display names are cleaned in the router.

    Control characters (including newlines) are replaced with a space so a
    note is always one line and cannot forge extra prompt lines, the result
    is stripped, and it is capped at ``MAX_NOTE_LENGTH`` characters.
    """
    if not value:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return cleaned[:MAX_NOTE_LENGTH] or None


def parse_notes_command(text: str) -> tuple[str, str] | None:
    """Parse a notes text command, or return None if the text is not one.

    Returns ``("add", raw_note)``, ``("list", "")`` or
    ``("forget", raw_index)``. Matching is case-insensitive for the command
    word; the note text keeps its original case.
    """
    stripped = text.strip()
    lower = stripped.lower()
    if lower == "notes":
        return ("list", "")
    if lower.startswith("remember:"):
        return ("add", stripped[len("remember:") :])
    if lower == "forget note" or lower.startswith("forget note "):
        return ("forget", stripped[len("forget note") :].strip())
    return None


class NotesStore:
    """Standing preferences for one config entry, keyed by Discord user id."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, STORE_VERSION, f"discord_assist.{entry_id}.notes"
        )
        self._data: dict[str, list[str]] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load notes from storage, cleaning whatever was stored."""
        raw = await self._store.async_load()
        data: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, list):
                    continue
                notes = [cleaned for item in value if (cleaned := clean_note(item))]
                if notes:
                    data[str(key)] = notes[:MAX_NOTES_PER_USER]
        self._data = data
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.async_load()

    async def async_all_notes(self) -> dict[int, list[str]]:
        """Every user's notes, keyed by Discord user id."""
        await self._ensure_loaded()
        result: dict[int, list[str]] = {}
        for key, notes in self._data.items():
            try:
                result[int(key)] = list(notes)
            except ValueError:
                continue
        return result

    async def async_notes_for(self, discord_user_id: int) -> list[str]:
        """One user's notes (a copy), in the order they were added."""
        await self._ensure_loaded()
        return list(self._data.get(str(discord_user_id), []))

    async def async_add_note(self, discord_user_id: int, raw_note: str) -> str:
        """Clean and store a note, returning the cleaned text.

        Raises ValueError if the note is empty after cleaning, and
        NoteLimitError if the user already has ``MAX_NOTES_PER_USER`` notes.
        """
        cleaned = clean_note(raw_note)
        if cleaned is None:
            raise ValueError("Note is empty after cleaning")
        await self._ensure_loaded()
        notes = self._data.setdefault(str(discord_user_id), [])
        if len(notes) >= MAX_NOTES_PER_USER:
            raise NoteLimitError(
                f"Discord user {discord_user_id} already has {MAX_NOTES_PER_USER} notes"
            )
        notes.append(cleaned)
        await self._store.async_save(self._data)
        return cleaned

    async def async_remove_note(self, discord_user_id: int, index: int) -> str | None:
        """Remove the note at 1-based ``index``; return it, or None if absent."""
        await self._ensure_loaded()
        notes = self._data.get(str(discord_user_id))
        if not notes or not 1 <= index <= len(notes):
            return None
        removed = notes.pop(index - 1)
        if not notes:
            del self._data[str(discord_user_id)]
        await self._store.async_save(self._data)
        return removed
