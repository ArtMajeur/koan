"""Runner for /handle — one tick of the issue-to-PR pipeline.

Each run reads the issue's phase from ``app.handle_state``, does that phase's
cheap work, then queues the next step (followed by a ``/handle`` tick) or stops
at a human gate. Heavy work always runs as its own mission. A tick whose
context starts with ``after:`` was queued by the pipeline itself; any other
tick is a human poke. See ``specs/skills/handle.md``.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from app import handle_state
from skills.core.handle import handle_ci, handle_data_guard
from skills.core.handle import handle_github as gh
from skills.core.handle.handle_messages import marked_key, render, with_marker
from skills.core.handle.handle_parse import (
    has_plan,
    open_questions,
    parse_follow_ups,
    parse_triage,
    review_verdict,
)

_SKILL_DIR = Path(__file__).parent
_TRIAGE_TOOLS = ["Read", "Glob", "Grep"]
_TERMINAL_PR_STATES = ("CLOSED", "MERGED")
QA_MARKER = "<!-- koan-handle-qa -->"
# The poker's own words must ask for an issue, verb and noun, before any model
# reads the thread: text other participants wrote can then shape a requested
# issue, never cause one. English and French, like the comment templates.
_ISSUE_WORD_RE = re.compile(r"\b(?:issues?|tickets?)\b", re.IGNORECASE)
_CREATE_WORD_RE = re.compile(
    r"\b(?:creat(?:e|es|ed|ing)|open(?:s|ed|ing)?|fil(?:e|es|ed|ing)|add(?:s|ed|ing)?"
    r"|track(?:s|ed|ing)?|split(?:s|ting)?|rais(?:e|es|ed|ing)|log(?:s|ged|ging)?"
    r"|mak(?:e|es|ing)"
    r"|cr[ée](?:e|es|ez|er|ons)|ouvr(?:e|es|ez|ir|ons)|ajout(?:e|es|ez|er|ons)"
    r"|fai(?:s|tes|re)|mets|mettez|mettre)\b",
    re.IGNORECASE,
)
# `@<bot> handle confirm` opens a pending draft (see _confirm_follow_ups).
_CONFIRM_RE = re.compile(r"\s*confirm(?:e|er|ed)?\b", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# "GH-123" is a reference too.
_GH_REF_RE = re.compile(r"\bGH-(?=\d)", re.IGNORECASE)
# Every "@name", code spans and emphasis ("_@name_") included: guessing
# Markdown's parse would leave a way to keep a mention live. An address
# ("a@b.c") is not a mention.
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9])@(?=\w)")

Result = Tuple[bool, str]


class Outcome(NamedTuple):
    """What a tick hands the agent loop for its one chat message."""

    ok: bool
    summary: str
    milestone: str = "progress"  # an outcome_line.PIPELINE_MILESTONES key
    url: str = ""
    note: str = ""  # the line under the chat message; the summary when empty


# Chat's one line on why the pipeline stopped, per gate comment key.
_GATE_NOTES = {
    "triage_needs_human": "Triage needs an answer",
    "triage_not_a_bug": "Triage found no code change to make",
    "plan_questions": "The plan has open questions",
    "plan_missing": "No fresh plan was produced",
    "implement_no_pr": "The implementation opened no pull request",
    "data_change": "The pull request changes data and needs a human: {pr_url}",
    "ci_blocked": "CI is stuck ({outcome}): {pr_url}",
    "review_exhausted": "Review still blocking after {rounds} fix rounds: {pr_url}",
    "review_missing": "The review left no verdict for the latest commit: {pr_url}",
}


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
    poker: str = ""  # author of the `@<bot> handle` comment that made this poke
    request: str = ""  # what that comment says besides the command
    request_at: float = 0  # when it was written
    pr_view: Optional[dict] = field(default=None, repr=False)
    pr_projects: Dict[Tuple[str, str], str] = field(default_factory=dict, repr=False)
    milestone: str = "progress"
    note: str = ""

    @property
    def chained(self) -> bool:
        """Queued by the pipeline itself (follow-up, CI relay, re-check)."""
        return bool(self.after)

    @property
    def is_pr(self) -> bool:
        """The pipeline drives a PR no issue pipeline tracks (see _adopt_pr)."""
        return bool(self.issue.get("is_pr"))

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def pr_ref(self) -> Tuple[str, str]:
        """Owner and repository of the pull request.

        The issue's own, unless the issue was transferred: the PR stays where
        it was opened, and pairing its number with the issue's new repository
        would address somebody else's pull request.
        """
        from app.github_url_parser import parse_github_url

        url = self.record.get("pr_url")
        if not url:
            return self.owner, self.repo
        owner, repo, _kind, _number = parse_github_url(url)
        return owner, repo

    @property
    def pr_full_repo(self) -> str:
        return "/".join(self.pr_ref)

    @property
    def pr_project(self) -> str:
        """The project whose checkout holds the pull request's branch.

        Steps that work on the PR (review, fix, CI fixes, QA) run there: in the
        issue's project they would fetch and push the branch in another
        repository. With no project for the PR's repository there is nowhere
        safe to run them: the tick fails (error gate) rather than fall back.
        Resolved once per tick, so every step of a tick agrees.
        """
        ref = self.pr_ref
        if ref == (self.owner, self.repo):
            return self.project_name
        if ref not in self.pr_projects:
            self.pr_projects[ref] = _resolve_project(*ref)[0]
        if not self.pr_projects[ref]:
            raise RuntimeError(
                f"no project is configured for {'/'.join(ref)}, where {self.record['pr_url']} "
                "lives: its review, fixes and QA have no checkout to run in")
        return self.pr_projects[ref]

    @property
    def pr_config(self) -> dict:
        """The `handle:` settings that describe the PR's repository (CI, data, QA)."""
        from app.config import get_handle_config

        project = self.pr_project
        return self.config if project == self.project_name else get_handle_config(project)

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
        values.setdefault("mention", self.mention)
        body = render(key, bot=self.bot, **values)
        gh.post_comment(self.owner, self.repo, self.number, with_marker(body, self.issue_url))


def _mention(login: str) -> str:
    return f"@{login}: " if login else ""


# -- entry point ------------------------------------------------------------


