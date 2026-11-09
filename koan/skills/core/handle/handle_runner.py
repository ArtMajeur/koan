"""Runner for /handle — one tick of the issue-to-PR pipeline.

Each run reads the issue's phase from ``app.handle_state``, does that phase's
cheap work, then queues the next step (followed by a ``/handle`` tick) or stops
at a human gate. Heavy work always runs as its own mission. A tick whose
context starts with ``after:`` was queued by the pipeline itself; any other
tick is a human poke. See ``specs/skills/handle.md``.
"""

import argparse
import json
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
    after: str = ""
    issue: dict = field(default_factory=dict)
    comments: List[dict] = field(default_factory=list)
    requester: str = ""
    pr_view: Optional[dict] = field(default=None, repr=False)

    @property
    def chained(self) -> bool:
        """Queued by the pipeline itself (follow-up, CI relay, re-check)."""
        return bool(self.after)

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def mention(self) -> str:
        return _mention(self.requester)

    @property
    def pr_url(self) -> str:
        return self.record["pr_url"]

    @property
    def pr_number(self) -> str:
        return self.record["pr_number"]

    def save(self, **changes) -> None:
        self.record = handle_state.update_record(self.instance_dir, self.issue_url, **changes)

    def comment(self, key: str, **values) -> None:
        gh.post_comment(
            self.owner, self.repo, self.number,
            render(key, mention=self.mention, bot=self.bot, **values),
        )


def _mention(login: str) -> str:
    return f"@{login}: " if login else ""


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
    from app.github_config import get_github_nickname
    from app.utils import load_config

    return get_github_nickname(load_config())


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
        instance_dir=instance_dir, bot=bot, config=get_handle_config(project_name),
        record=handle_state.load_record(instance_dir, issue_url),
        after=_after_tag(context), issue=issue,
    )
    tick.comments = gh.fetch_comments(owner, repo, number)
    tick.requester = _resolve_requester(tick)
    return tick


def _after_tag(context: str) -> str:
    """The step a chained tick waits for (``after:<step>``), or "" for a human poke."""
    first = (context or "").strip().split(" ", 1)[0]
    return first[len("after:"):] if first.startswith("after:") else ""


def _resolve_requester(tick: Tick) -> str:
    """The stored requester, updated only by an authorized human trigger.

    Chained ticks never change it, so a drive-by mention mid-pipeline cannot
    take over the pings, the review request or the board column.
    """
    stored = tick.record.get("requester", "")
    if tick.chained:
        return stored
    # A mention older than the last tick is not this poke (e.g. a poke from
    # chat): it must not hand the pipeline back to an earlier commenter.
    since = tick.record.get("updated_at", 0)
    candidates = [gh.latest_handle_mention(tick.comments, tick.bot, since)]
    if not stored:
        candidates.append(tick.issue.get("author", ""))
    for login in candidates:
        if login and gh.same_login(login, stored):
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
            "error", mention=_mention(requester), bot=bot,
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
    if tick.chained and tick.after not in _EXPECTED_AFTER.get(phase, ()):
        # A follow-up, relay or re-check left over from an earlier path (the
        # pipeline gated, a poke moved it on): acting on it would repeat work
        # no human asked for.
        return True, f"Stale after:{tick.after} tick ignored in phase {phase or 'new'}"
    if phase in ("done", "stopped"):
        return _reenter(tick)
    if not tick.chained and phase in _STEP_PHASES and _follow_up_pending(tick, phase):
        # The step is queued or running and its own tick will judge it; a poke
        # evaluating it now would read "no output" and gate by mistake.
        return _waiting(tick, phase)
    if tick.record.get("pr_number"):
        state = _pr(tick)["state"]
        if state in _TERMINAL_PR_STATES:
            return _stop(tick, state.lower())
    if _issue_closed(tick):
        return _stop_on_closed_issue(tick)
    return _PHASES.get(phase, _start)(tick)


def _pr(tick: Tick) -> dict:
    """The PR view, fetched once per tick: its head cannot move during a tick."""
    if tick.pr_view is None:
        tick.pr_view = gh.pr_info(tick.owner, tick.repo, tick.pr_number)
    return tick.pr_view


def _head_ci(tick: Tick) -> str:
    return handle_ci.ci_at_head(tick.record["branch"], tick.full_repo, _pr(tick)["headRefOid"])


