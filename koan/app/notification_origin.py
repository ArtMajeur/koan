"""Which chat conversation the agent loop's outbox notifications belong to.

A mission's notifications quote what someone asked for, so chat history must
replay them only in the conversation (Slack thread) the mission was asked for
in. The bridge records that origin when it queues the mission; the agent loop
looks it up when it runs the mission and, for the rest of that iteration,
``append_to_outbox`` prefixes each block with a ``[conversation:…]`` header.
The outbox flush strips the headers and saves the notification with that
conversation. See the "Chat history is scoped per conversation" contract in
``specs/components/bridge.md``.
"""

import re
import sqlite3
from typing import Tuple

# Saved for a flush that merged notifications from different conversations:
# no Slack thread has this id, so no thread replays it.
MIXED_CONVERSATION = "mixed"

_HEADER_RE = re.compile(r"^\[conversation:([^\]\n]+)\]\n?", re.MULTILINE)

# The run loop handles one mission per iteration in its main thread, so a
# process-wide value is enough; it is reset when the iteration ends.
_outbox_conversation = ""


def set_outbox_conversation(conversation: str) -> None:
    global _outbox_conversation
    _outbox_conversation = conversation


def get_outbox_conversation() -> str:
    return _outbox_conversation


def outbox_header() -> str:
    """The header ``append_to_outbox`` prefixes while a mission is running."""
    if not _outbox_conversation:
        return ""
    return f"[conversation:{_outbox_conversation}]\n"


def parse_outbox_conversation(content: str) -> Tuple[str, str]:
    """Return (conversation, content without headers) for a flushed outbox.

    Untagged blocks join the tagged conversation: narrowing who replays them
    leaks nothing. Blocks from two different conversations are saved as
    ``MIXED_CONVERSATION`` so neither thread sees the other's mission.
    """
    found = set(_HEADER_RE.findall(content))
    cleaned = _HEADER_RE.sub("", content)
    if not found:
        return "", cleaned
    if len(found) > 1:
        return MIXED_CONVERSATION, cleaned
    return found.pop(), cleaned


def record_mission_origin(instance: str, mission_text: str, conversation: str) -> None:
    """Remember the conversation a mission was asked for in (bridge side)."""
    from app.mission_store.aux_stores import OriginStore
    try:
        OriginStore(instance).record(mission_text, conversation)
    except sqlite3.DatabaseError as e:
        # The mission is already queued; losing its origin only means its
        # notifications stay shared, as they were before origins existed.
        print(f"[notification_origin] could not record mission origin: {e}")


def mission_conversation(instance: str, mission_title: str) -> str:
    """The conversation a mission was asked for in, or "" (agent-loop side)."""
    if not mission_title:
        return ""
    from app.mission_store.aux_stores import OriginStore
    try:
        return OriginStore(instance).conversation_for(mission_title)
    except sqlite3.DatabaseError as e:
        print(f"[notification_origin] could not read mission origin: {e}")
        return ""
