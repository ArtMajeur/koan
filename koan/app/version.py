"""Kōan version string from git tags."""

import os
import re
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired

_KOAN_SRC = Path(__file__).resolve().parents[1]

# Docker images ship without .git (see .dockerignore), so the git calls below
# fail there. Railway injects the deployed commit and branch into the runtime
# environment instead.
_DEPLOY_SHA_ENV = "RAILWAY_GIT_COMMIT_SHA"
_DEPLOY_BRANCH_ENV = "RAILWAY_GIT_BRANCH"

# Without tags, the newest release heading in CHANGES.md (copied into the image)
# names the release the tree is built on. Only accurate when the deployed branch
# sits on a release tag: plain upstream main can lag behind its latest release.
_CHANGES_MD = _KOAN_SRC.parent / "CHANGES.md"
_RELEASE_HEADING = re.compile(r"^## (v\d\S*)", re.MULTILINE)


def _git(*args: str) -> str:
    """Run git in the Kōan source tree; return stripped stdout, or "" on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=5,
            cwd=_KOAN_SRC,
        )
    except (OSError, TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_version() -> str:
    """Return Kōan version from git tags.

    Format: 'v0.73' (exact tag) or 'v0.73@deadbeef +17' (ahead of tag).
    Without a git checkout: 'v0.73@deadbeef' from CHANGES.md and the deployed
    commit (no commit count — it can't be known without git).
    """
    desc = _git("describe", "--tags")
    if not desc:
        return _deployed_version()
    parts = desc.rsplit("-", 2)
    if len(parts) == 3 and parts[2].startswith("g"):
        tag, commits_ahead, sha = parts[0], parts[1], parts[2][1:]
        return f"{tag}@{sha[:8]} +{commits_ahead}"
    return desc


def _deployed_version() -> str:
    """Version of a tree without .git: latest release and/or deployed commit."""
    release = _latest_release()
    sha = os.environ.get(_DEPLOY_SHA_ENV, "").strip()[:8]
    if release and sha:
        return f"{release}@{sha}"
    return release or sha


def _latest_release() -> str:
    """Newest released version in CHANGES.md ('v0.9.0'), or ""."""
    try:
        text = _CHANGES_MD.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _RELEASE_HEADING.search(text)
    return match.group(1) if match else ""


def get_branch() -> str:
    """Return current git branch name for the Kōan source tree."""
    return _git("rev-parse", "--abbrev-ref", "HEAD") or os.environ.get(
        _DEPLOY_BRANCH_ENV, ""
    ).strip()
