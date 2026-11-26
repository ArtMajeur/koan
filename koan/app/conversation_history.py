"""Conversation history management.

Handles saving, loading, formatting, and compacting conversation
history stored as JSONL files. Platform-agnostic — works with
any messaging provider.
"""

import contextlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# How far back load_recent_history looks for a conversation's own turns. The
# file is shared by every thread and compacted hourly, so a bounded tail is
# enough to find a thread's recent turns without reading the whole file.
_CONVERSATION_SCAN_LINES = 200


# Which conversation (Slack thread) and sender the message being handled
# comes from. Thread-local like the notify reply context: the bridge sets it
# per inbound message and carries it into worker threads, so every writer of a
# chat turn (chat replies, skills such as sparring) can tag its conversation.
_chat_origin = threading.local()


def set_chat_origin(conversation: str, sender: str) -> None:
    _chat_origin.value = (conversation, sender)


def get_chat_origin() -> Tuple[str, str]:
    return getattr(_chat_origin, "value", ("", ""))


def _atomic_write(path: Path, content: str):
    """Crash-safe file write — delegates to :func:`app.utils.atomic_write`.

    Imported lazily to avoid an import cycle: ``utils.py`` re-exports
    symbols from this module at the bottom of its file.
    """
    from app.utils import atomic_write
    atomic_write(path, content)


def _parse_jsonl_lines(lines: list) -> List[Dict]:
    """Parse JSONL lines into a list of message dicts.

    Skips blank lines and lines with invalid JSON.
    """
    messages = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def save_conversation_message(
    history_file: Path,
    role: str,
    text: str,
    message_id: int = 0,
    message_type: str = "",
    conversation: str = "",
    sender: str = "",
):
    """Save a message to the conversation history file (JSONL format).

    Args:
        history_file: Path to the history file (e.g., instance/conversation-history.jsonl)
        role: "user" or "assistant"
        text: Message content
        message_id: Telegram message_id for reaction correlation (0 = unknown)
        message_type: Origin context tag (e.g., "chat", "conclusion", "notification")
        conversation: Conversation the turn belongs to (Slack thread_ts; empty
            for providers without threads)
        sender: Who sent a user turn, when the provider exposes it
    """
    message = {
        "timestamp": datetime.now().isoformat(),
        "role": role,
        "text": text
    }
    if message_id:
        message["message_id"] = message_id
    if message_type:
        message["message_type"] = message_type
    if conversation:
        message["conversation"] = conversation
    if sender:
        message["sender"] = sender
    try:
        from app.locked_file import locked_jsonl_append
        locked_jsonl_append(history_file, message)
    except OSError as e:
        print(f"[conversation_history] Error saving message to history: {e}")


def _in_conversation(message: Dict, conversation: str) -> bool:
    """True if a turn belongs in ``conversation``'s replay.

    Kōan's own notifications are posted outside any thread, and a thread is
    often a reply to one of them, so they are shown to every conversation.
    They are written by the bot, not typed by a user, so they cannot carry one
    user's instructions into another user's conversation.
    """
    if message.get("conversation", "") == conversation:
        return True
    return (
        message.get("role") == "assistant"
        and message.get("message_type") == "notification"
        and not message.get("conversation")
    )