def run_handle(
    url: str, project_path: str, project_name: str, instance_dir: str, context: str = "",
) -> Outcome:
    from app.github_url_parser import parse_github_url

    bot = _bot_login()
    if not bot:
        return Outcome(False, "github.nickname is not configured; /handle needs the bot login")
    try:
        owner, repo, kind, number = parse_github_url(url)
    except ValueError as e:
        return Outcome(False, f"Not a GitHub URL: {e}")
    issue_url = f"https://github.com/{owner}/{repo}/issues/{number}"
    tick = None
    try:
        # A PR an issue's pipeline opened belongs to that issue, also when it is
        # addressed as /issues/<n>; any other PR is a pipeline of its own, keyed
        # by its own URL.
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        tracked = handle_state.find_issue_for_pr(instance_dir, pr_url)
        if tracked:
            issue_url = tracked
        elif kind == "pull":
            issue_url = pr_url
        tick = _load_tick(issue_url, project_path, project_name, instance_dir, bot, context)
        confirmed = _confirm_follow_ups(tick)
        if confirmed is not None:
            return Outcome(*confirmed, "progress", tick.issue_url, tick.note)
        _propose_follow_ups(tick)
        ok, summary = _dispatch(tick)
        _wake_parent(tick)
        subject = tick.issue_url
        if tick.milestone == "handoff":  # a delegating issue has no PR of its own
            subject = tick.record.get("pr_url") or tick.issue_url
        return Outcome(ok, summary, tick.milestone, subject, tick.note)
    # Runner boundary: any failure is reported on the issue, never swallowed.
    except Exception as e:
        target = (tick.owner, tick.repo, tick.number) if tick else (owner, repo, number)
        return Outcome(
            *_fail(tick, target, bot, instance_dir, tick.issue_url if tick else issue_url, e))


def _wake_parent(tick: Tick) -> None:
    """A delegated PR that stopped moving lets its issue re-read where its PRs stand."""
    from app.utils import insert_pending_mission

    parent = tick.record.get("parent")
    if not parent or tick.milestone == "progress":
        return
    try:
        project = handle_state.load_record(tick.instance_dir, parent).get("project", "")
        insert_pending_mission(
            Path(tick.instance_dir) / "missions.md",
            handle_state.handle_entry(project, parent, "prs"))
    except (RuntimeError, OSError) as e:
        # The PR's own tick succeeded; a poke on the issue re-reads its PRs.
        print(f"[handle] could not wake {parent}: {e}", file=sys.stderr)


def _bot_login() -> str:
    from app.github_config import get_github_nickname
    from app.utils import load_config

    return get_github_nickname(load_config())


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
    if not tick.record:
        tick.record = _follow_transfer(tick)
    elif tick.record.get("moved_from"):
        # Another tick renamed the record first; this one may still carry the
        # project it was queued under before the transfer.
        _run_under_the_new_repository(tick)
    _keep_children(tick)
    tick.requester = _resolve_requester(tick)
    return tick


def _follow_transfer(tick: Tick) -> dict:
    """The record this issue had under its previous URL, renamed to the current one.

    A transfer gives the issue a new URL; the bot's own comments moved with it
    and still name the old record key.
    """
    from app.github_url_parser import parse_github_url

    old_url = _former_record_key(tick)
    if not old_url:
        return {}
    try:
        owner, repo, _kind, number = parse_github_url(old_url)
        now_at = gh.fetch_issue(owner, repo, number).get("html_url", "")
    except (ValueError, RuntimeError) as e:
        print(f"[handle] cannot check where {old_url} moved: {e}", file=sys.stderr)
        return {}
    # GitHub redirects a transferred issue's old URL; anything else is a
    # marker quoted from another issue, which must not move that record.
    if not handle_state.same_pr_url(now_at, tick.issue_url):
        return {}
    print(f"[handle] {old_url} was transferred to {tick.issue_url}", file=sys.stderr)
    _run_under_the_new_repository(tick)
    return handle_state.rename_record(
        tick.instance_dir, old_url, tick.issue_url, project=tick.project_name)


def _keep_children(tick: Tick) -> None:
    """After a transfer, the issue's PRs name it by its new URL."""
    former = {url for url, _project in handle_state.former_keys(tick.record)}
    if not former:
        return
    for url in tick.record.get("prs") or []:
        if handle_state.load_record(tick.instance_dir, url).get("parent") in former:
            handle_state.update_record(tick.instance_dir, url, parent=tick.issue_url)


def _former_record_key(tick: Tick) -> str:
    """The key of the record the bot's comments on this thread point to ("" if none).

    A comment names the key the record had when it was posted; the record may
    have moved since (a second transfer before the next comment), so the key
    is looked up among the URLs each record was known by.
    """
    seen = set()
    for comment in reversed(tick.comments):
        named = marked_key(comment.get("body") or "")
        if not named or named in seen or not gh.same_login(comment.get("author") or "", tick.bot):
            continue
        seen.add(named)
        key = handle_state.find_moved(tick.instance_dir, named)
        if key and not handle_state.same_pr_url(key, tick.issue_url):
            return key
    return ""


def _run_under_the_new_repository(tick: Tick) -> None:
    """Switch the tick to the project of the issue's new repository.

    A tick queued before the transfer still carries the old repository's
    project: planning or implementing there would work in the wrong checkout.
    """
    from app.config import get_handle_config

    name, path = _resolve_project(tick.owner, tick.repo)
    if name and name != tick.project_name:
        tick.project_name, tick.project_path = name, path
        tick.config = get_handle_config(name)


def _resolve_project(owner: str, repo: str) -> Tuple[str, str]:
    """``(name, path)`` of the project configured for a repository, or two ""."""
    from app.github_skill_helpers import resolve_project_for_repo

    path, name = resolve_project_for_repo(repo, owner=owner)
    return name or "", path or ""


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
    tick.poker, tick.request, tick.request_at = gh.latest_handle_request(
        _mention_thread(tick), tick.bot, since)
    candidates = [tick.poker]
    if not stored:
        candidates.append(tick.issue.get("author", ""))
    for login in candidates:
        if login and gh.same_login(login, stored):
            return stored
        if login and gh.is_authorized(tick.owner, tick.repo, login, tick.project_name):
            tick.save(requester=login)
            return login
    return stored


def _mention_thread(tick: Tick) -> List[dict]:
    """Where a `@<bot> handle` can sit: on a PR, also in a review or an inline comment."""
    if not tick.is_pr:
        return tick.comments
    activity = gh.fetch_review_activity(tick.owner, tick.repo, tick.number)
    return sorted(tick.comments + activity, key=lambda c: c.get("created_at", 0))