# -- triage and plan ----------------------------------------------------------


def _start(tick: Tick) -> Result:
    if tick.issue.get("is_pr"):
        return False, f"{tick.issue_url} is a pull request, not an issue"
    tick.save(project=tick.project_name, phase="triage")
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
        if not gh.same_login(c.get("author") or "", tick.bot)
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
    plan = gh.latest_bot_comment(tick.comments, tick.bot, since, match=has_plan)
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
    if tick.config["ready_before_ci"] and _pr(tick).get("isDraft"):
        # Some CIs skip draft PRs: without this the loop would review and fix
        # a PR whose CI never ran.
        gh.mark_ready(tick.owner, tick.repo, tick.pr_number)
        tick.pr_view["isDraft"] = False
    tick.save(phase="ci", ci_outcome=None, ci_polls=0, ci_empty_checks=0,
              phase_started_at=time.time(), resume=None)
    return _after_ci(tick)


def _after_ci(tick: Tick) -> Result:
    """Decide from the PR head's own runs; the relay only wakes us up."""
    from app.config import is_ci_check_enabled

    state = _head_ci(tick)
    if state == "success":
        return _queue_review(tick)
    if _recheck_pending(tick, "ci-poll"):
        return _waiting(tick, "ci")  # a poke or a relay must not start a second poll chain
    if state in ("no_ci", "missing"):
        return _confirm_no_ci(tick, state)
    ci_check = is_ci_check_enabled()
    if ci_check and (state == "pending" or (state == "failure" and _ci_fix_may_continue(tick))):
        handle_ci.register(tick.instance_dir, tick.project_name, tick.record, tick.full_repo)
        return _waiting(tick, "ci")
    if state == "pending":
        return _poll_ci(tick, state)
    return _gate(tick, "ci_blocked", "ci", pr_url=tick.pr_url, outcome=state)


def _confirm_no_ci(tick: Tick, state: str) -> Result:
    """No run for the head: re-check once after ``poll_minutes``, then review.

    A path-filtered or docs-only commit never gets a run, but a run can also
    take a moment to appear after a push.
    """
    # A relay about an older commit can land seconds after the push: only the
    # scheduled re-check (or a poke, if it was lost) may conclude.
    if tick.record.get("ci_empty_checks") and tick.after != "ci":
        return _queue_review(tick)
    tick.save(ci_empty_checks=1)
    return _poll_ci(tick, state)


def _ci_fix_may_continue(tick: Tick) -> bool:
    """/ci_check still owns the failure unless the queue reported it exhausted."""
    if handle_ci.in_ci_queue(tick.instance_dir, tick.pr_url):
        return True
    return tick.record.get("ci_outcome") != "failure"


def _poll_ci(tick: Tick, state: str) -> Result:
    """No relay will fire: check again later, a bounded number of times."""
    polls = tick.record.get("ci_polls", 0)
    if polls >= tick.config["max_polls"]:
        return _gate(tick, "ci_blocked", "ci", pr_url=tick.pr_url, outcome=state)
    tick.save(ci_polls=polls + 1)
    _schedule_tick(tick, tick.config["poll_minutes"], "ci-poll")
    return _waiting(tick, "ci")


def _queue_review(tick: Tick) -> Result:
    return _queue(tick, f"/review {tick.pr_url} --force", "review", "reviewing")


def _after_review(tick: Tick) -> Result:
    since = tick.record.get("phase_started_at", 0)
    lgtm = review_verdict(_review_sidecar(tick), since, _pr(tick)["headRefOid"])
    pr_url = tick.pr_url
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
    wanted = f"{tick.owner}_{tick.repo}_{tick.pr_number}.json"
    if folder.is_dir():
        for path in folder.iterdir():
            if path.name.lower() == wanted.lower():
                return path
    return folder / wanted


def _queue_fix(tick: Tick) -> Result:
    tick.save(review_round=tick.record.get("review_round", 0) + 1)
    return _queue(tick, f"/rebase {tick.pr_url} --fix", "fix", "fixing")


# -- QA and hand-off ------------------------------------------------------------


