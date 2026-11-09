"""GitHub reads and writes for the /handle pipeline.

Required reads raise ``RuntimeError`` so the tick can gate on them; the
best-effort writes (assignment, board, team lookup) log and return False.
"""

import json
import re
import sys
from datetime import datetime
from typing import List, Optional, Tuple
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
    """Return every comment as ``{author, body, created_at}`` (epoch), oldest first."""
    raw = run_gh(
        "api", "--paginate", f"repos/{owner}/{repo}/issues/{number}/comments",
        "--jq", ".[] | {author: .user.login, body, created_at}",
    )
    comments = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for comment in comments:
        comment["created_at"] = _epoch(comment["created_at"])
    return comments


def find_requester(comments: List[dict], bot: str, fallback: str) -> str:
    """Author of the latest ``@<bot> handle`` comment that is not the bot."""
    if bot:
        mention = re.compile(rf"@{re.escape(bot)}\s+/?handle\b", re.IGNORECASE)
        for comment in reversed(comments):
            author = comment.get("author") or ""
            if author.lower() != bot.lower() and mention.search(comment.get("body") or ""):
                return author
    return fallback


def latest_bot_comment(comments: List[dict], bot: str, since: float) -> Optional[dict]:
    """Newest comment by *bot* created at or after *since*."""
    for comment in reversed(comments):
        if (comment.get("author") or "").lower() == bot.lower() and comment["created_at"] >= since:
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


def find_open_pr(owner: str, repo: str, branch: str) -> Optional[dict]:
    """Open PR whose head is *branch*: ``{url, number, headRefOid}``."""
    prs = _json(run_gh(
        "pr", "list", "--repo", f"{owner}/{repo}", "--head", branch,
        "--state", "open", "--json", "url,number,headRefOid",
    )) or []
    return prs[0] if prs else None


def pr_info(owner: str, repo: str, number: str) -> dict:
    """``{state, headRefOid, isDraft, url}`` of a PR (state: OPEN/CLOSED/MERGED)."""
    data = _json(run_gh(
        "pr", "view", number, "--repo", f"{owner}/{repo}",
        "--json", "state,headRefOid,isDraft,url",
    ))
    if not data:
        raise RuntimeError(f"empty response for PR {owner}/{repo}#{number}")
    return data


def hand_off_pr(owner: str, repo: str, number: str, reviewer: str) -> None:
    """Take the PR out of draft and ask *reviewer* for a review (best-effort)."""
    try:
        run_gh("pr", "ready", number, "--repo", f"{owner}/{repo}")
    except RuntimeError as e:
        _log(f"marking PR {number} ready failed: {e}")
    if reviewer:
        try:
            run_gh("pr", "edit", number, "--repo", f"{owner}/{repo}", "--add-reviewer", reviewer)
        except RuntimeError as e:
            _log(f"review request to {reviewer} failed: {e}")


def preview_status(
    owner: str, repo: str, environment: str, head_sha: str,
) -> Tuple[str, str]:
    """Return ``(status, url)`` of the PR's preview deployment.

    status is ``ready`` (latest deployment is for *head_sha* and succeeded),
    ``failed``, or ``pending`` (absent, outdated, or still deploying).
    """
    deployments = _json(api(
        f"repos/{owner}/{repo}/deployments?environment={quote(environment, safe='')}&per_page=1",
    )) or []
    if not deployments or deployments[0].get("sha") != head_sha:
        return "pending", ""
    statuses = _json(api(
        f"repos/{owner}/{repo}/deployments/{deployments[0]['id']}/statuses?per_page=1",
    )) or []
    if not statuses:
        return "pending", ""
    state = statuses[0].get("state")
    if state == "success":
        return "ready", statuses[0].get("environment_url") or ""
    if state in ("failure", "error", "inactive"):
        return "failed", ""
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
