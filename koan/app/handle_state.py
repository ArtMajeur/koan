"""Persistent state and queue plumbing for the /handle pipeline.

One JSON record per issue URL in ``instance/.handle-state.json`` holds the
pipeline phase. The /handle runner reads and advances it; the ``## CI`` queue
relays terminal CI outcomes back through :func:`relay_ci_outcome`.

See ``specs/skills/handle.md`` for the phase model and chaining contract.
"""

import contextlib
import fcntl
import json
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

STATE_FILENAME = ".handle-state.json"


def _state_path(instance_dir: str) -> Path:
    return Path(instance_dir) / STATE_FILENAME


@contextlib.contextmanager
def _locked(instance_dir: str) -> Iterator[None]:
    """Serialize read-modify-write cycles across the runner and the run loop."""
    lock_path = Path(instance_dir) / f"{STATE_FILENAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _read_all(instance_dir: str) -> dict:
    path = _state_path(instance_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # A corrupt file must not silently reset every pipeline: fail loud.
        raise RuntimeError(f"unreadable {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def load_record(instance_dir: str, issue_url: str) -> dict:
    """Return the record for *issue_url*, or an empty dict when untracked."""
    with _locked(instance_dir):
        return dict(_read_all(instance_dir).get(issue_url) or {})


def update_record(instance_dir: str, issue_url: str, **changes) -> dict:
    """Merge *changes* into the record for *issue_url* and return the result."""
    from app.utils import atomic_write_json

    with _locked(instance_dir):
        data = _read_all(instance_dir)
        record = dict(data.get(issue_url) or {})
        record.update(changes)
        record["updated_at"] = time.time()
        data[issue_url] = record
        atomic_write_json(_state_path(instance_dir), data, indent=2)
        return record


def find_issue_for_pr(instance_dir: str, pr_url: str) -> Optional[str]:
    """Return the issue URL whose record tracks *pr_url*, if any.

    Case-insensitive: callers build PR URLs from different sources, and the
    owner/repo casing of a GitHub URL is not significant.
    """
    wanted = pr_url.rstrip("/").lower()
    with _locked(instance_dir):
        for issue_url, record in _read_all(instance_dir).items():
            tracked = (record or {}).get("pr_url", "") if isinstance(record, dict) else ""
            if tracked and tracked.rstrip("/").lower() == wanted:
                return issue_url
    return None


def handle_entry(project_name: str, issue_url: str, after: str = "") -> str:
    """Build the ``/handle`` mission line; *after* tags a chained follow-up."""
    tag = f"[project:{project_name}] " if project_name else ""
    suffix = f" after:{after}" if after else ""
    return f"- {tag}/handle {issue_url}{suffix}"


def queue_step(
    instance_dir: str, project_name: str, issue_url: str,
    step_entry: str, after: str,
) -> bool:
    """Queue *step_entry* followed by the ``/handle`` tick that waits for it.

    Both lines go in one locked write so the run loop never sees one without
    the other. The step uses the regular dedup; the follow-up is deduplicated
    against Pending only, because the running ``/handle`` mission is itself
    In Progress and would otherwise block its own continuation.

    Returns True when the step was queued (False: already pending/running).
    """
    from app.missions import insert_mission, is_duplicate_mission
    from app.utils import modify_missions_file

    follow_up = handle_entry(project_name, issue_url, after)
    step_queued = True

    def _transform(content: str) -> str:
        nonlocal step_queued
        if is_duplicate_mission(content, step_entry):
            step_queued = False
        else:
            content = insert_mission(content, step_entry)
        if not is_duplicate_mission(content, follow_up, pending_only=True):
            content = insert_mission(content, follow_up)
        return content

    modify_missions_file(Path(instance_dir) / "missions.md", _transform)
    return step_queued


def relay_ci_outcome(instance_dir: str, pr_url: str, outcome: str) -> bool:
    """Hand a terminal ``## CI`` outcome back to the /handle pipeline.

    Only a record waiting in phase ``ci`` is woken: it stores *outcome* and
    queues ``/handle <issue>``. Returns True when a tick was queued.
    """
    from app.utils import insert_pending_mission

    issue_url = find_issue_for_pr(instance_dir, pr_url)
    if not issue_url:
        return False
    record = load_record(instance_dir, issue_url)
    if record.get("phase") != "ci":
        return False
    update_record(instance_dir, issue_url, ci_outcome=outcome)
    entry = handle_entry(record.get("project", ""), issue_url)
    insert_pending_mission(Path(instance_dir) / "missions.md", entry)
    print(f"[handle] CI {outcome} for {pr_url} → /handle {issue_url}", file=sys.stderr)
    return True
