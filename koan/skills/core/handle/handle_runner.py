"""Runner for /handle — one tick of the issue-to-PR pipeline.

Each run reads the issue's phase from ``app.handle_state``, does that phase's
cheap work, then queues the next step (followed by a ``/handle`` tick) or stops
at a human gate. Heavy work always runs as its own mission. See
``specs/skills/handle.md``.
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app import handle_state
from skills.core.handle import handle_github as gh
from skills.core.handle.handle_messages import render
from skills.core.handle.handle_parse import (
    has_plan,
    open_questions,
    parse_triage,
    review_verdict,
)

_SKILL_DIR = Path(__file__).parent
_TRIAGE_TOOLS = ["Read", "Glob", "Grep"]
_TERMINAL_PR_STATES = ("CLOSED", "MERGED")

Result = Tuple[bool, str]


@dataclass
class Tick:
    issue_url: str
    owner: str
    repo: str
    number: str
    project_name: str
    project_path: str
    instance_dir: str
    bot: str
    config: dict
    record: dict
    issue: dict = field(default_factory=dict)
    comments: List[dict] = field(default_factory=list)
    requester: str = ""

    def save(self, **changes) -> None:
        self.record = handle_state.update_record(self.instance_dir, self.issue_url, **changes)

    def comment(self, key: str, **values) -> None:
        gh.post_comment(
            self.owner, self.repo, self.number,
            render(key, requester=self.requester, bot=self.bot, **values),
        )

    def entry(self, command: str) -> str:
        return f"- [project:{self.project_name}] {command}"


# -- entry point ------------------------------------------------------------


def run_handle(
    url: str, project_path: str, project_name: str, instance_dir: str,
) -> Result:
    from app.config import get_handle_config
    from app.github_url_parser import parse_github_url

    try:
        owner, repo, kind, number = parse_github_url(url)
    except ValueError as e:
        return False, f"Not a GitHub URL: {e}"
    issue_url = f"https://github.com/{owner}/{repo}/issues/{number}"
    bot = _bot_login()
    if kind == "pull":
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        tracked = handle_state.find_issue_for_pr(instance_dir, pr_url)
        if not tracked:
            gh.post_comment(owner, repo, number, render("not_tracked", bot=bot))
            return False, f"No /handle pipeline tracks {pr_url}"
        issue_url = tracked
        owner, repo, _kind, number = parse_github_url(issue_url)

    try:
        issue = gh.fetch_issue(owner, repo, number)
        # The API's URL is canonical: one state record and one dedup key per
        # issue, whatever casing the caller typed.
        issue_url = issue.get("html_url") or issue_url
        owner, repo, _kind, number = parse_github_url(issue_url)
        tick = Tick(
            issue_url=issue_url, owner=owner, repo=repo, number=number,
            project_name=project_name, project_path=project_path,
            instance_dir=instance_dir, bot=bot, config=get_handle_config(),
            record=handle_state.load_record(instance_dir, issue_url), issue=issue,
        )
        _load_thread(tick)
        return _dispatch(tick)
    except RuntimeError as e:
        return False, f"/handle {issue_url} failed: {e}"


def _bot_login() -> str:
    from app.utils import load_config

    return str((load_config().get("github") or {}).get("nickname", "")).strip()


def _load_thread(tick: Tick) -> None:
    tick.comments = gh.fetch_comments(tick.owner, tick.repo, tick.number)
    tick.requester = gh.find_requester(tick.comments, tick.bot, tick.issue.get("author", ""))


def _dispatch(tick: Tick) -> Result:
    phase = tick.record.get("phase", "")
    if tick.record.get("pr_number") and phase not in ("done", "stopped"):
        state = gh.pr_info(tick.owner, tick.repo, tick.record["pr_number"])["state"]
        if state in _TERMINAL_PR_STATES:
            return _stop(tick, state.lower())
    return _PHASES.get(phase, _start)(tick)


# -- phases -----------------------------------------------------------------


def _start(tick: Tick) -> Result:
    if tick.issue.get("is_pr"):
        return False, f"{tick.issue_url} is a pull request, not an issue"
    tick.save(project=tick.project_name, phase="triage", started_at=time.time())
    gh.assign_self(tick.owner, tick.repo, tick.number)
    _move_board(tick, "in_progress")
    return _triage(tick)


def _triage(tick: Tick) -> Result:
    verdict = parse_triage(_run_triage(tick))
    if verdict is None:
        return _gate(tick, "triage_needs_human", "triage",
                     summary="The triage produced no usable verdict.", questions="")
    if verdict["verdict"] == "go":
        tick.comment("triage_go", summary=verdict["summary"])
        return _queue_plan(tick)
    questions = "\n".join(f"- {q}" for q in verdict["questions"])
    key = "triage_needs_human" if verdict["verdict"] == "needs_human" else "triage_not_a_bug"
    return _gate(tick, key, "triage", summary=verdict["summary"], questions=questions)


def _run_triage(tick: Tick) -> str:
    from app.language_preference import get_language_instruction
    from app.prompts import load_skill_prompt
    from app.provider import run_command

    comments = "\n\n".join(
        f"@{c['author']}: {c['body']}" for c in tick.comments if c.get("author") != tick.bot
    )
    prompt = load_skill_prompt(
        _SKILL_DIR, "triage", project_path=tick.project_path,
        REPO=f"{tick.owner}/{tick.repo}", NUMBER=tick.number,
        TITLE=tick.issue.get("title", ""), BODY=tick.issue.get("body") or "",
        COMMENTS=comments or "(none)", LANGUAGE_INSTRUCTION=get_language_instruction(),
    )
    return run_command(
        prompt=prompt, project_path=tick.project_path, allowed_tools=_TRIAGE_TOOLS,
        model_key="mission", max_turns=30, timeout=1200, project_name=tick.project_name,
    )


def _queue_plan(tick: Tick) -> Result:
    return _queue(tick, f"/plan {tick.issue_url}", "plan", "planning")


def _after_plan(tick: Tick) -> Result:
    since = tick.record.get("phase_started_at", 0)
    plan = next(
        (c for c in reversed(tick.comments)
         if (c.get("author") or "").lower() == tick.bot.lower()
         and c["created_at"] >= since and has_plan(c.get("body") or "")),
        None,
    )
    if plan is None or open_questions(plan["body"]) is None:
        return _gate(tick, "plan_missing", "plan")
    if open_questions(plan["body"]):
        return _gate(tick, "plan_questions", "plan")
    return _queue(tick, f"/implement {tick.issue_url}", "implement", "implementing")


def _after_implement(tick: Tick) -> Result:
    from app.config import get_branch_prefix

    branch = f"{get_branch_prefix()}implement-{tick.number}"
    pr = gh.find_open_pr(tick.owner, tick.repo, branch)
    if pr is None:
        return _gate(tick, "implement_no_pr", "implement")
    tick.save(pr_url=pr["url"], pr_number=str(pr["number"]), branch=branch, review_round=0)
    return _register_ci(tick)


def _register_ci(tick: Tick) -> Result:
    from app.config import is_ci_check_enabled

    if not is_ci_check_enabled():
        return _queue_review(tick)
    _add_ci_item(tick)
    tick.save(phase="ci", ci_outcome=None, phase_started_at=time.time())
    return True, f"Waiting for CI on {tick.record['pr_url']}"


def _add_ci_item(tick: Tick) -> None:
    from app.missions import add_ci_item
    from app.utils import load_config, modify_missions_file

    max_attempts = load_config().get("ci_fix_max_attempts", 5)
    record = tick.record
    modify_missions_file(
        Path(tick.instance_dir) / "missions.md",
        lambda c: add_ci_item(
            c, tick.project_name, record["pr_url"], record["pr_number"],
            record["branch"], f"{tick.owner}/{tick.repo}", max_attempts,
        ),
    )


def _after_ci(tick: Tick) -> Result:
    outcome = tick.record.get("ci_outcome")
    if outcome is None:
        # A human poke while CI runs: nothing to do until the relay fires.
        tick.comment("busy", phase="ci")
        return True, "CI still running"
    if outcome in ("success", "none"):
        return _queue_review(tick)
    return _gate(tick, "ci_blocked", "ci", pr_url=tick.record["pr_url"], outcome=outcome)


def _queue_review(tick: Tick) -> Result:
    return _queue(tick, f"/review {tick.record['pr_url']} --force", "review", "reviewing")


def _after_review(tick: Tick) -> Result:
    lgtm = review_verdict(_review_sidecar(tick), tick.record.get("phase_started_at", 0))
    pr_url = tick.record["pr_url"]
    if lgtm is None:
        return _gate(tick, "review_missing", "review", pr_url=pr_url)
    if lgtm:
        return _qa_or_finish(tick)
    rounds = tick.config["max_review_rounds"]
    if tick.record.get("review_round", 0) >= rounds:
        return _gate(tick, "review_exhausted", "fix", pr_url=pr_url, rounds=str(rounds))
    return _queue_fix(tick)


def _review_sidecar(tick: Tick) -> Path:
    """The review runner's findings file; named with the PR URL's casing."""
    folder = Path(tick.instance_dir) / ".review-findings"
    wanted = f"{tick.owner}_{tick.repo}_{tick.record['pr_number']}.json"
    if folder.is_dir():
        for path in folder.iterdir():
            if path.name.lower() == wanted.lower():
                return path
    return folder / wanted


def _queue_fix(tick: Tick) -> Result:
    tick.save(review_round=tick.record.get("review_round", 0) + 1)
    return _queue(tick, f"/rebase {tick.record['pr_url']} --fix", "fix", "fixing")


def _qa_or_finish(tick: Tick) -> Result:
    qa = tick.config["qa"]
    if not (qa["command"] and qa["preview_environment"]):
        return _finish(tick, qa_note="")
    head = gh.pr_info(tick.owner, tick.repo, tick.record["pr_number"])["headRefOid"]
    environment = qa["preview_environment"].format(pr=tick.record["pr_number"])
    status, url = gh.preview_status(tick.owner, tick.repo, environment, head)
    if status == "ready":
        command = (
            f"{qa['command']} {url} Verify the fix for {tick.issue_url} on this preview "
            f"of {tick.record['pr_url']}; report as a comment on {tick.record['pr_url']}."
        )
        return _queue(tick, command, "qa", "qa_running")
    if status == "failed":
        return _finish(tick, qa_note=render("qa_preview_failed"))
    return _wait_for_preview(tick)


def _wait_for_preview(tick: Tick) -> Result:
    from app.event_scheduler import write_event_file

    qa = tick.config["qa"]
    waits = tick.record.get("qa_waits", 0)
    if waits >= qa["max_wait_ticks"]:
        return _finish(tick, qa_note=render("qa_preview_timeout"))
    run_at = datetime.now() + timedelta(minutes=qa["wait_minutes"])
    write_event_file(
        Path(tick.instance_dir) / "events", run_at,
        handle_state.handle_entry(tick.project_name, tick.issue_url),
    )
    tick.save(phase="qa", qa_waits=waits + 1)
    return True, f"Preview not ready; next check at {run_at:%H:%M}"


def _finish(tick: Tick, qa_note: str) -> Result:
    pr_url = tick.record["pr_url"]
    gh.hand_off_pr(tick.owner, tick.repo, tick.record["pr_number"], _reviewer(tick))
    product = gh.is_team_member(tick.owner, tick.config["product_team"], tick.requester)
    _move_board(tick, "product_review" if product else "review")
    tick.comment("done", pr_url=pr_url, qa_note=qa_note)
    tick.save(phase="done", resume=None)
    return True, f"{pr_url} handed off to @{tick.requester}"


def _reviewer(tick: Tick) -> str:
    return "" if tick.requester.lower() == tick.bot.lower() else tick.requester


def _resume(tick: Tick) -> Result:
    resume = tick.record.get("resume") or "triage"
    return _RESUME[resume](tick)


def _report_busy(tick: Tick) -> Result:
    tick.comment("busy", phase=tick.record.get("phase", ""))
    return True, f"Pipeline already at {tick.record.get('phase')}"


def _stop(tick: Tick, state: str) -> Result:
    tick.comment("stopped", pr_url=tick.record["pr_url"], state=state)
    tick.save(phase="stopped", resume=None)
    return True, f"{tick.record['pr_url']} {state}; pipeline stopped"


# -- shared steps -------------------------------------------------------------


def _queue(tick: Tick, command: str, after: str, phase: str) -> Result:
    handle_state.queue_step(
        tick.instance_dir, tick.project_name, tick.issue_url, tick.entry(command), after,
    )
    tick.save(phase=phase, phase_started_at=time.time(), resume=None)
    return True, f"Queued {command.split()[0]} for {tick.issue_url}"


def _gate(tick: Tick, key: str, resume: str, **values) -> Result:
    tick.comment(key, **values)
    tick.save(phase="waiting_human", resume=resume)
    return True, f"Waiting for a human ({key}) on {tick.issue_url}"


def _move_board(tick: Tick, column_key: str) -> None:
    board = tick.config["board"]
    column = board.get(column_key) or ""
    if board and column:
        gh.move_on_board(board, tick.issue.get("node_id", ""), column)


_PHASES: Dict[str, Callable[[Tick], Result]] = {
    "triage": _triage,
    "planning": _after_plan,
    "implementing": _after_implement,
    "ci": _after_ci,
    "reviewing": _after_review,
    "fixing": _register_ci,
    "qa": _qa_or_finish,
    "qa_running": lambda tick: _finish(tick, qa_note=render("qa_ran")),
    "waiting_human": _resume,
    "done": _report_busy,
    "stopped": _report_busy,
}

_RESUME: Dict[str, Callable[[Tick], Result]] = {
    "triage": _triage,
    "plan": _queue_plan,
    "implement": lambda tick: _queue(
        tick, f"/implement {tick.issue_url}", "implement", "implementing",
    ),
    "ci": _register_ci,
    "review": _queue_review,
    "fix": _queue_fix,
}


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Run one /handle pipeline tick")
    parser.add_argument("--issue-url", required=True)
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--instance-dir", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--base-branch", default="")
    args = parser.parse_args(argv)

    success, summary = run_handle(
        args.issue_url, args.project_path, args.project_name, args.instance_dir,
    )
    print(summary)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
