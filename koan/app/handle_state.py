"""Persistent state and queue plumbing for the /handle pipeline.

One JSON record per issue URL in ``instance/.handle-state.json`` holds the
pipeline phase. The /handle runner reads and advances it; the ``## CI`` queue
relays terminal CI outcomes back through :func:`relay_ci_outcome`.

Writes are serialized by a lock file; reads need no lock because every write
replaces the file atomically.

See ``specs/skills/handle.md`` for the phase model and chaining contract.
"""

import contextlib
import fcntl
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from app.github_url_parser import ISSUE_URL_PATTERN

STATE_FILENAME = ".handle-state.json"
_ISSUE_URL_RE = re.compile(ISSUE_URL_PATTERN)


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


def _modify(instance_dir: str, change: Callable[[dict], Optional[bool]]) -> None:
    """Apply *change* to all records under the lock and write them back,
    unless *change* returns False (nothing changed)."""
    from app.utils import atomic_write_json

    with _locked(instance_dir):
        data = _read_all(instance_dir)
        if change(data) is not False:
            atomic_write_json(_state_path(instance_dir), data, indent=2)


def same_pr_url(a: str, b: str) -> bool:
    """PR URLs from different sources differ in owner/repo casing and slashes."""
    return bool(a) and a.rstrip("/").lower() == (b or "").rstrip("/").lower()


def _record_for_pr(data: dict, pr_url: str) -> Optional[Tuple[str, dict]]:
    for issue_url, record in data.items():
        if isinstance(record, dict) and same_pr_url(record.get("pr_url", ""), pr_url):
            return issue_url, record
    return None


def load_record(instance_dir: str, issue_url: str) -> dict:
    """Return the record for *issue_url*, or an empty dict when untracked."""
    return dict(_read_all(instance_dir).get(issue_url) or {})


def update_record(instance_dir: str, issue_url: str, **changes) -> dict:
    """Merge *changes* into the record for *issue_url* and return the result."""
    record = {}

    def _change(data: dict) -> None:
        record.update(data.get(issue_url) or {}, **changes, updated_at=time.time())
        data[issue_url] = record

    _modify(instance_dir, _change)
    return record


def reset_record(instance_dir: str, issue_url: str, **keep) -> None:
    """Restart *issue_url*'s pipeline from triage, keeping only *keep* fields."""
    def _change(data: dict) -> None:
        data.pop(issue_url, None)
        if keep:
            data[issue_url] = {**keep, "updated_at": time.time()}

    _modify(instance_dir, _change)


def find_issue_for_pr(instance_dir: str, pr_url: str) -> Optional[str]:
    """Return the issue URL whose record tracks *pr_url*, if any."""
    found = _record_for_pr(_read_all(instance_dir), pr_url)
    return found[0] if found else None


def tracks_mission(instance_dir: str, mission_title: str) -> bool:
    """True when an active /handle pipeline owns an issue this mission targets.

    Used to keep other automation (autoreview) from running a second
    review/fix loop on the PR the pipeline is driving.
    """
    urls = [m.group(0) for m in _ISSUE_URL_RE.finditer(mission_title or "")]
    if not urls or not _state_path(instance_dir).exists():
        return False
    data = _read_all(instance_dir)
    return any(
        (data.get(url) or {}).get("phase", "") not in ("", "done", "stopped") for url in urls
    )


def tracks_issue(instance_dir: str, issue_url: str) -> bool:
    """True when a /handle pipeline was started on *issue_url*, whatever its phase.

    The pipeline assigns the bot to its issue, so that assignment is never a
    request to implement — not at a gate, and not once the PR is handed off.
    """
    if not _state_path(instance_dir).exists():
        return False
    wanted = issue_url.rstrip("/").lower()
    return any(url.rstrip("/").lower() == wanted for url in _read_all(instance_dir))


def mission_line(project_name: str, command: str) -> str:
    """A pending-mission line for *command*, tagged with its project."""
    tag = f"[project:{project_name}] " if project_name else ""
    return f"- {tag}{command}"


def handle_entry(project_name: str, issue_url: str, after: str = "") -> str:
    """Build the ``/handle`` mission line; *after* tags a chained follow-up."""
    suffix = f" after:{after}" if after else ""
    return mission_line(project_name, f"/handle {issue_url}{suffix}")


def queue_step(
    instance_dir: str, project_name: str, issue_url: str, command: str, after: str,
) -> bool:
    """Queue *command* followed by the ``/handle`` tick that waits for it.

    Both lines go in one locked write so the run loop never sees one without
    the other. The follow-up's ``after:`` tag makes its dedup signature differ
    from the running ``/handle`` mission's, so it is never dropped as a copy of
    the tick that queues it.

    Returns True when the step was queued (False: already pending/running).
    """
    from app.utils import insert_pending_missions

    step_queued, _ = insert_pending_missions(
        Path(instance_dir) / "missions.md",
        [mission_line(project_name, command), handle_entry(project_name, issue_url, after)],
    )
    return step_queued


def _claim_ci_wakeup(instance_dir: str, pr_url: str, outcome: str) -> Optional[dict]:
    """Under one lock: find the record waiting on *pr_url* in phase ``ci``,
    store *outcome*, and return it (None when nothing waits on this PR)."""
    claimed = {}

    def _change(data: dict) -> bool:
        found = _record_for_pr(data, pr_url)
        if not found or found[1].get("phase") != "ci":
            return False
        found[1].update(ci_outcome=outcome, updated_at=time.time())
        claimed.update(issue_url=found[0], project=found[1].get("project", ""))
        return True

    _modify(instance_dir, _change)
    return claimed or None


def relay_ci_outcome(instance_dir: str, pr_url: str, outcome: str) -> bool:
    """Hand a terminal ``## CI`` outcome back to the /handle pipeline.

    Only a record waiting in phase ``ci`` is woken: it stores *outcome* and
    queues ``/handle <issue> after:ci``. Returns True when a tick was queued.
    """
    from app.utils import insert_pending_mission

    if not _state_path(instance_dir).exists():
        return False  # /handle never ran on this instance
    claimed = _claim_ci_wakeup(instance_dir, pr_url, outcome)
    if claimed is None:
        return False
    entry = handle_entry(claimed["project"], claimed["issue_url"], "ci")
    insert_pending_mission(Path(instance_dir) / "missions.md", entry)
    print(f"[handle] CI {outcome} for {pr_url} → /handle {claimed['issue_url']}", file=sys.stderr)
    return True