def _propose_follow_ups(tick: Tick) -> None:
    """Draft the follow-up issues the poke explicitly asks for, and show the draft.

    Nothing is opened here: the drafts come from a thread anyone can write in,
    and exist as issues only once their requester has read and confirmed them.
    """
    request = tick.request.strip()  # the poker's own words: code and quotes left out upstream
    if not (_ISSUE_WORD_RE.search(request) and _CREATE_WORD_RE.search(request)):
        return
    if tick.request_at <= tick.record.get("request_at", 0):
        return  # this comment was already answered (a re-run tick, a later poke from chat)
    # Checked now and here: a stored requester may have lost the right since,
    # or hold it on another repository than this one.
    if not gh.is_authorized(tick.owner, tick.repo, tick.poker, tick.project_name):
        return
    tick.save(request_at=tick.request_at)  # before the model runs: a comment is drafted once
    try:
        items = [
            {"title": " ".join(_defused(item["title"]).split()), "body": _defused(item["body"])}
            for item in parse_follow_ups(_run_request(tick, request))
        ]
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        # No reply: the pre-check also passes on ordinary sentences, whose
        # authors asked for nothing.
        print(f"[handle] follow-up draft failed: {e}", file=sys.stderr)
        return
    if not items:
        return
    tick.save(proposal={"by": tick.poker, "at": tick.request_at, "items": items})
    try:
        tick.comment("follow_ups_proposed", repo=tick.full_repo, drafts=_render_drafts(items))
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        # A draft nobody could read must not be confirmable.
        print(f"[handle] follow-up proposal not posted on {tick.issue_url}: {e}", file=sys.stderr)
        tick.save(proposal=None)


def _render_drafts(items: List[dict]) -> str:
    return "\n\n".join(
        f"**{n}. {item['title']}**\n\n<details><summary>Body</summary>\n\n{item['body']}\n\n</details>"
        for n, item in enumerate(items, 1)
    )


