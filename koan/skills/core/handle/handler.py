"""Kōan handle skill -- queue the /handle pipeline tick for a GitHub issue or PR."""

from app.github_skill_helpers import handle_github_skill
from app.github_url_parser import parse_github_url


def handle(ctx):
    """Handle /handle -- queue a tick of the issue-to-PR pipeline.

    Usage:
        /handle https://github.com/owner/repo/issues/42
        /handle https://github.com/owner/repo/pull/43
    """
    return handle_github_skill(
        ctx,
        command="handle",
        url_type="pr-or-issue",
        parse_func=parse_github_url,
        success_prefix="Pipeline queued",
    )
