"""Runner for /handle — one tick of the issue-to-PR pipeline.

Each run reads the issue's phase from ``app.handle_state``, does that phase's
cheap work, then queues the next step (followed by a ``/handle`` tick) or stops
at a human gate. Heavy work always runs as its own mission. A tick whose
context starts with ``after:`` was queued by the pipeline itself; any other
tick is a human poke. See ``specs/skills/handle.md``.
"""

import argparse
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app import handle_state
from skills.core.handle import handle_ci
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
QA_MARKER = "<!-- koan-handle-qa -->"

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
    chained: bool = False
    issue: dict = field(default_factory=dict)
    comments: List[dict] = field(default_factory=list)
    requester: str = ""

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def mention(self) -> str:
        return f"@{self.requester}: " if self.requester else ""

    def save(self, **changes) -> None:
        self.record = handle_state.update_record(self.instance_dir, self.issue_url, **changes)

    def comment(self, key: str, **values) -> None:
        gh.post_comment(
            self.owner, self.repo, self.number,
            render(key, mention=self.mention, bot=self.bot, **values),
        )

    def entry(self, command: str) -> str:
        tag = f"[project:{self.project_name}] " if self.project_name else ""
        return f"- {tag}{command}"


# -- entry point ------------------------------------------------------------


def run_handle(
    url: str, project_path: str, project_name: str, instance_dir: str, context: str = "",
) -> Result:
    from app.github_url_parser import parse_github_url

    bot = _bot_login()
    if not bot:
        return False, "github.nickname is not configured; /handle needs the bot login"
    try:
        owner, repo, kind, number = parse_github_url(url)
    except ValueError as e:
        return False, f"Not a GitHub URL: {e}"
    issue_url = f"https://github.com/{owner}/{repo}/issues/{number}"
    tick = None
    try:
        if kind == "pull":
            issue_url = _issue_for_pr(owner, repo, number, instance_dir, bot)
            if not issue_url:
                return False, f"No /handle pipeline tracks {owner}/{repo}#{number}"
        tick = _load_tick(issue_url, project_path, project_name, instance_dir, bot, context)
        return _dispatch(tick)
    # Runner boundary: any failure is reported on the issue, never swallowed.
    except Exception as e:
        target = (tick.owner, tick.repo, tick.number) if tick else (owner, repo, number)
        return _fail(tick, target, bot, instance_dir, tick.issue_url if tick else issue_url, e)


def _bot_login() -> str:
    from app.utils import load_config

    return str((load_config().get("github") or {}).get("nickname", "")).strip()


def _issue_for_pr(owner: str, repo: str, number: str, instance_dir: str, bot: str) -> str:
    tracked = handle_state.find_issue_for_pr(
        instance_dir, f"https://github.com/{owner}/{repo}/pull/{number}",
    )
    if not tracked:
        gh.post_comment(owner, repo, number, render("not_tracked", bot=bot))
    return tracked or ""


def _load_tick(
    issue_url: str, project_path: str, project_name: str, instance_dir: str,
    bot: str, context: str,
) -> Tick:
    from app.config import get_handle_config
    from app.github_url_parser import parse_github_url

    owner, repo, _kind, number = parse_github_url(issue_url)
    issue = gh.fetch_issue(owner, repo, number)
    # The API's URL is canonical: one state record and one dedup key per
    # issue, whatever casing the caller typed.
    issue_url = issue.get("html_url") or issue_url
    owner, repo, _kind, number = parse_github_url(issue_url)
    tick = Tick(
        issue_url=issue_url, owner=owner, repo=repo, number=number,
        project_name=project_name, project_path=project_path,
        instance_dir=instance_dir, bot=bot, config=get_handle_config(),
        record=handle_state.load_record(instance_dir, issue_url),
        chained=context.strip().startswith("after:"), issue=issue,
    )
    tick.comments = gh.fetch_comments(owner, repo, number)
    tick.requester = _resolve_requester(tick)
    return tick


def _resolve_requester(tick: Tick) -> str:
    """The stored requester, updated only by an authorized human trigger.

    Chained ticks never change it, so a drive-by mention mid-pipeline cannot
    take over the pings, the review request or the board column.
    """
    stored = tick.record.get("requester", "")
    if tick.chained:
        return stored
    candidates = [gh.latest_handle_mention(tick.comments, tick.bot)]
    if not stored:
        candidates.append(tick.issue.get("author", ""))
    for login in candidates:
        if login and login == stored:
            return stored
        if login and gh.is_authorized(tick.owner, tick.repo, login, tick.project_name):
            tick.save(requester=login)
            return login
    return stored


