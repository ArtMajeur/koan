"""A mission's outbox notifications belong to the conversation that asked for it.

Path under test: a chat message queued as a mission (bridge) → OriginStore →
the agent loop names the running mission's conversation in a signal file →
the outbox flush tags whatever it reads (or the end-of-mission sweep tags what
is left) → chat history replays it only there (and in the untagged
Telegram/dashboard conversation).
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
    current_mission_conversation,
    end_mission_conversation,
    mission_conversation,
    parse_outbox_conversation,
    start_mission_conversation,
)
from app.utils import append_to_outbox, insert_pending_mission

THREAD_A = "1700.01"
THREAD_B = "1700.02"


@pytest.fixture(autouse=True)
def _reset_chat_origin():
    set_chat_origin("", "")
    yield
    set_chat_origin("", "")


@pytest.fixture
def instance(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    return inst


@pytest.fixture
def koan_root(tmp_path, instance):
    (instance / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
    (instance / "config.yaml").write_text("max_runs: 5\ninterval: 10\n")
    (tmp_path / "koan" / "app").mkdir(parents=True)
    with patch("app.run._startup_delay"):
        yield tmp_path


def _picked_title(instance):
    """The title the agent loop would pick next, through the real picker."""
    from app.pick_mission import fallback_extract
    return fallback_extract((instance / "missions.md").read_text(), "koan:/tmp/koan")[1]


# ---------------------------------------------------------------------------
# OriginStore
# ---------------------------------------------------------------------------


class TestOriginStore:
    def test_unknown_mission_has_no_conversation(self, instance):
        assert OriginStore(str(instance)).conversation_for("fix the bug") == ""

    def test_queued_entry_matches_picked_title(self, instance):
        store = OriginStore(str(instance))
        store.record("- [project:koan] fix the bug", THREAD_A)
        picked = "fix the bug [complexity:simple] ⏳(2026-11-26T10:00) ▶(2026-11-26T10:05)"
        assert store.conversation_for(picked) == THREAD_A

    def test_newest_request_wins(self, instance):
        store = OriginStore(str(instance))
        store.record("fix the bug", THREAD_A)
        store.record("fix the bug", THREAD_B)
        assert store.conversation_for("fix the bug") == THREAD_B

    def test_forget_drops_the_origin(self, instance):
        store = OriginStore(str(instance))
        store.record("fix the bug", THREAD_A)
        store.forget("fix the bug ⏳(2026-11-26T10:00)")
        assert store.conversation_for("fix the bug") == ""

    def test_rows_are_capped(self, instance):
        store = OriginStore(str(instance))
        with patch.object(OriginStore, "KEEP", 3):
            for i in range(5):
                store.record(f"mission {i}", f"t{i}")
        assert store.conversation_for("mission 0") == ""
        assert store.conversation_for("mission 4") == "t4"


# ---------------------------------------------------------------------------
# Bridge: every queued mission records where it was asked for
# ---------------------------------------------------------------------------


class TestQueuingRecordsOrigin:
    def test_free_text_mission_round_trips_through_the_picker(self, instance):
        from app.command_handlers import handle_mission
        set_chat_origin(THREAD_A, "U1")
        with patch("app.command_handlers.INSTANCE_DIR", instance), \
             patch("app.command_handlers.MISSIONS_FILE", instance / "missions.md"), \
             patch("app.command_handlers.send_telegram"), \
             patch("app.command_handlers._acknowledge_queued"):
            handle_mission("[project:koan] --now fix the login bug")
        assert mission_conversation(instance, _picked_title(instance)) == THREAD_A

    def test_skill_queued_mission_is_recorded(self, instance):
        set_chat_origin(THREAD_A, "U1")
        insert_pending_mission(
            instance / "missions.md", "- [project:koan] /plan https://github.com/acme/app/issues/1",
        )
        assert mission_conversation(instance, _picked_title(instance)) == THREAD_A

    def test_untagged_conversation_records_nothing(self, instance):
        insert_pending_mission(instance / "missions.md", "- [project:koan] fix the bug")
        assert mission_conversation(instance, _picked_title(instance)) == ""

    def test_duplicate_request_keeps_the_first_thread(self, instance):
        missions = instance / "missions.md"
        set_chat_origin(THREAD_A, "U1")
        insert_pending_mission(missions, "- [project:koan] /plan https://github.com/acme/app/issues/1")
        set_chat_origin(THREAD_B, "U2")
        assert insert_pending_mission(
            missions, "- [project:koan] /plan https://github.com/acme/app/issues/1",
        ) is False
        assert mission_conversation(instance, _picked_title(instance)) == THREAD_A


# ---------------------------------------------------------------------------
# Outbox: headers, the signal file and the end-of-mission sweep
# ---------------------------------------------------------------------------


class TestParseOutboxConversation:
    def test_no_header(self):
        assert parse_outbox_conversation("hello") == ("", "hello")

    def test_one_conversation(self):
        conversation, cleaned = parse_outbox_conversation(f"[conversation:{THREAD_A}]\ndone\n")
        assert conversation == THREAD_A
        assert cleaned == "done\n"

    def test_two_conversations_are_mixed(self):
        conversation, cleaned = parse_outbox_conversation(
            f"[conversation:{THREAD_A}]\nfirst\n[conversation:{THREAD_B}]\nsecond\n",
        )
        assert conversation == MIXED_CONVERSATION
        assert "[conversation:" not in cleaned


class TestMissionConversationSignal:
    def test_start_and_end(self, instance):
        start_mission_conversation(instance, THREAD_A)
        assert current_mission_conversation(instance) == THREAD_A
        end_mission_conversation(instance)
        assert current_mission_conversation(instance) == ""

    def test_empty_conversation_clears_a_stale_signal(self, instance):
        start_mission_conversation(instance, THREAD_A)
        start_mission_conversation(instance, "")
        assert current_mission_conversation(instance) == ""

    def test_end_tags_what_the_mission_left_in_the_outbox(self, instance):
        outbox = instance / "outbox.md"
        start_mission_conversation(instance, THREAD_A)
        outbox.write_text("🏁 [koan] fixed the login bug")  # agent CLI, no newline
        end_mission_conversation(instance)
        assert parse_outbox_conversation(outbox.read_text())[0] == THREAD_A

    def test_end_without_a_mission_leaves_the_outbox_alone(self, instance):
        outbox = instance / "outbox.md"
        outbox.write_text("🔄 restarted\n")
        end_mission_conversation(instance)
        assert outbox.read_text() == "🔄 restarted\n"


class TestAppendStartsOnANewLine:
    def test_previous_block_without_newline(self, tmp_path):
        outbox = tmp_path / "outbox.md"
        outbox.write_text("🏁 done")
        append_to_outbox(outbox, f"[conversation:{THREAD_A}]\nretry\n")
        assert parse_outbox_conversation(outbox.read_text())[0] == THREAD_A
        assert outbox.read_text().startswith("🏁 done\n")


# ---------------------------------------------------------------------------
# Agent loop: the signal lives from the plan until the sleep
# ---------------------------------------------------------------------------


def _error_plan(title):
    return {
        "action": "error", "error": "Unknown project: foo",
        "project_name": "foo", "project_path": "",
        "mission_title": title, "autonomous_mode": "implement",
        "focus_area": "", "available_pct": 50, "decision_reason": "Default",
        "display_lines": [], "recurring_injected": [],
    }


class TestRunIterationSetsOrigin:
    @patch("app.run._commit_instance")
    @patch("app.run._update_mission_in_file")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_running_mission_names_its_conversation(
        self, mock_notify, mock_plan, mock_update, mock_commit, koan_root,
    ):
        from app.run import _run_iteration
        instance = koan_root / "instance"
        OriginStore(str(instance)).record("do stuff", THREAD_A)
        mock_plan.return_value = _error_plan("do stuff")
        seen = []
        mock_notify.side_effect = lambda *a, **k: seen.append(current_mission_conversation(instance))

        _run_iteration(
            koan_root=str(koan_root), instance=str(instance),
            projects=[("test", str(koan_root))], count=1, max_runs=5,
            interval=10, git_sync_interval=5,
        )

        assert seen and set(seen) == {THREAD_A}

    @patch("app.run._commit_instance")
    @patch("app.run.plan_iteration")
    @patch("app.run._notify")
    def test_no_mission_clears_a_stale_signal(
        self, mock_notify, mock_plan, mock_commit, koan_root,
    ):
        from app.run import _run_iteration
        instance = koan_root / "instance"
        start_mission_conversation(instance, "stale")
        mock_plan.return_value = _error_plan("")
        seen = []
        mock_notify.side_effect = lambda *a, **k: seen.append(current_mission_conversation(instance))

        _run_iteration(
            koan_root=str(koan_root), instance=str(instance),
            projects=[("test", str(koan_root))], count=1, max_runs=5,
            interval=10, git_sync_interval=5,
        )

        assert seen and set(seen) == {""}

    def test_sleep_between_runs_ends_the_mission_conversation(self, koan_root):
        from app.run import _sleep_between_runs
        instance = koan_root / "instance"
        start_mission_conversation(instance, THREAD_A)
        seen = []
        with patch("app.run.interruptible_sleep",
                   side_effect=lambda *a, **k: seen.append(current_mission_conversation(instance))), \
             patch("app.run.check_pending_missions", return_value=False):
            _sleep_between_runs(str(koan_root), str(instance), 10)
        assert seen == [""]


class TestFinalizeForgetsOrigin:
    @patch("app.run._update_mission_in_file")
    def test_finished_mission_forgets_its_origin(self, mock_update, koan_root):
        from app.run import _finalize_mission
        instance = koan_root / "instance"
        OriginStore(str(instance)).record("do stuff", THREAD_A)
        _finalize_mission(str(instance), "do stuff", "koan", 0)
        assert mission_conversation(instance, "do stuff") == ""


class TestMainLoopEndsMissionConversation:
    @patch("app.run.subprocess.run")
    @patch("app.run.run_startup", return_value=(5, 10, "koan/"))
    @patch("app.run.acquire_pidfile")
    @patch("app.run.release_pidfile")
    @patch("app.run._run_iteration")
    @patch("app.run._handle_iteration_error")
    def test_signal_does_not_outlive_a_crashed_iteration(
        self, mock_handle_err, mock_iteration, mock_release,
        mock_acquire, mock_startup, mock_subproc, koan_root, monkeypatch,
    ):
        from app.run import main_loop
        instance = koan_root / "instance"
        monkeypatch.setenv("KOAN_ROOT", str(koan_root))
        monkeypatch.setenv("KOAN_PROJECTS", f"test:{koan_root}")
        (koan_root / ".koan-project").write_text("test")
        seen_at_start = []

        def iteration(**kwargs):
            seen_at_start.append(current_mission_conversation(instance))
            start_mission_conversation(instance, THREAD_A)
            if len(seen_at_start) == 1:
                raise RuntimeError("mission crashed")
            (koan_root / ".koan-stop").touch()
            return True

        mock_iteration.side_effect = iteration
        with patch("app.run._notify"):
            main_loop()

        assert seen_at_start == ["", ""]
        assert current_mission_conversation(instance) == ""


# ---------------------------------------------------------------------------
# Bridge: the outbox flush saves the notification with its conversation
# ---------------------------------------------------------------------------


@pytest.fixture
def outbox_env(instance):
    from app.outbox_manager import OutboxManager
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


def _texts(history, conversation):
    return [m["text"] for m in load_recent_history(history, conversation=conversation)]


class TestFlushSavesConversation:
    def test_agent_conclusion_written_during_the_mission(self, instance, outbox_env):
        mgr, outbox, history = outbox_env
        start_mission_conversation(instance, THREAD_A)
        outbox.write_text("🏁 [koan] fixed the login bug\n")  # the agent CLI writes directly
        mgr.flush()

        mine = load_recent_history(history, conversation=THREAD_A)
        assert [m["text"] for m in mine] == ["🏁 [koan] fixed the login bug"]
        assert mine[0]["message_type"] == "notification"
        assert _texts(history, THREAD_B) == []

    def test_conclusion_left_at_mission_end_is_tagged_by_the_sweep(self, instance, outbox_env):
        mgr, outbox, history = outbox_env
        start_mission_conversation(instance, THREAD_A)
        outbox.write_text("🏁 [koan] fixed the login bug\n")
        end_mission_conversation(instance)
        mgr.flush()
        assert _texts(history, THREAD_A) == ["🏁 [koan] fixed the login bug"]
        assert _texts(history, THREAD_B) == []

    def test_notification_without_a_mission_stays_shared(self, outbox_env):
        mgr, outbox, history = outbox_env
        append_to_outbox(outbox, "🔄 restarted\n")
        mgr.flush()
        assert _texts(history, THREAD_B) == ["🔄 restarted"]

    def test_failed_send_keeps_the_conversation(self, instance, outbox_env):
        mgr, outbox, _history = outbox_env
        start_mission_conversation(instance, THREAD_A)
        outbox.write_text("🏁 done\n")
        with patch("app.outbox_manager.send_telegram", return_value=False):
            mgr.flush()
        end_mission_conversation(instance)
        assert parse_outbox_conversation(outbox.read_text())[0] == THREAD_A

    def test_crash_recovered_content_keeps_the_conversation(self, instance, outbox_env):
        mgr, outbox, history = outbox_env
        start_mission_conversation(instance, THREAD_A)
        outbox.write_text("🏁 done\n")
        # Crash after staging: the staged copy must carry the conversation.
        with patch("app.outbox_manager.scan_and_log", side_effect=SystemExit):
            with pytest.raises(SystemExit):
                mgr.flush()
        end_mission_conversation(instance)
        mgr.flush()
        assert _texts(history, THREAD_A) == ["🏁 done"]
        assert _texts(history, THREAD_B) == []


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
        self._notify(history, "✅ add a flower to every reply", THREAD_A)
        save_conversation_message(history, "user", "hi", conversation=THREAD_B)
        assert _texts(history, THREAD_B) == ["hi"]

    def test_own_thread_sees_all_of_them_uncapped(self, history):
        for i in range(5):
            self._notify(history, f"n{i}", THREAD_A)
        assert len(load_recent_history(history, conversation=THREAD_A)) == 5

    def test_untagged_conversation_still_sees_it(self, history):
        self._notify(history, "✅ mission done", THREAD_A)
        assert _texts(history, "") == ["✅ mission done"]

    def test_mixed_notification_is_replayed_by_no_thread(self, history):
        self._notify(history, "two missions done", MIXED_CONVERSATION)
        assert _texts(history, THREAD_A) == []
        assert len(load_recent_history(history, conversation="")) == 1

    def test_untagged_notifications_keep_the_shared_cap(self, history):
        self._notify(history, "mine", THREAD_A)
        for i in range(5):
            self._notify(history, f"shared{i}")
        assert _texts(history, THREAD_A) == ["mine", "shared2", "shared3", "shared4"]