def _qa_or_finish(tick: Tick) -> Result:
    qa = tick.config["qa"]
    if not (qa["command"] and qa["preview_environment"]):
        return _finish(tick, qa_note="")
    if tick.record.get("phase") == "qa" and _recheck_pending(tick, "qa-wait"):
        return _waiting(tick, "qa")  # a poke must not add a re-check or burn a wait
    number = tick.pr_number
    shas = (_pr(tick)["headRefOid"], gh.merge_commit_sha(tick.owner, tick.repo, number))
    environment = qa["preview_environment"].replace("{pr}", number)
    status, url = gh.preview_status(tick.owner, tick.repo, environment, shas)
    if status == "ready":
        command = (
            f"{qa['command']} {url} Verify the fix for {tick.issue_url} on this preview "
            f"of {tick.pr_url}; post the report as a comment on "
            f"{tick.pr_url} whose first line is {QA_MARKER}"
        )
        return _queue(tick, command, "qa", "qa_running")
    if status == "failed":
        return _finish(tick, qa_note=render("qa_preview_failed"))
    return _wait_for_preview(tick)


def _wait_for_preview(tick: Tick) -> Result:
    qa = tick.config["qa"]
    # The budget is per wait: a count left by an earlier round must not shorten
    # (or skip) the wait for this head's preview.
    waits = tick.record.get("qa_waits", 0) if tick.record.get("phase") == "qa" else 0
    if waits >= qa["max_wait_ticks"]:
        return _finish(tick, qa_note=render("qa_preview_timeout"))
    tick.save(phase="qa", qa_waits=waits + 1)
    when = _schedule_tick(tick, qa["wait_minutes"], "qa-wait")
    return True, f"Preview not ready; next check at {when:%H:%M}"


def _after_qa(tick: Tick) -> Result:
    """Hand off; say whether the QA run left a report (it may have timed out)."""
    comments = gh.fetch_comments(tick.owner, tick.repo, tick.pr_number)
    since = tick.record.get("phase_started_at", 0)
    report = gh.latest_bot_comment(comments, tick.bot, since, match=lambda body: QA_MARKER in body)
    return _finish(tick, qa_note=render("qa_ran" if report else "qa_no_report"))


def _finish(tick: Tick, qa_note: str) -> Result:
    pr_url = tick.pr_url
    gh.hand_off_pr(tick.owner, tick.repo, tick.pr_number, _reviewer(tick),
                   is_draft=bool(_pr(tick).get("isDraft")))
    product = gh.is_team_member(tick.owner, tick.config["product_team"], tick.requester)
    _move_board(tick, "product_review" if product else "review")
    tick.comment("done", pr_url=pr_url, ci_note=_ci_note(tick), qa_note=qa_note)
    tick.save(phase="done", resume=None)
    return True, f"{pr_url} handed off to @{tick.requester or '(nobody)'}"


def _ci_note(tick: Tick) -> str:
    """State the head's CI as it is now, never as an assumption."""
    state = _head_ci(tick)
    if state == "success":
        return render("ci_green")
    if state in ("no_ci", "missing"):
        return render("ci_none")
    return render("ci_other", state=state)


def _reviewer(tick: Tick) -> str:
    return "" if gh.same_login(tick.requester, tick.bot) else tick.requester


# -- re-entry, gates, shared steps ----------------------------------------------


def _reenter(tick: Tick) -> Result:
    """A human `handle` on a finished pipeline: another round, or a fresh start."""
    state = _pr(tick)["state"] if tick.record.get("pr_number") else "CLOSED"
    if state == "MERGED":
        tick.comment("merged", pr_url=tick.pr_url)
        tick.save(phase="stopped")
        return True, f"{tick.pr_url} already merged"
    if _issue_closed(tick):
        return _stop_on_closed_issue(tick)
    if state == "CLOSED":
        keep = {"requester": tick.requester} if tick.requester else {}
        handle_state.reset_record(tick.instance_dir, tick.issue_url, **keep)
        tick.record, tick.pr_view = dict(keep), None
        return _start(tick)
    tick.comment("new_round", pr_url=tick.pr_url)
    tick.save(review_round=0)
    _move_board(tick, "in_progress")
    return _queue_fix(tick)