def _confirm_follow_ups(tick: Tick) -> Optional[Result]:
    """Open the pending drafts when their requester confirms; None for any other tick.

    A confirmation only confirms: the caller ends the tick there, so the same
    comment cannot also resume a gate or approve a data change.
    """
    proposal = tick.record.get("proposal")
    if not proposal or not _CONFIRM_RE.match(tick.request):
        return None
    if tick.request_at <= proposal.get("at", 0):
        return None
    # The one who asked read the draft: nobody else answers for them.
    if not gh.same_login(tick.poker, proposal.get("by", "")) or not gh.is_authorized(
            tick.owner, tick.repo, tick.poker, tick.project_name):
        return None
    # Cleared first: whatever fails below, a proposal is opened once.
    tick.save(proposal=None, request_at=tick.request_at)
    urls, error = [], ""
    try:
        # One by one into the list: what exists when a creation fails is kept.
        urls.extend(
            gh.create_issue(tick.owner, tick.repo, item["title"], _follow_up_body(tick, item))
            for item in proposal.get("items") or []
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        from app.leak_detector import scan_and_redact

        print(f"[handle] follow-up issues failed: {e}", file=sys.stderr)
        error = scan_and_redact(" ".join(str(e).replace("`", "'").split())[:300],
                                "/handle follow-up comment") or type(e).__name__
    _report_follow_ups(tick, urls, error)
    tick.note = f"{len(urls)} follow-up issue(s) opened"
    return not error, f"{len(urls)} follow-up issue(s) opened from {tick.issue_url}"


def _report_follow_ups(tick: Tick, urls: List[str], error: str) -> None:
    """Say what was opened and what was not. A lost reply must not lose the tick."""
    try:
        if urls:
            tick.comment("follow_ups_created", issues="\n".join(f"- {url}" for url in urls))
        if error:
            tick.comment("follow_ups_failed", error=error)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print(f"[handle] follow-up issues not reported on {tick.issue_url} ({e}); "
              f"opened: {', '.join(urls) or 'none'}", file=sys.stderr)


def _follow_up_body(tick: Tick, item: dict) -> str:
    footer = render("follow_up_footer", issue_url=tick.issue_url, login=tick.poker)
    return f"{item['body']}\n\n---\n{footer}"


def _defused(text: str) -> str:
    """Keep a drafted issue from reaching anyone else: its text comes from the thread.

    No mention notifies, no bare ``#123`` or ``GH-123`` back-links another
    issue, and no control character reaches the command that opens it.
    """
    from app.github import escape_issue_refs

    text = _CONTROL_RE.sub("", text)
    return _GH_REF_RE.sub("GH-\u200b", escape_issue_refs(_MENTION_RE.sub("@\u200b", text)))


def _run_request(tick: Tick, request: str) -> str:
    from app.language_preference import get_language_instruction
    from app.prompts import load_skill_prompt
    from app.provider import run_command

    # On a PR the request can point at a review or an inline comment.
    comments = "\n\n".join(
        f"@{c.get('author')}: {c.get('body')}" for c in _mention_thread(tick))
    prompt = load_skill_prompt(
        _SKILL_DIR, "request", project_path=tick.project_path,
        REPO=tick.full_repo, NUMBER=tick.number,
        TITLE=tick.issue.get("title", ""), BODY=tick.issue.get("body") or "",
        COMMENTS=comments or "(none)", REQUEST=request,
        LANGUAGE_INSTRUCTION=get_language_instruction(),
    )
    # The prompt carries text any participant wrote: the read-only role denies
    # write tools at the provider, and one turn leaves no room to explore.
    return run_command(
        prompt=prompt, project_path=tick.project_path, allowed_tools=[],
        model_key="review_mode", max_turns=1, timeout=300, project_name=tick.project_name,
    )


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
        gh.post_comment(*target, with_marker(render(
            "error", mention=_mention(requester), bot=bot,
            phase=phase or "start", error=detail,
        ), issue_url))
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
    if tick.after == "delegate":
        return _start_delegated(tick)
    if tick.chained and tick.after not in _EXPECTED_AFTER.get(phase, ()):
        # A follow-up, relay or re-check left over from an earlier path (the
        # pipeline gated, a poke moved it on): acting on it would repeat work
        # no human asked for.
        return True, f"Stale after:{tick.after} tick ignored in phase {phase or 'new'}"
    if tick.record.get("pr_url"):
        tick.pr_project  # fails the tick before any step when the PR has no checkout
    if phase in ("done", "stopped"):
        return _reenter(tick)
    if not tick.chained and phase in handle_state.STEP_PHASES and _follow_up_pending(tick, phase):
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
        # A pull-request pipeline is its own PR, known before the record names it.
        number = tick.number if tick.is_pr else tick.pr_number
        tick.pr_view = gh.pr_info(*tick.pr_ref, number)
    return tick.pr_view


def _head_ci(tick: Tick) -> str:
    return handle_ci.ci_at_head(tick.record["branch"], tick.pr_full_repo, _pr(tick)["headRefOid"])


# -- triage and plan ----------------------------------------------------------


def _start(tick: Tick) -> Result:
    if tick.is_pr:
        return _adopt_pr(tick)
    tick.save(project=tick.project_name, phase="triage")
    # An issue that already has pull requests is not triaged or implemented again.
    adopted = _adopt_linked_prs(tick, _linked_prs(tick))
    if adopted and tick.record.get("phase") == "stopped":
        return adopted  # its PRs belong to another issue: this one is not taken
    gh.assign_self(tick.owner, tick.repo, tick.number)
    _move_board(tick, "in_progress")
    return adopted or _triage_issue(tick)


def _adopt_pr(tick: Tick) -> Result:
    """Drive a PR no issue pipeline tracks: it already exists, so start at CI."""
    pr = _pr(tick)
    if pr.get("isCrossRepository"):
        # /rebase --fix pushes to the head branch and the CI read looks up runs
        # by branch name in this repository: neither reaches a fork.
        tick.comment("fork_pr")
        tick.milestone, tick.note = "stopped", "The pull request comes from a fork"
        return True, f"{tick.issue_url} comes from a fork; not handled"
    tick.save(project=tick.project_name, pr_url=pr["url"], pr_number=tick.number,
              branch=pr["headRefName"], review_round=0)
    return _await_ci(tick)


def _triage(tick: Tick) -> Result:
    """Resume at triage: the PRs linked since the gate come first."""
    return _adopt_linked_prs(tick, _linked_prs(tick)) or _triage_issue(tick)


def _triage_issue(tick: Tick) -> Result:
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
    since = tick.record.get("phase_started_at", 0)
    # A PR left by an earlier attempt does not prove this /implement worked.
    if pr is None or pr["updated_at"] < since:
        # The code may live in other repositories than the issue.
        opened = [
            p for p in _linked_prs(tick)
            if gh.same_login(p["author"], tick.bot) and p["updated_at"] >= since
        ]
        return _adopt_linked_prs(tick, opened) or _gate(tick, "implement_no_pr", "implement")
    owner = handle_state.pr_owner(tick.instance_dir, pr["url"])
    if owner not in ("", tick.issue_url):
        # Somebody drives this PR already (it was poked on its own, or another
        # issue it closes took it): one owner per PR, here as everywhere.
        settled = handle_state.load_record(tick.instance_dir, pr["url"])
        if handle_state.same_pr_url(owner, pr["url"]) and settled.get("phase") in (
                "done", "stopped"):
            # Its own pipeline had finished, and /implement just pushed to it:
            # the new head goes through CI and review like any other.
            keep = {"requester": settled["requester"]} if settled.get("requester") else {}
            handle_state.reset_record(tick.instance_dir, pr["url"], **keep)
        claimed = {"url": pr["url"], "owner": tick.owner, "repo": tick.repo,
                   "number": str(pr["number"]), "project": tick.project_name}
        return _delegate(tick, [claimed], []) or _gate(tick, "implement_no_pr", "implement")
    tick.save(pr_url=pr["url"], pr_number=str(pr["number"]), branch=branch, review_round=0)
    return _await_ci(tick)


# -- an issue's linked pull requests ----------------------------------------------


def _linked_prs(tick: Tick) -> List[dict]:
    """The issue's linked PRs, or none when GitHub cannot say.

    The lookup comes before steps that worked without it (triage, the PR an
    /implement opened): a linked repository the token cannot read must not
    stop them.
    """
    try:
        return gh.linked_prs(tick.owner, tick.repo, tick.number)
    except RuntimeError as e:
        print(f"[handle] linked pull requests of {tick.issue_url} unreadable: {e}", file=sys.stderr)
        return []


def _handleable(prs: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Split linked PRs into those a pipeline can drive and those it cannot.

    The first get their ``project``; the others say why in ``skip``.
    """
    ok, skipped = [], []
    for pr in prs:
        project = "" if pr["is_fork"] else _resolve_project(pr["owner"], pr["repo"])[0]
        if project:
            ok.append({**pr, "project": project})
        else:
            skipped.append({**pr, "skip": "fork" if pr["is_fork"] else "no_project"})
    return ok, skipped


def _adopt_linked_prs(tick: Tick, prs: List[dict]) -> Optional[Result]:
    """Carry on from the issue's pull requests; None when there is none to drive."""
    ok, skipped = _handleable(prs)
    if not ok:
        return None
    pr = ok[0]
    own_repo = (pr["owner"].lower(), pr["repo"].lower()) == (tick.owner.lower(), tick.repo.lower())
    # The single-PR pipeline only takes a PR nobody drives yet; one that has a
    # pipeline already keeps it, and the issue waits for it like for any other.
    if len(prs) == 1 and own_repo and not handle_state.pr_owner(tick.instance_dir, pr["url"]):
        tick.comment("pr_adopted", pr_url=pr["url"])
        tick.save(pr_url=pr["url"], pr_number=pr["number"], branch=pr["branch"], review_round=0)
        return _await_ci(tick)
    return _delegate(tick, ok, skipped)


def _delegate(tick: Tick, prs: List[dict], skipped: List[dict]) -> Optional[Result]:
    """Give each PR the issue owns a pipeline of its own, under its repository's project.

    None, with nothing written, when the issue owns none of them and no other
    issue does either: the caller goes on as it does without a linked PR.
    """
    handed_off = tick.record.get("phase") == "done"
    known = list(tick.record.get("prs") or [])
    plans = [_plan_pr(tick, pr) for pr in prs]
    plans += _unlisted_members(tick, known, {plan["url"] for plan in plans})
    members = [plan for plan in plans if plan["member"]]
    if not members:
        return _left_to_other_issues(tick, plans)
    if tick.record.get("pr_url"):
        # The issue no longer drives a PR itself: a reference left here would
        # route that PR's ticks to the issue instead of its own pipeline.
        tick.save(pr_url=None, pr_number=None, branch=None)
        tick.pr_view = None
    queued = sum(_start_pr(tick, plan) for plan in members)
    urls = [plan["url"] for plan in members]
    lines = [f"- {plan['url']}: {plan['state']}" for plan in plans]
    lines += [f"- {pr['url']}: {render('pr_state_' + pr['skip'])}" for pr in skipped]
    tick.note = f"{len(urls)} pull request(s), each handled on its own thread"
    # A poke on an issue already handed off, with nothing new to start or to
    # wait for: the status only.
    still_done = (handed_off and not queued and set(urls) <= set(known)
                  and all(plan["phase"] == "done" for plan in members))
    tick.save(phase="done" if still_done else "delegated", prs=urls, resume=None)
    tick.comment("prs_status", prs="\n".join(lines))
    if still_done:
        return True, f"{tick.issue_url} is handed off; nothing new"
    return _after_prs(tick)


def _left_to_other_issues(tick: Tick, plans: List[dict]) -> Optional[Result]:
    """Every linked PR belongs to another issue's pipeline: say so and stop.

    Triaging and implementing here would redo the fix under way there. None
    when no PR is owned elsewhere (they were only refused): the caller goes on.
    """
    owned = [plan for plan in plans if plan.get("owner")]
    if not owned:
        return None
    tick.comment("prs_owned", prs="\n".join(f"- {p['url']}: {p['state']}" for p in owned))
    tick.save(phase="stopped", resume=None)
    tick.milestone, tick.note = "stopped", "Its pull requests are handled from another issue"
    return True, f"{tick.issue_url}: its pull requests belong to other pipelines"


def _unlisted_members(tick: Tick, known: List[str], listed: set) -> List[dict]:
    """PRs the issue owns that GitHub no longer lists as closing it, still under way.

    An open one had its closing keyword edited out: dropping it here would hand
    the issue off without it. One that was closed or merged while parked (a
    gate gets no tick) is given a tick, so that its pipeline stops and stops
    holding the issue.
    """
    from app.github_url_parser import parse_github_url

    plans = []
    for url in known:
        record = handle_state.load_record(tick.instance_dir, url)
        phase = record.get("phase", "")
        if url in listed or record.get("parent") != tick.issue_url or phase in ("done", "stopped"):
            continue
        owner, repo, _kind, _number = parse_github_url(url)
        project = _resolve_project(owner, repo)[0]
        reach = _reach(url) if project else "lost"
        data_gate = phase == "waiting_human" and record.get("resume") == "data_change"
        state = {"gone": "stopped", "lost": "lost"}.get(reach) or (
            "data_gate" if data_gate else phase)
        plans.append({
            "url": url, "member": True, "reopened": False, "seed_requester": False,
            "phase": phase, "project": project, "state": _pr_state(state),
            # Only a parked pipeline needs the tick: one on its way has its own,
            # which finds the PR closed.
            "run": reach == "gone" and phase in ("", "waiting_human"),
            # No tick can run for a PR GitHub no longer serves, or that has no
            # project left: the issue stops waiting for it.
            "abandon": reach == "lost",
        })
    return plans


def _reach(pr_url: str) -> str:
    """``open``, ``gone`` (closed or merged) or ``lost`` (GitHub no longer serves it)."""
    from app.github_url_parser import parse_github_url

    owner, repo, _kind, number = parse_github_url(pr_url)
    try:
        return "open" if gh.pr_info(owner, repo, number)["state"] == "OPEN" else "gone"
    except RuntimeError as e:
        print(f"[handle] cannot read {pr_url}: {e}", file=sys.stderr)
        return "lost"


def _plan_pr(tick: Tick, pr: dict) -> dict:
    """What a poke on the issue means for one linked PR; nothing is written here.

    ``member``: the issue owns this PR and waits for it. ``run``: the PR gets a tick.
    """
    plan = {"url": pr["url"], "project": pr["project"], "member": False, "run": False,
            "reopened": False, "seed_requester": False, "phase": ""}
    # One owner per PR: unclaimed, ours, or handled on its own so far (the issue
    # then takes it over). Anything else is another issue's, and stays so.
    owner = handle_state.pr_owner(tick.instance_dir, pr["url"])
    if owner and owner != tick.issue_url and not handle_state.same_pr_url(owner, pr["url"]):
        return {**plan, "owner": owner, "state": render("pr_state_owned", issue_url=owner)}
    record = handle_state.load_record(tick.instance_dir, pr["url"])
    # Rights decide what a poke may start or claim, never which of its PRs the
    # issue waits for.
    authorized = not tick.requester or gh.is_authorized(
        pr["owner"], pr["repo"], tick.requester, pr["project"])
    if not authorized and owner != tick.issue_url:
        return {**plan, "state": render("pr_state_unauthorized")}
    phase = record.get("phase", "")
    # Linked PRs are open ones: a stopped pipeline's PR was closed, then reopened.
    reopened = authorized and phase == "stopped"
    if reopened:
        phase = ""
    data_gate = phase == "waiting_human" and record.get("resume") == "data_change"
    return {
        **plan, "member": True, "phase": phase, "reopened": reopened,
        # A PR on its way is left alone, and a handed-off one is not restarted:
        # only a new PR, or one parked at a gate, gets a tick. A data change is
        # approved on the PR's own thread, never by a poke on the issue.
        "run": authorized and (phase == "" or (phase == "waiting_human" and not data_gate)),
        "seed_requester": authorized and bool(tick.requester) and not record.get("requester"),
        "state": _pr_state("data_gate" if data_gate else phase),
    }


def _start_pr(tick: Tick, plan: dict) -> int:
    """Attach the PR's record to the issue and queue its tick; 1 when one was queued."""
    from app.utils import insert_pending_mission

    if plan.get("abandon"):
        handle_state.update_record(tick.instance_dir, plan["url"], phase="stopped", resume=None)
        return 0
    if plan["reopened"]:
        answered = handle_state.load_record(tick.instance_dir, plan["url"]).get("request_at")
        handle_state.reset_record(
            tick.instance_dir, plan["url"], **({"request_at": answered} if answered else {}))
    seed = {"parent": tick.issue_url}
    if plan["seed_requester"]:
        seed["requester"] = tick.requester
    handle_state.update_record(tick.instance_dir, plan["url"], **seed)
    if not plan["run"]:
        return 0
    # Tagged, so it is never read as a human poke: see _start_delegated.
    return int(insert_pending_mission(
        Path(tick.instance_dir) / "missions.md",
        handle_state.handle_entry(plan["project"], plan["url"], "delegate")))


def _start_delegated(tick: Tick) -> Result:
    """The tick an issue queued for one of its PRs (``after:delegate``).

    It may start that PR's pipeline or resume a gate the issue's poke answered,
    and nothing else. It acts on the PR's own record only: if an issue took the
    PR as its own since the tick was queued, the tick reaches that issue's
    record, where it must not stand in for the human reply its gate waits for.
    """
    record, phase = tick.record, tick.record.get("phase", "")
    if not (tick.is_pr and record.get("parent")):
        return True, f"Stale after:delegate tick ignored on {tick.issue_url}"
    if phase in ("done", "stopped"):
        return True, f"after:delegate tick ignored in phase {phase}"  # settled meanwhile
    if _issue_closed(tick):
        return _stop_on_closed_issue(tick)
    if phase == "":
        _fall_back_to_the_author(tick)
        return _start(tick)
    if phase == "waiting_human" and record.get("resume") != "data_change":
        if record.get("resume") == "retry":
            # The retry runs the failed phase through _dispatch again, where
            # this tag would send it straight back here and leave it undone.
            tick.after = ""
        return _resume(tick)
    return True, f"after:delegate tick ignored in phase {phase}"


def _fall_back_to_the_author(tick: Tick) -> None:
    """A PR started for an issue nobody is the requester of answers to its author.

    Same fallback as a human poke on the PR; a chained tick skips it.
    """
    author = tick.issue.get("author", "")
    if not tick.requester and author and gh.is_authorized(
            tick.owner, tick.repo, author, tick.project_name):
        tick.save(requester=author)
        tick.requester = author


def _pr_state(phase: str) -> str:
    key = {"": "new", "waiting_human": "gate", "data_gate": "data_gate",
           "done": "done", "stopped": "stopped", "lost": "lost"}.get(phase)
    return render(f"pr_state_{key}") if key else render("pr_state_running", phase=phase)


def _delegated(tick: Tick) -> Result:
    return _after_prs(tick) if tick.chained else _poke_delegated(tick)


def _poke_delegated(tick: Tick) -> Result:
    """A human `handle` on a delegating issue: look the PRs up again and report."""
    # A failed lookup must not read as "no PR left" (that starts over): it raises.
    ok, skipped = _handleable(gh.linked_prs(tick.owner, tick.repo, tick.number))
    result = _delegate(tick, ok, skipped)
    if result:
        return result
    return _stop_as_merged(tick) or _restart(tick)


def _stop_as_merged(tick: Tick) -> Optional[Result]:
    """Stop when one of the issue's PRs is merged; None when none is."""
    merged = [url for url in tick.record.get("prs") or [] if _is_merged(url)]
    if not merged:
        return None
    tick.comment("prs_merged", prs="\n".join(f"- {url}" for url in merged))
    tick.save(phase="stopped", resume=None)
    tick.milestone, tick.note = "stopped", "The pull requests are already merged"
    return True, f"{tick.issue_url}: its pull requests are merged"


def _stop_without_prs(tick: Tick) -> Result:
    """Every PR of the issue stopped and none is ready: say where that leaves the issue."""
    merged = _stop_as_merged(tick)
    if merged:
        return merged
    tick.comment("prs_closed", prs="\n".join(f"- {url}" for url in tick.record.get("prs") or []))
    tick.save(phase="stopped", resume=None)
    tick.milestone, tick.note = "stopped", "Its pull requests were closed"
    return True, f"{tick.issue_url}: its pull requests were closed"


def _is_merged(pr_url: str) -> bool:
    from app.github_url_parser import parse_github_url

    owner, repo, _kind, number = parse_github_url(pr_url)
    try:
        return gh.pr_info(owner, repo, number)["state"] == "MERGED"
    except RuntimeError as e:
        # A PR that can no longer be read (repository gone, access lost) is
        # not known to be merged; failing here would park the issue for good.
        print(f"[handle] cannot read {pr_url}: {e}", file=sys.stderr)
        return False


def _after_prs(tick: Tick) -> Result:
    """Hand the issue off once every PR is handed off (a closed one does not hold it)."""
    phases = {
        url: handle_state.load_record(tick.instance_dir, url).get("phase", "")
        for url in tick.record.get("prs") or []
    }
    ready = [url for url, phase in phases.items() if phase == "done"]
    if tick.chained and phases and all(phase == "stopped" for phase in phases.values()):
        # The last PR just stopped: without this the issue would sit in
        # `delegated` until somebody thought of poking it again.
        return _stop_without_prs(tick)
    if not ready or any(phase not in ("done", "stopped") for phase in phases.values()):
        return True, f"Waiting for the pull requests of {tick.issue_url}"
    _move_board(tick, _review_column(tick))
    tick.comment("prs_done", prs="\n".join(f"- {url}" for url in ready))
    tick.save(phase="done", resume=None)
    tick.milestone, tick.note = "handoff", "Every pull request is ready for review"
    return True, f"{tick.issue_url}: {len(ready)} pull request(s) handed off"


def _await_ci(tick: Tick) -> Result:
    flagged = _unapproved_data_changes(tick)
    if flagged:
        return _gate_data_changes(tick, flagged)
    if tick.pr_config["ready_before_ci"] and _pr(tick).get("isDraft"):
        # Some CIs skip draft PRs: without this the loop would review and fix
        # a PR whose CI never ran.
        gh.mark_ready(*tick.pr_ref, tick.pr_number)
        tick.pr_view["isDraft"] = False
    tick.save(phase="ci", ci_outcome=None, ci_polls=0, ci_empty_checks=0,
              phase_started_at=time.time(), resume=None)
    return _after_ci(tick)


def _unapproved_data_changes(tick: Tick) -> List[dict]:
    """PR files touching data (migrations, scripts, existing translations) not yet approved."""
    head = _pr(tick)["headRefOid"]
    files = gh.pr_files(*tick.pr_ref, tick.pr_number)
    total, head_after = gh.pr_changed_files(*tick.pr_ref, tick.pr_number)
    if head_after != head:
        # A push landed during the check: the listing may mix two heads, so it
        # proves nothing. Fail closed; the retry reads the new head.
        raise RuntimeError(f"PR head moved during the data-change check ({head} → {head_after})")
    approved = set(tick.record.get("data_changes_approved") or ())
    flagged = handle_data_guard.flagged_files(files, tick.pr_config["data_guard"])
    flagged += handle_data_guard.truncated_listing(len(files), total, head)
    return [f for f in flagged if handle_data_guard.approval_key(f) not in approved]


def _gate_data_changes(tick: Tick, flagged: List[dict]) -> Result:
    tick.save(data_changes_pending=[handle_data_guard.approval_key(f) for f in flagged])
    files = "\n".join(f"- `{f['filename']}` ({f['label']})" for f in flagged)
    return _gate(tick, "data_change", "data_change", pr_url=tick.pr_url, files=files)


def _approve_data_changes(tick: Tick) -> Result:
    """The poke after a data-change gate approves the listed files at their gated content."""
    approved = list(tick.record.get("data_changes_approved") or [])
    approved += tick.record.get("data_changes_pending") or []
    tick.save(data_changes_approved=approved, data_changes_pending=[])
    return _await_ci(tick)


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
        handle_ci.register(tick.instance_dir, tick.pr_project, tick.record, tick.pr_full_repo)
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
    # /ci_check may have pushed fixes since the check on entering ci; every
    # hand-off follows a review of the PR head, so checking here covers them.
    flagged = _unapproved_data_changes(tick)
    if flagged:
        return _gate_data_changes(tick, flagged)
    return _queue(tick, f"/review {tick.pr_url} --force", "review", "reviewing", tick.pr_project)


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
    wanted = f"{'_'.join(tick.pr_ref)}_{tick.pr_number}.json"
    if folder.is_dir():
        for path in folder.iterdir():
            if path.name.lower() == wanted.lower():
                return path
    return folder / wanted


def _queue_fix(tick: Tick) -> Result:
    tick.save(review_round=tick.record.get("review_round", 0) + 1)
    return _queue(tick, f"/rebase {tick.pr_url} --fix", "fix", "fixing", tick.pr_project)


# -- QA and hand-off ------------------------------------------------------------


def _qa_or_finish(tick: Tick) -> Result:
    qa = tick.pr_config["qa"]
    if not (qa["command"] and qa["preview_environment"]):
        return _finish(tick, qa_note="")
    if tick.record.get("phase") == "qa" and _recheck_pending(tick, "qa-wait"):
        return _waiting(tick, "qa")  # a poke must not add a re-check or burn a wait
    number = tick.pr_number
    shas = (_pr(tick)["headRefOid"], gh.merge_commit_sha(*tick.pr_ref, number))
    environment = qa["preview_environment"].replace("{pr}", number)
    status, url = gh.preview_status(*tick.pr_ref, environment, shas)
    if status == "ready":
        command = (
            f"{qa['command']} {url} Verify the fix for {tick.issue_url} on this preview "
            f"of {tick.pr_url}; post the report as a comment on "
            f"{tick.pr_url} whose first line is {QA_MARKER}"
        )
        return _queue(tick, command, "qa", "qa_running", tick.pr_project)
    if status == "failed":
        return _finish(tick, qa_note=render("qa_preview_failed"))
    return _wait_for_preview(tick)


def _wait_for_preview(tick: Tick) -> Result:
    qa = tick.pr_config["qa"]
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
    # A pull-request pipeline's own thread is the one this tick already read.
    comments = tick.comments if tick.is_pr else gh.fetch_comments(
        *tick.pr_ref, tick.pr_number)
    since = tick.record.get("phase_started_at", 0)
    report = gh.latest_bot_comment(comments, tick.bot, since, match=lambda body: QA_MARKER in body)
    return _finish(tick, qa_note=render("qa_ran" if report else "qa_no_report"))


def _finish(tick: Tick, qa_note: str) -> Result:
    # A commit pushed after the review was queued, or during the preview wait,
    # has not been through the check.
    flagged = _unapproved_data_changes(tick)
    if flagged:
        return _gate_data_changes(tick, flagged)
    pr_url = tick.pr_url
    gh.hand_off_pr(*tick.pr_ref, tick.pr_number, _reviewer(tick),
                   is_draft=bool(_pr(tick).get("isDraft")))
    _move_board(tick, _review_column(tick))
    state = _head_ci(tick)
    tick.comment("done", pr_url=pr_url, ci_note=_ci_note(state), qa_note=qa_note)
    tick.save(phase="done", resume=None)
    tick.milestone, tick.note = "handoff", _ci_chat_note(state)
    return True, f"{pr_url} handed off to @{tick.requester or '(nobody)'}"


def _review_column(tick: Tick) -> str:
    product = gh.is_team_member(tick.owner, tick.config["product_team"], tick.requester)
    return "product_review" if product else "review"


def _ci_note(state: str) -> str:
    """State the head's CI as it is now, never as an assumption."""
    if state == "success":
        return render("ci_green")
    if state in ("no_ci", "missing"):
        return render("ci_none")
    return render("ci_other", state=state)


def _ci_chat_note(state: str) -> str:
    if state == "success":
        return "CI green"
    if state in ("no_ci", "missing"):
        return "No CI ran on the latest commit"
    return f"CI on the latest commit is {state}"


def _reviewer(tick: Tick) -> str:
    """Who the hand-off asks for a review: the requester, unless GitHub would refuse."""
    if gh.same_login(tick.requester, tick.bot):
        return ""
    if tick.is_pr and gh.same_login(tick.requester, tick.issue.get("author") or ""):
        return ""  # a review cannot be requested from the PR's own author
    return tick.requester


# -- re-entry, gates, shared steps ----------------------------------------------


def _reenter(tick: Tick) -> Result:
    """A human `handle` on a finished pipeline: another round, or a fresh start."""
    if tick.record.get("prs"):
        return _stop_on_closed_issue(tick) if _issue_closed(tick) else _poke_delegated(tick)
    state = _pr(tick)["state"] if tick.record.get("pr_number") else "CLOSED"
    if state == "MERGED":
        tick.comment("merged", pr_url=tick.pr_url)
        tick.save(phase="stopped")
        tick.milestone, tick.note = "stopped", "The pull request is already merged"
        return True, f"{tick.pr_url} already merged"
    if _issue_closed(tick):
        return _stop_on_closed_issue(tick)
    if state == "CLOSED":
        return _restart(tick)
    tick.comment("new_round", pr_url=tick.pr_url)
    tick.save(review_round=0)
    _move_board(tick, "in_progress")
    return _queue_fix(tick)


def _restart(tick: Tick) -> Result:
    """Start over from triage, keeping the requester and the request already answered."""
    keep = {"requester": tick.requester} if tick.requester else {}
    for key in ("request_at", "proposal"):  # or the same comment asks again
        if tick.record.get(key):
            keep[key] = tick.record[key]
    handle_state.reset_record(tick.instance_dir, tick.issue_url, **keep)
    tick.record, tick.pr_view = dict(keep), None
    return _start(tick)


def _resume(tick: Tick) -> Result:
    resume = tick.record.get("resume") or "triage"
    if resume == "retry":
        tick.save(phase=tick.record.get("failed_phase", ""), resume=None)
        return _dispatch(tick)
    if not _replied_since_gate(tick):
        # A poke queued while the tick that opened this gate was running
        # predates its question: resuming would redo the step unanswered.
        tick.comment("gate_open", mention="")
        tick.milestone, tick.note = "gate", "Still waiting for an answer in the thread"
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
        if not tick.is_pr:  # a pull-request pipeline's thread is tick.comments already
            threads += gh.fetch_comments(*tick.pr_ref, tick.pr_number)
        threads += gh.fetch_review_activity(*tick.pr_ref, tick.pr_number)
    # A delegated PR's gate is also answered on its issue, except a data
    # change: approving one takes a look at the PR, on the PR.
    if tick.record.get("parent") and tick.record.get("resume") != "data_change":
        from app.github_url_parser import parse_github_url

        owner, repo, _kind, number = parse_github_url(tick.record["parent"])
        threads += gh.fetch_comments(owner, repo, number)
    return any(
        c.get("created_at", 0) > gated_at and not c.get("is_bot")
        and not gh.same_login(c.get("author") or "", tick.bot)
        for c in threads
    )


def _stop(tick: Tick, state: str) -> Result:
    tick.comment("stopped", pr_url=tick.pr_url, state=render(f"state_{state}"))
    tick.save(phase="stopped", resume=None)
    tick.milestone, tick.note = "stopped", f"The pull request is {state}"
    return True, f"{tick.pr_url} {state}; pipeline stopped"


def _issue_closed(tick: Tick) -> bool:
    return tick.issue.get("state") == "closed"


def _stop_on_closed_issue(tick: Tick) -> Result:
    """Closing the issue is how a human calls the pipeline off.

    A pull-request pipeline's "issue" is the PR itself, closed or merged.
    """
    if not tick.is_pr:
        tick.comment("issue_closed")
        tick.note = "The issue is closed"
    elif _pr(tick)["state"] == "MERGED":
        tick.comment("merged", pr_url=tick.issue_url)
        tick.note = "The pull request is merged"
    else:
        tick.comment("pr_closed")
        tick.note = "The pull request is closed"
    tick.milestone = "stopped"
    tick.save(phase="stopped", resume=None)
    return True, f"{tick.issue_url} is closed; pipeline stopped"


def _queue(tick: Tick, command: str, after: str, phase: str, project: str = "") -> Result:
    """Queue a step and the tick that follows it.

    *project* is where the step runs when that is not the issue's project (a
    step on a PR that stayed in another repository); the tick stays with the issue.
    """
    handle_state.queue_step(
        tick.instance_dir, tick.project_name, tick.issue_url, command, after,
        step_project=project or tick.project_name)
    # `step` lets the agent loop tell this mission from an operator's own.
    tick.save(phase=phase, phase_started_at=time.time(), resume=None, step=command)
    tick.note = f"{command.split()[0]} queued"
    return True, f"Queued {command.split()[0]} for {tick.issue_url}"


def _gate(tick: Tick, key: str, resume: str, **values) -> Result:
    # Built first: a gate key without a chat note must fail before the comment
    # and the phase change, not leave a gate that then reports as an error.
    note = _GATE_NOTES[key].format(**values)
    gated_at = time.time()
    tick.comment(key, **values)
    tick.save(phase="waiting_human", resume=resume, gated_at=gated_at)
    tick.milestone, tick.note = "gate", note
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
        is_duplicate_mission(content, entry)
        for a in _EXPECTED_AFTER[phase] for entry in _handle_entries(tick, a)
    )


def _handle_entries(tick: Tick, after: str) -> List[str]:
    """The `/handle … after:` lines a pending tick of this pipeline can read.

    One queued before a transfer still names the issue's former URL and project.
    """
    known = [(tick.issue_url, tick.project_name), *handle_state.former_keys(tick.record)]
    return [handle_state.handle_entry(project, url, after) for url, project in known]


def _recheck_pending(tick: Tick, after: str) -> bool:
    """True when this tick should leave the decision to a scheduled re-check.

    The re-check tick itself decides. A retry after an error, or a lost event
    file, finds none and re-evaluates instead of answering "busy" forever.
    """
    if tick.after == after:
        return False
    wanted = _handle_entries(tick, after)
    for path in (Path(tick.instance_dir) / "events").glob("*.json"):
        try:
            if json.loads(path.read_text()).get("mission", "").strip() in wanted:
                return True
        except (OSError, ValueError):
            continue
    return False


def _move_board(tick: Tick, column_key: str) -> None:
    if tick.is_pr:
        return  # the board tracks issues; adding a PR to it would be noise
    board = tick.config["board"]
    column = board.get(column_key) or ""
    error = gh.move_on_board(board, tick.issue.get("node_id", ""), column)
    if error:
        from app.leak_detector import scan_and_redact

        # Only the container's stderr saw these failures, so the board went
        # stale without anyone noticing.
        detail = scan_and_redact(error[:300], "/handle board comment")
        tick.comment("board_move_failed", column=column, error=detail)


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
    "delegated": ("prs",),
}

