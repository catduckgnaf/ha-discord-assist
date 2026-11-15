"""Clarifying answers as buttons (issue #17).

When the agent's reply expects a follow-up (``continue_conversation``), or
optionally when it simply ends in a question mark, the reply gets Yes / No
buttons. Pressing one sends that answer as the speaker's next turn in the
same conversation; only the person who was asked can press them, and a
newer turn in the conversation retires any that are still pending.
"""

from __future__ import annotations

ANSWER_ACTION = "answer"
CLARIFY_ANSWERS = ("Yes", "No")


def needs_clarify_buttons(
    speech: str, continue_conversation: bool, *, heuristic: bool = False
) -> bool:
    """Whether a reply should carry Yes / No clarify buttons.

    ``continue_conversation`` (the agent saying it expects a follow-up) is
    the real signal. The "ends with ?" heuristic only applies when the
    ``clarify_buttons_heuristic`` option turns it on.
    """
    if continue_conversation:
        return True
    return heuristic and speech.strip().endswith("?")
