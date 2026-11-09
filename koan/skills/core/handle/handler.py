"""Kōan handle skill -- queue the /handle pipeline tick for a GitHub issue."""

from app.github_url_parser import parse_github_url
from app.github_skill_helpers import handle_github_skill


def handle(ctx):
    """Handle /handle -- queue a tick of the issue-to-PR pipeline.

    Usage:
        /handle https://github.com/owner/repo/issues/42
    """
    return handle_github_skill(
        ctx,
        command="handle",
        url_type="pr-or-issue",
        parse_func=parse_github_url,
        success_prefix="Pipeline queued",
    )