_PHASES: Dict[str, Callable[[Tick], Result]] = {
    "triage": _triage,
    "planning": _after_plan,
    "implementing": _after_implement,
    "ci": _after_ci,
    "reviewing": _after_review,
    "fixing": _await_ci,
    "qa": _qa_or_finish,
    "qa_running": _after_qa,
    "delegated": _delegated,
    "waiting_human": _resume,
}

_RESUME: Dict[str, Callable[[Tick], Result]] = {
    "triage": _triage,
    "plan": _queue_plan,
    "implement": _queue_implement,
    "ci": _await_ci,
    "data_change": _approve_data_changes,
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

    outcome = run_handle(
        args.issue_url, args.project_path, args.project_name, args.instance_dir,
        context=args.context,
    )
    print(outcome.summary)
    print(_handover(outcome), flush=True)
    sys.exit(0 if outcome.ok else 1)


def _handover(outcome: Outcome) -> str:
    """The tick's outcome as stdout markers: the agent loop sends the chat message."""
    from app import outcome_line as ol
    from app.leak_detector import redact_secrets

    text = redact_secrets(outcome.note or outcome.summary)[0]
    if not outcome.ok:
        return ol.markers(reason=text, ok=False)
    return ol.markers(url=outcome.url, detail=text, milestone=outcome.milestone)


if __name__ == "__main__":
    main()