def load_recent_history(
    history_file: Path,
    max_messages: int = 10,
    conversation: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Load the most recent messages from conversation history.

    Args:
        history_file: Path to the history file
        max_messages: Maximum number of recent messages to return
        conversation: When given, keep only that conversation's turns ("" is
            the conversation of untagged turns). None keeps every turn.

    Returns:
        List of message dicts with keys: timestamp, role, text
    """
    if not history_file.exists():
        return []

    # Over-read a few lines so blank/malformed lines dropped by the parser
    # can't shrink the window below max_messages. When filtering by
    # conversation, read further back so other threads can't crowd it out.
    if conversation is None:
        tail_lines = max_messages + 5
    else:
        tail_lines = max(_CONVERSATION_SCAN_LINES, max_messages + 5)

    try:
        from app.locked_file import locked_jsonl_tail
        # Tail from EOF instead of reading the whole append-only file (#2354).
        lines = locked_jsonl_tail(history_file, tail_lines)
    except OSError as e:
        print(f"[conversation_history] Error loading history: {e}")
        return []

    messages = _parse_jsonl_lines(lines)
    if conversation is not None:
        messages = [m for m in messages if _in_conversation(m, conversation)]
    return messages[-max_messages:] if len(messages) > max_messages else messages


def format_conversation_history(
    messages: List[Dict[str, str]],
    max_chars: int = 3000,
) -> str:
    """Format conversation history for inclusion in the prompt.

    Args:
        messages: List of message dicts from load_recent_history
        max_chars: Maximum total characters for the formatted history

    Returns:
        Formatted string ready to include in the prompt
    """
    if not messages:
        return ""

    lines = ["Recent conversation:"]
    total = len(lines[0])
    for msg in messages:
        if msg["role"] != "user":
            role_label = "K\u014dan"
        elif msg.get("sender"):
            role_label = f"Human ({msg['sender']})"
        else:
            role_label = "Human"
        text = msg["text"]
        if len(text) > 500:
            text = text[:500] + "..."
        line = f"{role_label}: {text}"
        total += len(line) + 1
        if total > max_chars:
            break
        lines.append(line)

    return "\n".join(lines)


def prune_topics(entries: list, max_entries: int = 20) -> list:
    """Keep only the most recent N compaction entries.

    Sorts by ``compacted_at`` timestamp (falling back to list order) and
    returns the last ``max_entries`` items.

    Args:
        entries: List of compaction entry dicts.
        max_entries: Maximum entries to retain.

    Returns:
        Pruned list (may be the same list if already within limit).
    """
    if not isinstance(entries, list) or len(entries) <= max_entries:
        return entries

    # Sort by compacted_at to ensure we keep the most recent
    with contextlib.suppress(TypeError, AttributeError):
        entries.sort(key=lambda e: e.get("compacted_at", ""))

    return entries[-max_entries:]


def compact_history(history_file: Path, topics_file: Path, min_messages: int = 20) -> int:
    """Compact conversation history at startup to avoid context bleed.

    Reads all messages from history_file, extracts discussion topics grouped
    by date, appends them to topics_file (JSON array), then truncates history.

    Args:
        history_file: Path to conversation-history.jsonl
        topics_file: Path to previous-discussions-topics.json
        min_messages: Minimum messages before compaction triggers (avoid compacting tiny histories)

    Returns:
        Number of messages compacted (0 if skipped)
    """
    if not history_file.exists():
        return 0

    # Read all messages
    messages = []
    try:
        from app.locked_file import locked_jsonl_read
        lines = locked_jsonl_read(history_file)
    except OSError:
        return 0

    messages = _parse_jsonl_lines(lines)

    if len(messages) < min_messages:
        return 0

    # Group messages by date, extract topics from user messages
    topics_by_date: Dict[str, List[str]] = {}
    for msg in messages:
        ts = msg.get("timestamp", "")
        date = ts[:10] if len(ts) >= 10 else "unknown"
        if msg.get("role") == "user":
            text = msg.get("text", "").strip()
            # Take first sentence as topic hint (max 120 chars)
            topic = text.split(".")[0].split("?")[0].split("!")[0][:120].strip()
            if topic and len(topic) > 5:
                if date not in topics_by_date:
                    topics_by_date[date] = []
                if topic not in topics_by_date[date]:
                    topics_by_date[date].append(topic)

    if not topics_by_date:
        # No extractable topics, just purge atomically
        with contextlib.suppress(OSError):
            _atomic_write(history_file, "")
        return len(messages)

    # Build compaction entry
    entry = {
        "compacted_at": datetime.now().isoformat(),
        "message_count": len(messages),
        "date_range": {
            "from": min(topics_by_date.keys()),
            "to": max(topics_by_date.keys()),
        },
        "topics_by_date": topics_by_date,
    }

    # Load existing topics file or create new
    existing = []
    if topics_file.exists():
        try:
            existing = json.loads(topics_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(entry)

    # Prune to keep only the most recent entries
    existing = prune_topics(existing)

    # Write topics file atomically
    try:
        _atomic_write(topics_file, json.dumps(existing, ensure_ascii=False, indent=2))
    except OSError:
        return 0

    # Truncate history atomically
    try:
        _atomic_write(history_file, "")
    except OSError:
        return 0

    count = len(messages)
    print(f"[conversation_history] Compacted {count} messages \u2192 {topics_file.name}")
    return count