def _fail(
    tick: Optional[Tick], target: Tuple[str, str, str], bot: str,
    instance_dir: str, issue_url: str, error: Exception,
) -> Result:
    """Gate on an unexpected error: tell the requester, keep the step resumable."""
    from app.leak_detector import scan_and_redact

    print(f"[handle] {issue_url} failed:\n{traceback.format_exc()}", file=sys.stderr)
    detail = scan_and_redact(f"{type(error).__name__}: {error}"[:300], "/handle error comment")
    phase = ""
    try:
        # The tick may not exist yet (failure while loading the issue): fall
        # back to the stored record so the gate still names the requester.
        record = tick.record if tick else handle_state.load_record(instance_dir, issue_url)
        phase = record.get("phase", "")
        requester = record.get("requester", "")
        gh.post_comment(*target, render(
            "error", mention=f"@{requester}: " if requester else "", bot=bot,
            phase=phase or "start", error=detail,
        ))
        if phase and phase != "waiting_human":  # keep a gate's own resume target
            handle_state.update_record(
                instance_dir, issue_url, phase="waiting_human", resume="retry", failed_phase=phase,
            )
    # Best effort only: the tick is already failing and its error is logged above.
    except Exception as nested:
        print(f"[handle] could not report the error: {nested}", file=sys.stderr)
    return False, f"/handle {issue_url} failed at {phase or 'start'}: {detail}"


def _dispatch(tick: Tick) -> Result:
    phase = tick.record.get("phase", "")
    if phase in ("done", "stopped"):
        if tick.chained:
            return True, f"Pipeline already {phase}; chained tick ignored"
        return _reenter(tick)
    if tick.record.get("pr_number"):
        state = _pr(tick)["state"]
        if state in _TERMINAL_PR_STATES:
            return _stop(tick, state.lower())
    return _PHASES.get(phase, _start)(tick)


def _pr(tick: Tick) -> dict:
    return gh.pr_info(tick.owner, tick.repo, tick.record["pr_number"])


# -- triage and plan ----------------------------------------------------------


def _start(tick: Tick) -> Result:
    if tick.issue.get("is_pr"):
        return False, f"{tick.issue_url} is a pull request, not an issue"
    tick.save(project=tick.project_name, phase="triage", started_at=time.time())
    gh.assign_self(tick.owner, tick.repo, tick.number)
    _move_board(tick, "in_progress")
    return _triage(tick)


def _triage(tick: Tick) -> Result:
    try:
        output = _run_triage(tick)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        # A dead triage must still reach the requester, not leave the issue
        # silently parked in phase "triage".
        print(f"[handle] triage failed: {e}", file=sys.stderr)
        output = ""
    verdict = _redacted(parse_triage(output))
    if verdict is None:
        return _gate(tick, "triage_needs_human", "triage",
                     summary=render("triage_failed"), questions="")
    if verdict["verdict"] == "go":
        tick.comment("triage_go", summary=verdict["summary"])
        return _queue_plan(tick)
    questions = "\n".join(f"- {q}" for q in verdict["questions"])
    key = "triage_needs_human" if verdict["verdict"] == "needs_human" else "triage_not_a_bug"
    return _gate(tick, key, "triage", summary=verdict["summary"], questions=questions)


def _redacted(verdict: Optional[dict]) -> Optional[dict]:
    """Strip credentials from the model's free text before it is posted.

    The triage prompt carries text anyone with issue access wrote, and the
    model can read files: an injected comment could steer it into echoing a
    secret into the public thread.
    """
    if verdict is None:
        return None
    from app.leak_detector import scan_and_redact

    context = "/handle triage comment"
    return {
        **verdict,
        "summary": scan_and_redact(verdict["summary"], context),
        "questions": [scan_and_redact(q, context) for q in verdict["questions"]],
    }


def _run_triage(tick: Tick) -> str:
    from app.language_preference import get_language_instruction
    from app.prompts import load_skill_prompt
    from app.provider import run_command_streaming

    comments = "\n\n".join(
        f"@{c.get('author')}: {c.get('body')}" for c in tick.comments
        if (c.get("author") or "").lower() != tick.bot.lower()
    )
    prompt = load_skill_prompt(
        _SKILL_DIR, "triage", project_path=tick.project_path,
        REPO=tick.full_repo, NUMBER=tick.number,
        TITLE=tick.issue.get("title", ""), BODY=tick.issue.get("body") or "",
        COMMENTS=comments or "(none)", LANGUAGE_INSTRUCTION=get_language_instruction(),
    )
    # Streaming prints one line per provider event, which keeps the skill
    # runner's liveness watchdog (first_output_timeout) from killing a long
    # but healthy triage; idle_timeout ends a silent one before it does.
    return run_command_streaming(
        prompt=prompt, project_path=tick.project_path, allowed_tools=_TRIAGE_TOOLS,
        model_key="mission", max_turns=30, timeout=1200, project_name=tick.project_name,
        idle_timeout=_triage_idle_timeout(),
    )


