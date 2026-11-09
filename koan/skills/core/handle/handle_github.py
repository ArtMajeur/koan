"""GitHub reads and writes for the /handle pipeline.

Required reads raise ``RuntimeError`` so the tick can gate on them; the
best-effort writes (assignment, board, team lookup) log and return False.
"""

import json
import sys
from datetime import datetime
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

from app.github import api, run_gh

_PROJECT_QUERY = (
    "query($org:String!,$num:Int!,$field:String!){organization(login:$org){"
    "projectV2(number:$num){id field(name:$field){"
    "... on ProjectV2SingleSelectField{id options{id name}}}}}}"
)
_ADD_ITEM = (
    "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c})"
    "{item{id}}}"
)
_SET_STATUS = (
    "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
    "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}})"
    "{projectV2Item{id}}}"
)


def _log(msg: str) -> None:
    print(f"[handle] {msg}", file=sys.stderr)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _json(raw: str):
    return json.loads(raw) if raw and raw.strip() else None


# -- issue thread ---------------------------------------------------------


def fetch_issue(owner: str, repo: str, number: str) -> dict:
    """Return ``{title, body, state, author, node_id, html_url, is_pr}`` for an issue."""
    data = _json(api(
        f"repos/{owner}/{repo}/issues/{number}",
        jq="{title, body, state, author: .user.login, node_id, html_url,"
           " is_pr: (.pull_request != null)}",
    ))
    if not data:
        raise RuntimeError(f"empty response for {owner}/{repo}#{number}")
    return data


def fetch_comments(owner: str, repo: str, number: str) -> List[dict]:
    """Every comment as ``{author, is_bot, body, created_at}`` (epoch), oldest first."""
    raw = run_gh(
        "api", "--paginate", f"repos/{owner}/{repo}/issues/{number}/comments",
        "--jq", '.[] | {author: .user.login, is_bot: (.user.type == "Bot"), body, created_at}',
    )
    comments = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for comment in comments:
        comment["created_at"] = _epoch(comment["created_at"])
    return comments


def _canonical_login(login: str) -> str:
    """gh writes a GitHub App's login ``app/<slug>``; REST writes ``<slug>[bot]``.

    Only that spelling difference is folded. A user ``x`` and an App ``x[bot]``
    are different accounts and must never compare equal.
    """
    login = (login or "").strip().lower()
    return f"{login[len('app/'):]}[bot]" if login.startswith("app/") else login


def same_login(a: str, b: str) -> bool:
    """Case-insensitive login equality across gh's and REST's App spellings.

    When Kōan runs as a GitHub App, ``github.nickname`` must hold the App's
    real login (``<slug>[bot]``), not the bare slug.
    """
    canonical = _canonical_login(a)
    return bool(canonical) and canonical == _canonical_login(b)


def latest_handle_mention(comments: List[dict], bot: str, since: float = 0) -> str:
    """Author of the latest ``@<bot> handle`` comment newer than *since* ("" if none).

    Parsed like every @mention command, so the instruction quoted from a gate
    comment (`` `@<bot> handle` `` in a code span) is not a request.
    """
    from app.github_notifications import parse_mention_command

    for comment in reversed(comments):
        if comment.get("created_at", 0) <= since:
            break
        author = comment.get("author") or ""
        command = parse_mention_command(comment.get("body") or "", bot)
        if command and command[0] == "handle" and not same_login(author, bot):
            return author
    return ""


def is_authorized(owner: str, repo: str, login: str, project_name: str) -> bool:
    """Same rule as GitHub @mention commands: allowlist, or write access for "*"."""
    import os

    from app.github_config import get_github_authorized_users
    from app.github_notifications import check_user_permission
    from app.projects_config import load_projects_config
    from app.utils import load_config

    if not login:
        return False
    projects = load_projects_config(os.environ.get("KOAN_ROOT", ""))
    allowed = get_github_authorized_users(load_config(), project_name, projects)
    return check_user_permission(owner, repo, login, allowed)


def latest_bot_comment(
    comments: List[dict], bot: str, since: float,
    match: Callable[[str], bool] = lambda body: True,
) -> Optional[dict]:
    """Newest comment by *bot* at or after *since* whose body satisfies *match*."""
    for comment in reversed(comments):
        if (same_login(comment.get("author") or "", bot)
                and comment["created_at"] >= since
                and match(comment.get("body") or "")):
            return comment
    return None


def post_comment(owner: str, repo: str, number: str, body: str) -> None:
    from app.github_reply import post_reply

    if not post_reply(owner, repo, number, body):
        raise RuntimeError(f"could not comment on {owner}/{repo}#{number}")


def assign_self(owner: str, repo: str, number: str) -> bool:
    try:
        run_gh("issue", "edit", number, "--repo", f"{owner}/{repo}", "--add-assignee", "@me")
        return True
    except RuntimeError as e:
        _log(f"self-assignment failed on {owner}/{repo}#{number}: {e}")
        return False


# -- pull request ---------------------------------------------------------


