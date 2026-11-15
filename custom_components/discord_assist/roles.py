"""Reading a Discord member's roles without the Server Members intent.

Decision (docs/ROADMAP.md, issue #37): role-based mapping does NOT require
the privileged Server Members intent. Roles come from, in order:

1. The member payload on the message itself (``message.author.roles`` when
   the author is a guild member). No extra call.
2. ``guild.fetch_member(user_id)`` on demand, cached briefly per guild and
   user, for when the payload carries no roles (plain ``User`` author, or a
   partial member without a role list).

Resolution fails closed: any error means "no roles", never an exception and
never a guess. ``RoleResolution.resolved`` is False only when this was a
guild message whose roles we genuinely could not determine, which is the
signal the router uses to raise the role-resolution repair issue.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

ROLE_CACHE_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class RoleResolution:
    """Outcome of one role lookup."""

    role_ids: frozenset[int]
    # False only for a guild message whose roles could not be determined.
    resolved: bool


def _payload_role_ids(author: Any) -> frozenset[int] | None:
    """Role ids from the author object itself, or None if it carries none.

    A discord.Member built from the MESSAGE_CREATE payload exposes ``roles``;
    a plain discord.User has no such attribute, and a partial member may
    carry None. Both mean "not on the payload", not "no roles".
    """
    roles = getattr(author, "roles", None)
    if roles is None:
        return None
    return frozenset(role.id for role in roles)


@dataclass
class RoleResolver:
    """Resolves member role ids, payload first, REST fetch as fallback."""

    ttl: timedelta = ROLE_CACHE_TTL
    _cache: dict[tuple[int, int], tuple[float, frozenset[int]]] = field(
        default_factory=dict
    )

    async def resolve(self, *, author: Any, guild: Any) -> RoleResolution:
        """Role ids for a message author; see the module docstring for order."""
        payload = _payload_role_ids(author)
        if payload is not None:
            return RoleResolution(role_ids=payload, resolved=True)
        if guild is None:
            # A DM has no guild and no roles; nothing failed.
            return RoleResolution(role_ids=frozenset(), resolved=True)

        key = (guild.id, author.id)
        cached = self._cache.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            return RoleResolution(role_ids=cached[1], resolved=True)

        try:
            member = await guild.fetch_member(author.id)
        except Exception:  # noqa: BLE001 - any Discord failure fails closed
            _LOGGER.warning(
                "Could not fetch member %s in guild %s to read their roles",
                author.id,
                guild.id,
                exc_info=True,
            )
            return RoleResolution(role_ids=frozenset(), resolved=False)
        role_ids = _payload_role_ids(member) or frozenset()
        self._cache[key] = (time.monotonic() + self.ttl.total_seconds(), role_ids)
        return RoleResolution(role_ids=role_ids, resolved=True)