def _triage_idle_timeout() -> Optional[int]:
    """Inactivity bound strictly below the outer skill liveness watchdog."""
    from app.config import get_first_output_timeout

    outer = get_first_output_timeout()
    return max(30, outer - 60) if outer > 0 else None


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
    if plan is None:
        return _gate(tick, "plan_missing", "plan")
    if open_questions(plan["body"]):
        return _gate(tick, "plan_questions", "plan")
    return _queue_implement(tick)


def _queue_implement(tick: Tick) -> Result:
    return _queue(tick, f"/implement {tick.issue_url}", "implement", "implementing")


# -- implementation, CI, review ---------------------------------------------------


def _after_implement(tick: Tick) -> Result:
    from app.config import get_branch_prefix

    branch = f"{get_branch_prefix()}implement-{tick.number}"
    pr = gh.find_open_pr(tick.owner, tick.repo, branch, tick.bot)
    # A PR left by an earlier attempt does not prove this /implement worked.
    if pr is None or pr["updated_at"] < tick.record.get("phase_started_at", 0):
        return _gate(tick, "implement_no_pr", "implement")
    tick.save(pr_url=pr["url"], pr_number=str(pr["number"]), branch=branch, review_round=0)
    return _await_ci(tick)


def _await_ci(tick: Tick) -> Result:
    tick.save(phase="ci", ci_outcome=None, ci_polls=0, ci_empty_checks=0,
              phase_started_at=time.time(), resume=None)
    return _after_ci(tick)


def _after_ci(tick: Tick) -> Result:
    """Decide from the PR head's own runs; the relay only wakes us up."""
    from app.config import is_ci_check_enabled

    head = _pr(tick)["headRefOid"]
    state = handle_ci.ci_at_head(tick.record["branch"], tick.full_repo, head)
    if state == "success":
        return _queue_review(tick)
    if state == "no_ci":
        if tick.record.get("ci_empty_checks", 0) >= 1:
            return _queue_review(tick)
        tick.save(ci_empty_checks=tick.record.get("ci_empty_checks", 0) + 1)
        return _poll_ci(tick, state)
    if state == "pending" and is_ci_check_enabled():
        handle_ci.register(tick.instance_dir, tick.project_name, tick.record, tick.full_repo)
        return _waiting(tick, "ci")
    if state in ("pending", "missing"):
        return _poll_ci(tick, state)
    if state == "failure" and is_ci_check_enabled() and _ci_fix_may_continue(tick):
        handle_ci.register(tick.instance_dir, tick.project_name, tick.record, tick.full_repo)
        return _waiting(tick, "ci")
    return _gate(tick, "ci_blocked", "ci", pr_url=tick.record["pr_url"], outcome=state)


def _ci_fix_may_continue(tick: Tick) -> bool:
    """/ci_check still owns the failure unless the queue reported it exhausted."""
    if handle_ci.in_ci_queue(tick.instance_dir, tick.record["pr_url"]):
        return True
    return tick.record.get("ci_outcome") != "failure"


def _poll_ci(tick: Tick, state: str) -> Result:
    """No relay will fire: check again later, a bounded number of times."""
    polls = tick.record.get("ci_polls", 0)
    if polls >= tick.config["max_polls"]:
        return _gate(tick, "ci_blocked", "ci", pr_url=tick.record["pr_url"], outcome=state)
    tick.save(ci_polls=polls + 1)
    _schedule_tick(tick, tick.config["poll_minutes"], "ci-poll")
    return _waiting(tick, "ci")


def _queue_review(tick: Tick) -> Result:
    return _queue(tick, f"/review {tick.record['pr_url']} --force", "review", "reviewing")


def _after_review(tick: Tick) -> Result:
    head = _pr(tick)["headRefOid"]
    since = tick.record.get("phase_started_at", 0)
    lgtm = review_verdict(_review_sidecar(tick), since, head)
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


# -- QA and hand-off ------------------------------------------------------------


def _qa_or_finish(tick: Tick) -> Result:
    qa = tick.config["qa"]
    if not (qa["command"] and qa["preview_environment"]):
        return _finish(tick, qa_note="")
    if tick.record.get("phase") == "qa" and not tick.chained:
        return _waiting(tick, "qa")  # a poke must not add a re-check or burn a wait
    number = tick.record["pr_number"]
    shas = (_pr(tick)["headRefOid"], gh.merge_commit_sha(tick.owner, tick.repo, number))
    environment = qa["preview_environment"].replace("{pr}", number)
    status, url = gh.preview_status(tick.owner, tick.repo, environment, shas)
    if status == "ready":
        command = (
            f"{qa['command']} {url} Verify the fix for {tick.issue_url} on this preview "
            f"of {tick.record['pr_url']}; post the report as a comment on "
            f"{tick.record['pr_url']} whose first line is {QA_MARKER}"
        )
        return _queue(tick, command, "qa", "qa_running")
    if status == "failed":
        return _finish(tick, qa_note=render("qa_preview_failed"))
    return _wait_for_preview(tick)


