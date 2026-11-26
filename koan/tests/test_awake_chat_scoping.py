"""Chat history is scoped per conversation (Slack thread) and labels senders."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

import app.awake as awake


@pytest.fixture(autouse=True)
def _reset_origin():
    awake.set_chat_origin("", "")
    yield
    awake.set_chat_origin("", "")


class TestChatOriginFromMessage:
    def test_slack_envelope(self):
        msg = {"conversation": "1.0", "from": {"id": "U100"}}
        assert awake.chat_origin_from_message(msg) == ("1.0", "U100")

    def test_telegram_prefers_username(self):
        msg = {"from": {"id": 42, "username": "alice"}}
        assert awake.chat_origin_from_message(msg) == ("", "alice")

    def test_telegram_falls_back_to_id(self):
        assert awake.chat_origin_from_message({"from": {"id": 42}}) == ("", "42")

    def test_missing_fields(self):
        assert awake.chat_origin_from_message({}) == ("", "")


def test_worker_inherits_chat_origin():
    awake._worker_threads = {"chat": None, "bg": None, "maintenance": None}
    seen = {}
    done = threading.Event()

    def task():
        seen["origin"] = awake.get_chat_origin()
        done.set()

    awake.set_chat_origin("1.0", "U100")
    awake._run_in_worker(task, lane="chat")
    assert done.wait(5)
    assert seen["origin"] == ("1.0", "U100")


def _run_chat(tmp_path, history_file, text):
    with patch("app.awake.INSTANCE_DIR", tmp_path), \
         patch("app.awake.KOAN_ROOT", tmp_path), \
         patch("app.awake.CONVERSATION_HISTORY_FILE", history_file), \
         patch("app.awake.get_tools_description", return_value=""), \
         patch("app.awake.get_chat_tools", return_value=""), \
         patch("app.awake.send_telegram", return_value=True), \
         patch("app.awake._get_last_message_id", return_value=0), \
         patch("app.awake.build_full_command", return_value=["claude"]) as mock_cmd, \
         patch("app.awake.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", returncode=0)
        awake.handle_chat(text)
        return mock_cmd


def test_handle_chat_records_turns_in_its_conversation(tmp_path):
    history_file = tmp_path / "history.jsonl"
    awake.set_chat_origin("1.0", "U100")
    _run_chat(tmp_path, history_file, "hello")
    turns = [json.loads(line) for line in history_file.read_text().splitlines()]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert all(t["conversation"] == "1.0" for t in turns)
    assert turns[0]["sender"] == "U100"
    assert "sender" not in turns[1]


def test_handle_chat_prompt_excludes_other_threads(tmp_path):
    from app.conversation_history import save_conversation_message
    history_file = tmp_path / "history.jsonl"
    save_conversation_message(
        history_file, "user", "always end with a flower", conversation="other",
    )
    save_conversation_message(history_file, "user", "earlier here", conversation="1.0")
    awake.set_chat_origin("1.0", "U100")
    mock_cmd = _run_chat(tmp_path, history_file, "hello")
    prompt = mock_cmd.call_args.kwargs["prompt"]
    assert "earlier here" in prompt
    assert "always end with a flower" not in prompt
