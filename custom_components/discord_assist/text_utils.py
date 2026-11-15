"""Pure text helpers for Discord message formatting."""

from __future__ import annotations

import re

MAX_MESSAGE_LENGTH = 2000
FENCE = "```"
_CLOSER = "\n" + FENCE
# Opening fence lines carry a language tag; cap it so the reopen prefix stays small.
_MAX_OPENER = 24


def chunk_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into <=limit pieces, preferring line boundaries.

    A split inside a ``` code block closes the block at the end of one piece
    and reopens it (with the same language tag) at the start of the next, so
    Discord renders every piece correctly.
    """
    chunks: list[str] = []
    buffer = ""
    prefix = ""  # reopened fence carried into the current piece
    fence: str | None = None  # opener line of the block we are inside

    def flush(in_fence: str | None) -> None:
        nonlocal buffer, prefix
        if not buffer:
            return
        out = prefix + buffer
        if in_fence is not None:
            out = out.rstrip("\n") + _CLOSER
        chunks.append(out)
        buffer = ""
        prefix = in_fence + "\n" if in_fence is not None else ""

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            after = stripped[:_MAX_OPENER] if fence is None else None
        else:
            after = fence
        budget = limit - (len(_CLOSER) if after is not None else 0)

        if len(prefix) + len(buffer) + len(line) > budget:
            flush(fence)
        while len(prefix) + len(line) > budget:
            cut = max(1, budget - len(prefix))
            buffer, line = line[:cut], line[cut:]
            flush(fence)
        buffer += line
        fence = after

    flush(fence)
    return chunks or [""]


def strip_self_mention(content: str, bot_user_id: int) -> str:
    """Remove the bot's own mention (<@id> or <@!id>) from raw message content."""
    return re.sub(rf"<@!?{bot_user_id}>", "", content).strip()