def find_open_pr(owner: str, repo: str, branch: str, bot: str) -> Optional[dict]:
    """The bot's own open PR on *branch*: ``{url, number, headRefOid, updated_at}``.

    ``--head`` matches the branch *name* only, so a fork could open a PR from a
    same-named branch; only a PR the bot authored from the base repository
    is adopted (it is then reviewed and fixed autonomously).
    """
    prs = _json(run_gh(
        "pr", "list", "--repo", f"{owner}/{repo}", "--head", branch, "--state", "open",
        "--json", "url,number,headRefOid,updatedAt,author,headRepositoryOwner",
    )) or []
    for pr in prs:
        author = (pr.get("author") or {}).get("login") or ""
        head_owner = ((pr.get("headRepositoryOwner") or {}).get("login") or "").lower()
        if same_login(author, bot) and head_owner == owner.lower():
            return {
                "url": pr["url"], "number": pr["number"], "headRefOid": pr["headRefOid"],
                "updated_at": _epoch(pr["updatedAt"]),
            }
    return None


def pr_info(owner: str, repo: str, number: str) -> dict:
    """``{state, headRefOid, isDraft, url}`` of a PR (state: OPEN/CLOSED/MERGED)."""
    data = _json(run_gh(
        "pr", "view", number, "--repo", f"{owner}/{repo}",
        "--json", "state,headRefOid,isDraft,url",
    ))
    if not data:
        raise RuntimeError(f"empty response for PR {owner}/{repo}#{number}")
    return data


def mark_ready(owner: str, repo: str, number: str) -> None:
    """Take the PR out of draft; raises, because CI may depend on it."""
    run_gh("pr", "ready", number, "--repo", f"{owner}/{repo}")


def hand_off_pr(owner: str, repo: str, number: str, reviewer: str, is_draft: bool) -> None:
    """Take the PR out of draft and ask *reviewer* for a review (best-effort)."""
    if is_draft:
        try:
            mark_ready(owner, repo, number)
        except RuntimeError as e:
            _log(f"marking PR {number} ready failed: {e}")
    if reviewer:
        try:
            run_gh("pr", "edit", number, "--repo", f"{owner}/{repo}", "--add-reviewer", reviewer)
        except RuntimeError as e:
            _log(f"review request to {reviewer} failed: {e}")


def merge_commit_sha(owner: str, repo: str, number: str) -> str:
    """The PR's test-merge commit, which ``pull_request`` workflows deploy."""
    return api(f"repos/{owner}/{repo}/pulls/{number}", jq=".merge_commit_sha // empty").strip()


def preview_status(
    owner: str, repo: str, environment: str, accepted_shas: Tuple[str, ...],
) -> Tuple[str, str]:
    """Return ``(status, url)`` of the PR's preview deployment.

    status is ``ready`` (latest deployment is for one of *accepted_shas* —
    the PR head or its merge commit — and succeeded with an environment URL),
    ``failed``, or ``pending`` (absent, outdated, still deploying, or no URL).
    """
    deployments = _json(api(
        f"repos/{owner}/{repo}/deployments?environment={quote(environment, safe='')}&per_page=1",
    )) or []
    if not deployments or deployments[0].get("sha") not in accepted_shas:
        return "pending", ""
    statuses = _json(api(
        f"repos/{owner}/{repo}/deployments/{deployments[0]['id']}/statuses?per_page=1",
    )) or []
    if not statuses:
        return "pending", ""
    state = statuses[0].get("state")
    url = statuses[0].get("environment_url") or ""
    if state == "success" and url:
        return "ready", url
    if state in ("failure", "error", "inactive"):
        return "failed", ""
    if state == "success":
        # QA takes the URL as its first argument: without one there is no
        # preview to test, and the wait ends on its own bound.
        _log(f"preview deployment for {environment} succeeded without an environment_url")
    return "pending", ""


# -- organization: team and board -----------------------------------------


def is_team_member(org: str, team: str, login: str) -> bool:
    if not (org and team and login):
        return False
    try:
        state = api(f"orgs/{org}/teams/{team}/memberships/{login}", jq=".state")
    except RuntimeError as e:
        _log(f"team lookup {org}/{team} for {login}: {e}")
        return False
    return state.strip() == "active"


def move_on_board(board: dict, issue_node_id: str, column: str) -> bool:
    """Set the board's single-select status of the issue to *column*."""
    owner, number = board.get("owner"), board.get("number")
    if not (owner and number and column and issue_node_id):
        return False
    try:
        project = _json(run_gh(
            "api", "graphql", "-f", f"query={_PROJECT_QUERY}", "-f", f"org={owner}",
            "-F", f"num={int(number)}", "-f", f"field={board.get('status_field', 'Status')}",
        ))["data"]["organization"]["projectV2"]
        option = next(o["id"] for o in project["field"]["options"] if o["name"] == column)
        item = _json(run_gh(
            "api", "graphql", "-f", f"query={_ADD_ITEM}",
            "-f", f"p={project['id']}", "-f", f"c={issue_node_id}",
        ))["data"]["addProjectV2ItemById"]["item"]["id"]
        run_gh(
            "api", "graphql", "-f", f"query={_SET_STATUS}", "-f", f"p={project['id']}",
            "-f", f"i={item}", "-f", f"f={project['field']['id']}", "-f", f"o={option}",
        )
        return True
    except (RuntimeError, KeyError, TypeError, ValueError, StopIteration) as e:
        _log(f"board move to {column!r} failed: {e}")
        return False
