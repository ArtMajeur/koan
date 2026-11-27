"""Which chat conversation the agent loop's outbox notifications belong to.

A mission's notifications quote what someone asked for, so chat history must
replay them only in the conversation (Slack thread) the mission was asked for
in. See the "Chat history is scoped per conversation" contract in
``specs/components/bridge.md``:

- the bridge records the origin whenever it queues a mission
  (``record_mission_origin``, called from ``insert_pending_mission(s)``);
- while the agent loop runs that mission, the ``.outbox-conversation`` signal
  file names it (``start_mission_conversation``). The outbox is written by the
  run loop, by skill runners and by the agent CLI itself, so the conversation
  cannot travel with each write; instead the flush tags whatever it reads
  while the file exists;
- when the mission ends, ``end_mission_conversation`` tags what is still
  waiting in the outbox and removes the file, under the outbox lock, so a
  notification written at the very end is tagged too;
- the flush strips the ``[conversation:…]`` headers and saves the notification
  with that conversation.
"""

import contextlib
import fcntl
import re
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

# Saved for a flush that merged notifications from different conversations:
# no Slack thread has this id, so no thread replays it.
MIXED_CONVERSATION = "mixed"

SIGNAL_FILE = ".outbox-conversation"

_HEADER_RE = re.compile(r"^\[conversation:([^\]\n]+)\]\n?", re.MULTILINE)


def _signal_path(instance) -> Path:
    return Path(instance) / SIGNAL_FILE


def tag(content: str, conversation: str) -> str:
    """Prefix outbox content with a conversation header ("" leaves it as is)."""
    if not conversation or not content.strip():
        return content
    return f"[conversation:{conversation}]\n{content}"


def parse_outbox_conversation(content: str) -> Tuple[str, str]:
    """Return (conversation, content without headers) for a flushed outbox.

    Blocks from two different conversations are saved as
    ``MIXED_CONVERSATION`` so neither thread sees the other's mission.
    """
    found = set(_HEADER_RE.findall(content))
    cleaned = _HEADER_RE.sub("", content)
    if not found:
        return "", cleaned
    if len(found) > 1:
        return MIXED_CONVERSATION, cleaned
    return found.pop(), cleaned


def current_mission_conversation(instance) -> str:
    """The conversation of the mission running now, or "".

    The outbox flush calls this while it holds the outbox lock.
    """
    with contextlib.suppress(OSError):
        return _signal_path(instance).read_text(encoding="utf-8").strip()
    return ""


def start_mission_conversation(instance, conversation: str) -> None:
    """Name the running mission's conversation for the outbox flush."""
    path = _signal_path(instance)
    if not conversation:
        path.unlink(missing_ok=True)
        return
    path.write_text(conversation, encoding="utf-8")


def end_mission_conversation(instance, outbox_path: Optional[Path] = None) -> None:
    """Tag what the mission left in the outbox, then forget its conversation.

    Holding the outbox lock makes this atomic with the flush, which reads the
    signal file under the same lock: every block written during the mission
    is tagged either by the flush or here.
    """
    path = _signal_path(instance)
    if not path.exists():
        return
    outbox = Path(outbox_path or Path(instance) / "outbox.md")
    if not outbox.exists():
        path.unlink(missing_ok=True)
        return
    with open(outbox, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            tagged = tag(content, current_mission_conversation(instance))
            if tagged != content:
                f.seek(0)
                f.write(tagged)
                f.truncate()
                f.flush()
            path.unlink(missing_ok=True)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record_mission_origin(instance, mission_text: str, conversation: str) -> None:
    """Remember the conversation a mission was asked for in (bridge side)."""
    if not conversation:
        return
    from app.mission_store.aux_stores import OriginStore
    try:
        OriginStore(str(instance)).record(mission_text, conversation)
    except sqlite3.DatabaseError as e:
        # The mission is already queued; losing its origin only means its
        # notifications stay shared, as they were before origins existed.
        print(f"[notification_origin] could not record mission origin: {e}")


def mission_conversation(instance, mission_title: str) -> str:
    """The conversation a mission was asked for in, or "" (agent-loop side)."""
    if not mission_title:
        return ""
    from app.mission_store.aux_stores import OriginStore
    try:
        return OriginStore(str(instance)).conversation_for(mission_title)
    except sqlite3.DatabaseError as e:
        print(f"[notification_origin] could not read mission origin: {e}")
        return ""


def forget_mission_origin(instance, mission_title: str) -> None:
    """Drop a finished mission's origin so a later identical request starts clean."""
    from app.mission_store.aux_stores import OriginStore
    try:
        OriginStore(str(instance)).forget(mission_title)
    except sqlite3.DatabaseError as e:
        print(f"[notification_origin] could not forget mission origin: {e}")