def _wait_for_preview(tick: Tick) -> Result:
    qa = tick.config["qa"]
    waits = tick.record.get("qa_waits", 0)
    if waits >= qa["max_wait_ticks"]:
        return _finish(tick, qa_note=render("qa_preview_timeout"))
    tick.save(phase="qa", qa_waits=waits + 1)
    when = _schedule_tick(tick, qa["wait_minutes"], "qa-wait")
    return True, f"Preview not ready; next check at {when:%H:%M}"


def _after_qa(tick: Tick) -> Result:
    """Hand off; say whether the QA run left a report (it may have timed out)."""
    comments = gh.fetch_comments(tick.owner, tick.repo, tick.record["pr_number"])
    since = tick.record.get("phase_started_at", 0)
    report = gh.latest_bot_comment(comments, tick.bot, since, marker=QA_MARKER)
    return _finish(tick, qa_note=render("qa_ran" if report else "qa_no_report"))


def _finish(tick: Tick, qa_note: str) -> Result:
    pr_url = tick.record["pr_url"]
    gh.hand_off_pr(tick.owner, tick.repo, tick.record["pr_number"], _reviewer(tick))
    product = gh.is_team_member(tick.owner, tick.config["product_team"], tick.requester)
    _move_board(tick, "product_review" if product else "review")
    tick.comment("done", pr_url=pr_url, ci_note=_ci_note(tick), qa_note=qa_note)
    tick.save(phase="done", resume=None)
    return True, f"{pr_url} handed off to @{tick.requester or '(nobody)'}"


def _ci_note(tick: Tick) -> str:
    """State the head's CI as it is now, never as an assumption."""
    head = _pr(tick)["headRefOid"]
    state = handle_ci.ci_at_head(tick.record["branch"], tick.full_repo, head)
    if state == "success":
        return render("ci_green")
    if state == "no_ci":
        return render("ci_none")
    return render("ci_other", state=state)


def _reviewer(tick: Tick) -> str:
    return "" if tick.requester.lower() == tick.bot.lower() else tick.requester


# -- re-entry, gates, shared steps ----------------------------------------------


def _reenter(tick: Tick) -> Result:
    """A human `handle` on a finished pipeline: another round, or a fresh start."""
    state = _pr(tick)["state"] if tick.record.get("pr_number") else "CLOSED"
    if state == "MERGED":
        tick.comment("merged", pr_url=tick.record["pr_url"])
        tick.save(phase="stopped")
        return True, f"{tick.record['pr_url']} already merged"
    if state == "CLOSED":
        requester = tick.requester
        handle_state.reset_record(tick.instance_dir, tick.issue_url)
        tick.record = {}
        if requester:
            tick.save(requester=requester)
        return _start(tick)
    tick.comment("new_round", pr_url=tick.record["pr_url"])
    tick.save(review_round=0)
    _move_board(tick, "in_progress")
    return _queue_fix(tick)


def _resume(tick: Tick) -> Result:
    resume = tick.record.get("resume") or "triage"
    if resume == "retry":
        tick.save(phase=tick.record.get("failed_phase", ""), resume=None)
        return _dispatch(tick)
    return _RESUME[resume](tick)


def _stop(tick: Tick, state: str) -> Result:
    tick.comment("stopped", pr_url=tick.record["pr_url"], state=render(f"state_{state}"))
    tick.save(phase="stopped", resume=None)
    return True, f"{tick.record['pr_url']} {state}; pipeline stopped"


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


def _waiting(tick: Tick, phase: str) -> Result:
    if not tick.chained:
        tick.comment("busy", phase=phase)
    return True, f"Waiting in phase {phase} on {tick.issue_url}"


def _schedule_tick(tick: Tick, minutes: int, after: str) -> datetime:
    from app.event_scheduler import write_event_file

    run_at = datetime.now() + timedelta(minutes=minutes)
    write_event_file(
        Path(tick.instance_dir) / "events", run_at,
        handle_state.handle_entry(tick.project_name, tick.issue_url, after),
    )
    return run_at


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
    "fixing": _await_ci,
    "qa": _qa_or_finish,
    "qa_running": _after_qa,
    "waiting_human": _resume,
}

_RESUME: Dict[str, Callable[[Tick], Result]] = {
    "triage": _triage,
    "plan": _queue_plan,
    "implement": _queue_implement,
    "ci": _await_ci,
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
        context=args.context,
    )
    print(summary)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
