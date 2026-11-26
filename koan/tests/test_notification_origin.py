"""A mission's outbox notifications belong to the conversation that asked for it.

Path under test: a chat message queued as a mission (bridge) → OriginStore →
the agent loop tags the outbox while it runs that mission → the outbox flush
saves the notification with that conversation → chat history replays it only
there (and in the untagged Telegram/dashboard conversation).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.conversation_history import (
    load_recent_history,
    save_conversation_message,
    set_chat_origin,
)
from app.mission_store.aux_stores import OriginStore
from app.notification_origin import (
    MIXED_CONVERSATION,
    get_outbox_conversation,
    mission_conversation,
    parse_outbox_conversation,
    set_outbox_conversation,
)
from app.utils import append_to_outbox


@pytest.fixture
def koan_root(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
    (instance / "config.yaml").write_text("max_runs: 5\ninterval: 10\n")
    (tmp_path / "koan" / "app").mkdir(parents=True)
    with patch("app.run._startup_delay"):
        yield tmp_path


@pytest.fixture(autouse=True)
def _reset_origins():
    set_outbox_conversation("")
    set_chat_origin("", "")
    yield
    set_outbox_conversation("")
    set_chat_origin("", "")


# ---------------------------------------------------------------------------
# OriginStore
# ---------------------------------------------------------------------------


class TestOriginStore:
    def test_unknown_mission_has_no_conversation(self, tmp_path):
        assert OriginStore(str(tmp_path)).conversation_for("fix the bug") == ""

    def test_lifecycle_markers_do_not_change_the_lookup(self, tmp_path):
        store = OriginStore(str(tmp_path))
        store.record("fix the bug", "1700.01")
        picked = "fix the bug [complexity:simple] ⏳(2026-11-26T10:00) ▶(2026-11-26T10:05)"
        assert store.conversation_for(picked) == "1700.01"

    def test_newest_request_wins(self, tmp_path):
        store = OriginStore(str(tmp_path))
        store.record("fix the bug", "1700.01")
        store.record("fix the bug", "1700.02")
        assert store.conversation_for("fix the bug") == "1700.02"

    def test_empty_conversation_is_not_recorded(self, tmp_path):
        store = OriginStore(str(tmp_path))
        store.record("fix the bug", "1700.01")
        store.record("fix the bug", "")
        assert store.conversation_for("fix the bug") == "1700.01"

    def test_rows_are_capped(self, tmp_path):
        store = OriginStore(str(tmp_path))
        with patch.object(OriginStore, "KEEP", 3):
            for i in range(5):
                store.record(f"mission {i}", f"t{i}")
        assert store.conversation_for("mission 0") == ""
        assert store.conversation_for("mission 4") == "t4"


# ---------------------------------------------------------------------------
# Bridge: handle_mission records where the mission was asked for
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    with patch("app.command_handlers.INSTANCE_DIR", instance), \
         patch("app.command_handlers.MISSIONS_FILE", instance / "missions.md"), \
         patch("app.command_handlers.insert_pending_mission"), \
         patch("app.command_handlers.send_telegram"):
        yield instance


class TestHandleMissionRecordsOrigin:
    def test_slack_thread_is_recorded(self, bridge):
        from app.command_handlers import handle_mission
        set_chat_origin("1700.01", "U1")
        handle_mission("[project:koan] fix the login bug")
        assert mission_conversation(str(bridge), "fix the login bug") == "1700.01"

    def test_untagged_conversation_records_nothing(self, bridge):
        from app.command_handlers import handle_mission
        handle_mission("[project:koan] fix the login bug")
        assert mission_conversation(str(bridge), "fix the login bug") == ""


# ---------------------------------------------------------------------------
# Agent loop: the outbox is tagged while a mission runs
# ---------------------------------------------------------------------------


class TestOutboxTagging:
    def test_untagged_outside_a_mission(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        append_to_outbox(outbox, "hello\n")
        assert outbox.read_text() == "hello\n"

    def test_tagged_while_a_mission_runs(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        set_outbox_conversation("1700.01")
        append_to_outbox(outbox, "mission done\n")
        conversation, cleaned = parse_outbox_conversation(outbox.read_text())
        assert conversation == "1700.01"
        assert cleaned.strip() == "mission done"

    def test_tag_and_priority_headers_combine(self, tmp_path):
        from app.notify import NotificationPriority
        from app.outbox_manager import parse_outbox_priority
        outbox = tmp_path / "outbox.md"
        set_outbox_conversation("1700.01")
        append_to_outbox(outbox, "mission done\n", priority=NotificationPriority.INFO)
        conversation, cleaned = parse_outbox_conversation(outbox.read_text())
        priority, cleaned = parse_outbox_priority(cleaned)
        assert conversation == "1700.01"
        assert priority == NotificationPriority.INFO
        assert cleaned.strip() == "mission done"


class TestParseOutboxConversation:
    def test_no_header(self):
        assert parse_outbox_conversation("hello") == ("", "hello")

    def test_untagged_blocks_join_the_tagged_conversation(self):
        conversation, cleaned = parse_outbox_conversation(
            "idle notice\n[conversation:1700.01]\nmission done\n",
        )
        assert conversation == "1700.01"
        assert "[conversation:" not in cleaned
        assert "idle notice" in cleaned and "mission done" in cleaned

    def test_two_conversations_are_mixed(self):
        conversation, cleaned = parse_outbox_conversation(
            "[conversation:1700.01]\nfirst\n[conversation:1700.02]\nsecond\n",
        )
        assert conversation == MIXED_CONVERSATION
        assert "[conversation:" not in cleaned

    def test_same_conversation_twice_stays_that_conversation(self):
        conversation, _ = parse_outbox_conversation(
            "[conversation:1700.01]\nfirst\n[conversation:1700.01]\nsecond\n",
        )
        assert conversation == "1700.01"


class TestRunIterationSetsOrigin:
    @patch("app.run._commit_instance")
    @patch("app.run._update_mission_in_file")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_mission_notifications_carry_its_conversation(
        self, mock_notify, mock_plan, mock_update, mock_commit, koan_root,
    ):
        from app.run import _run_iteration
        instance = str(koan_root / "instance")
        OriginStore(instance).record("do stuff", "1700.01")
        mock_plan.return_value = {
            "action": "error", "error": "Unknown project: foo",
            "project_name": "foo", "project_path": "",
            "mission_title": "do stuff", "autonomous_mode": "implement",
            "focus_area": "", "available_pct": 50, "decision_reason": "Default",
            "display_lines": [], "recurring_injected": [],
        }
        seen = []
        mock_notify.side_effect = lambda *a, **k: seen.append(get_outbox_conversation())

        _run_iteration(
            koan_root=str(koan_root), instance=instance,
            projects=[("test", str(koan_root))], count=1, max_runs=5,
            interval=10, git_sync_interval=5,
        )

        assert seen and set(seen) == {"1700.01"}

    @patch("app.run._commit_instance")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_no_mission_leaves_the_outbox_untagged(
        self, mock_notify, mock_plan, mock_commit, koan_root,
    ):
        from app.run import _run_iteration
        set_outbox_conversation("stale")
        mock_plan.return_value = {
            "action": "error", "error": "boom",
            "project_name": "foo", "project_path": "",
            "mission_title": "", "autonomous_mode": "implement",
            "focus_area": "", "available_pct": 50, "decision_reason": "Default",
            "display_lines": [], "recurring_injected": [],
        }
        seen = []
        mock_notify.side_effect = lambda *a, **k: seen.append(get_outbox_conversation())

        _run_iteration(
            koan_root=str(koan_root), instance=str(koan_root / "instance"),
            projects=[("test", str(koan_root))], count=1, max_runs=5,
            interval=10, git_sync_interval=5,
        )

        assert seen and set(seen) == {""}


# ---------------------------------------------------------------------------
# Bridge: the outbox flush saves the notification with its conversation
# ---------------------------------------------------------------------------


@pytest.fixture
def outbox_env(tmp_path):
    from app.outbox_manager import OutboxManager
    instance = tmp_path / "instance"
    instance.mkdir()
    outbox = instance / "outbox.md"
    history = instance / "conversation-history.jsonl"
    mgr = OutboxManager(outbox, instance, history)
    with patch("app.outbox_manager.send_telegram", return_value=True), \
         patch("app.outbox_manager.scan_and_log", return_value=MagicMock(blocked=False)), \
         patch("app.outbox_manager.log"), \
         patch.object(OutboxManager, "_get_last_message_id", return_value=0), \
         patch.object(OutboxManager, "_format_message", side_effect=lambda t: t), \
         patch.object(OutboxManager, "_expand_github_refs", side_effect=lambda t, _raw: t):
        yield mgr, outbox, history


class TestFlushSavesConversation:
    def test_mission_notification_is_saved_to_its_thread(self, outbox_env):
        mgr, outbox, history = outbox_env
        set_outbox_conversation("1700.01")
        append_to_outbox(outbox, "✅ fix the login bug\n")
        set_outbox_conversation("")
        mgr.flush()

        mine = load_recent_history(history, conversation="1700.01")
        assert [m["text"] for m in mine] == ["✅ fix the login bug"]
        assert mine[0]["message_type"] == "notification"
        assert "[conversation:" not in mine[0]["text"]
        assert load_recent_history(history, conversation="1700.99") == []

    def test_notification_without_origin_stays_shared(self, outbox_env):
        mgr, outbox, history = outbox_env
        append_to_outbox(outbox, "🔄 restarted\n")
        mgr.flush()
        other = load_recent_history(history, conversation="1700.99")
        assert [m["text"] for m in other] == ["🔄 restarted"]

    def test_failed_send_requeues_with_its_tag(self, outbox_env):
        mgr, outbox, _history = outbox_env
        set_outbox_conversation("1700.01")
        append_to_outbox(outbox, "✅ done\n")
        set_outbox_conversation("")
        with patch("app.outbox_manager.send_telegram", return_value=False):
            mgr.flush()
        assert parse_outbox_conversation(outbox.read_text())[0] == "1700.01"


# ---------------------------------------------------------------------------
# Chat history: who replays a tagged notification
# ---------------------------------------------------------------------------


class TestReplayOfTaggedNotifications:
    @pytest.fixture
    def history(self, tmp_path):
        return tmp_path / "conversation-history.jsonl"

    def _notify(self, history, text, conversation=""):
        save_conversation_message(
            history, "assistant", text,
            message_type="notification", conversation=conversation,
        )

    def test_other_threads_never_see_it(self, history):
        self._notify(history, "✅ add a flower to every reply", "1700.01")
        save_conversation_message(history, "user", "hi", conversation="1700.02")
        texts = [m["text"] for m in load_recent_history(history, conversation="1700.02")]
        assert texts == ["hi"]

    def test_own_thread_sees_all_of_them_uncapped(self, history):
        for i in range(5):
            self._notify(history, f"n{i}", "1700.01")
        assert len(load_recent_history(history, conversation="1700.01")) == 5

    def test_untagged_conversation_still_sees_it(self, history):
        self._notify(history, "✅ mission done", "1700.01")
        texts = [m["text"] for m in load_recent_history(history, conversation="")]
        assert texts == ["✅ mission done"]

    def test_mixed_notification_is_replayed_by_no_thread(self, history):
        self._notify(history, "two missions done", MIXED_CONVERSATION)
        assert load_recent_history(history, conversation="1700.01") == []
        assert len(load_recent_history(history, conversation="")) == 1

    def test_untagged_notifications_keep_the_shared_cap(self, history):
        self._notify(history, "mine", "1700.01")
        for i in range(5):
            self._notify(history, f"shared{i}")
        texts = [m["text"] for m in load_recent_history(history, conversation="1700.01")]
        assert texts == ["mine", "shared2", "shared3", "shared4"]


class TestMainLoopResetsOrigin:
    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    @patch("app.run._handle_iteration_error")
    def test_tag_does_not_outlive_the_iteration(
        self, mock_handle_err, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root, monkeypatch,
    ):
        from app.run import main_loop
        monkeypatch.setenv("KOAN_ROOT", str(koan_root))
        monkeypatch.setenv("KOAN_PROJECTS", f"test:{koan_root}")
        (koan_root / ".koan-project").write_text("test")
        seen_at_start = []

        def iteration(**kwargs):
            seen_at_start.append(get_outbox_conversation())
            set_outbox_conversation("1700.01")
            if len(seen_at_start) == 1:
                raise RuntimeError("mission crashed")
            (koan_root / ".koan-stop").touch()
            return True

        mock_iteration.side_effect = iteration
        with patch("app.run._notify"):
            main_loop()

        assert seen_at_start == ["", ""]
        assert get_outbox_conversation() == ""
