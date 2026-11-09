"""CI checks for /handle: only the PR head's own runs decide.

``check_existing_ci`` (used by the ``## CI`` queue) reads ``none`` on any
``gh`` failure and aggregates the newest SHA *among the runs it finds*, which
right after a force-push can still be the previous commit. /handle turns the
result into a claim on the issue, so it re-reads the runs and keeps only
those of the current head.
"""

from pathlib import Path


def ci_at_head(branch: str, full_repo: str, head_sha: str) -> str:
    """Return the CI state of *head_sha* on *branch*.

    One of ``success``, ``failure``, ``blocked_approval``, ``pending``,
    ``missing`` (the branch has runs, none for this commit yet) or ``no_ci``
    (the branch has no CI runs at all). A ``gh`` failure raises — it is never
    read as green.
    """
    from app.claude_step import aggregate_ci_runs, fetch_branch_ci_runs

    runs = fetch_branch_ci_runs(branch, full_repo)
    if not runs:
        return "no_ci"
    at_head = [run for run in runs if run.get("headSha") == head_sha]
    if not at_head:
        return "missing"
    status, _run_id = aggregate_ci_runs(at_head)
    return "no_ci" if status == "none" else status


def in_ci_queue(instance_dir: str, pr_url: str) -> bool:
    """True while the ``## CI`` queue still watches *pr_url*."""
    from app.missions import get_ci_items

    path = Path(instance_dir) / "missions.md"
    content = path.read_text() if path.exists() else ""
    wanted = pr_url.rstrip("/").lower()
    return any(item["pr_url"].rstrip("/").lower() == wanted for item in get_ci_items(content))


def register(instance_dir: str, project_name: str, record: dict, full_repo: str) -> None:
    """Put the record's PR in the ``## CI`` queue unless it is already there.

    Skipping an existing entry keeps its fix-attempt counter: ``add_ci_item``
    would reset it.
    """
    from app.missions import add_ci_item
    from app.utils import load_config, modify_missions_file

    if in_ci_queue(instance_dir, record["pr_url"]):
        return
    max_attempts = load_config().get("ci_fix_max_attempts", 5)
    modify_missions_file(
        Path(instance_dir) / "missions.md",
        lambda c: add_ci_item(
            c, project_name, record["pr_url"], record["pr_number"],
            record["branch"], full_repo, max_attempts,
        ),
    )