def _resume(tick: Tick) -> Result:
    resume = tick.record.get("resume") or "triage"
    if resume == "retry":
        tick.save(phase=tick.record.get("failed_phase", ""), resume=None)
        return _dispatch(tick)
    if not _replied_since_gate(tick):
        # A poke queued while the tick that opened this gate was running
        # predates its question: resuming would redo the step unanswered.
        gh.post_comment(tick.owner, tick.repo, tick.number,
                        render("gate_open", mention="", bot=tick.bot))
        return True, f"Gate still open on {tick.issue_url}: no reply since the question"
    return _RESUME[resume](tick)


def _replied_since_gate(tick: Tick) -> bool:
    """True when a human commented after the gate — on the issue or on its PR.

    The comment is the answer the gate asks for, or the `@<bot> handle` itself;
    gates about CI or review are often answered on the PR, as a comment, an
    inline review comment or a review.
    """
    gated_at = tick.record.get("gated_at")
    if gated_at is None:
        return True
    threads = list(tick.comments)
    if tick.record.get("pr_number"):
        threads += gh.fetch_comments(tick.owner, tick.repo, tick.pr_number)
        threads += gh.fetch_review_activity(tick.owner, tick.repo, tick.pr_number)
    return any(
        c.get("created_at", 0) > gated_at and not c.get("is_bot")
        and not gh.same_login(c.get("author") or "", tick.bot)
        for c in threads
    )


def _stop(tick: Tick, state: str) -> Result:
    tick.comment("stopped", pr_url=tick.pr_url, state=render(f"state_{state}"))
    tick.save(phase="stopped", resume=None)
    return True, f"{tick.pr_url} {state}; pipeline stopped"


def _issue_closed(tick: Tick) -> bool:
    return tick.issue.get("state") == "closed"


def _stop_on_closed_issue(tick: Tick) -> Result:
    """Closing the issue is how a human calls the pipeline off."""
    tick.comment("issue_closed")
    tick.save(phase="stopped", resume=None)
    return True, f"{tick.issue_url} is closed; pipeline stopped"


def _queue(tick: Tick, command: str, after: str, phase: str) -> Result:
    handle_state.queue_step(tick.instance_dir, tick.project_name, tick.issue_url, command, after)
    tick.save(phase=phase, phase_started_at=time.time(), resume=None)
    return True, f"Queued {command.split()[0]} for {tick.issue_url}"


def _gate(tick: Tick, key: str, resume: str, **values) -> Result:
    gated_at = time.time()
    tick.comment(key, **values)
    tick.save(phase="waiting_human", resume=resume, gated_at=gated_at)
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


def _follow_up_pending(tick: Tick, phase: str) -> bool:
    """True while the tick chained after this phase's step is still queued."""
    from app.missions import is_duplicate_mission

    path = Path(tick.instance_dir) / "missions.md"
    content = path.read_text() if path.exists() else ""
    return any(
        is_duplicate_mission(content, handle_state.handle_entry(tick.project_name, tick.issue_url, a))
        for a in _EXPECTED_AFTER[phase]
    )


def _recheck_pending(tick: Tick, after: str) -> bool:
    """True when this tick should leave the decision to a scheduled re-check.

    The re-check tick itself decides. A retry after an error, or a lost event
    file, finds none and re-evaluates instead of answering "busy" forever.
    """
    if tick.after == after:
        return False
    wanted = handle_state.handle_entry(tick.project_name, tick.issue_url, after)
    for path in (Path(tick.instance_dir) / "events").glob("*.json"):
        try:
            if json.loads(path.read_text()).get("mission", "").strip() == wanted:
                return True
        except (OSError, ValueError):
            continue
    return False


def _move_board(tick: Tick, column_key: str) -> None:
    board = tick.config["board"]
    gh.move_on_board(board, tick.issue.get("node_id", ""), board.get(column_key) or "")


# The `after:` tags a chained tick may carry in each phase; any other chained
# tick is stale. A gate, `done` and `stopped` accept human pokes only.
_EXPECTED_AFTER: Dict[str, Tuple[str, ...]] = {
    "planning": ("plan",),
    "implementing": ("implement",),
    "ci": ("ci", "ci-poll"),
    "reviewing": ("review",),
    "fixing": ("fix",),
    "qa": ("qa-wait",),
    "qa_running": ("qa",),
}

# Phases waiting on a queued step mission whose follow-up tick will judge it.
_STEP_PHASES = ("planning", "implementing", "reviewing", "fixing", "qa_running")

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
    # Emitted by the shared URL+context command builder; /handle has no use for it.
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
