"""Tests for app.version and the /version skill."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")


@pytest.fixture(autouse=True)
def _no_deploy_env(monkeypatch, tmp_path):
    """Keep a real Railway environment and the repo's own CHANGES.md from
    leaking into the git-failure tests."""
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_BRANCH", raising=False)
    monkeypatch.setattr("app.version._CHANGES_MD", tmp_path / "missing.md", raising=False)


def _write_changes(monkeypatch, tmp_path, text):
    changes = tmp_path / "CHANGES.md"
    changes.write_text(text)
    monkeypatch.setattr("app.version._CHANGES_MD", changes, raising=False)


_CHANGES_WITH_RELEASES = """# Kōan changelog

Released versions of Kōan, newest on top.

---

## v0.9.0 — 2026-10-28

**Added**

## v0.8.1 — 2026-07-22
"""


class TestGetVersion:
    """Test app.version.get_version()."""

    def test_exact_tag(self):
        from app.version import get_version
        mock_result = MagicMock(returncode=0, stdout="v0.76\n")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_version() == "v0.76"

    def test_ahead_of_tag(self):
        from app.version import get_version
        mock_result = MagicMock(returncode=0, stdout="v0.76-178-ga456c1e8\n")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_version() == "v0.76@a456c1e8 +178"

    def test_git_failure(self):
        from app.version import get_version
        mock_result = MagicMock(returncode=128, stdout="")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_version() == ""

    def test_git_not_found(self):
        from app.version import get_version
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_version() == ""

    def test_timeout(self):
        from app.version import get_version
        with patch("app.version.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git", 5)):
            assert get_version() == ""

    def test_no_git_checkout_falls_back_to_deployed_commit(self, monkeypatch):
        # Docker images ship without .git: git describe fails, Railway
        # still tells us which commit is deployed.
        from app.version import get_version
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "fee2bbd1ebcde48c55616be754208de44e2fc63e")
        mock_result = MagicMock(returncode=128, stdout="")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_version() == "fee2bbd1"

    def test_no_git_checkout_labels_commit_with_latest_release(self, monkeypatch, tmp_path):
        # The image carries CHANGES.md; its newest release heading names the
        # release the deployed commit is built on.
        from app.version import get_version
        _write_changes(monkeypatch, tmp_path, _CHANGES_WITH_RELEASES)
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "29f036eefa708b6cd5eecd18218968ba056f5fd2")
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_version() == "v0.9.0@29f036ee"

    def test_no_git_checkout_no_deployed_commit_shows_release(self, monkeypatch, tmp_path):
        from app.version import get_version
        _write_changes(monkeypatch, tmp_path, _CHANGES_WITH_RELEASES)
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_version() == "v0.9.0"

    def test_changes_without_release_heading_keeps_commit_only(self, monkeypatch, tmp_path):
        from app.version import get_version
        _write_changes(monkeypatch, tmp_path, "# Kōan changelog\n\n## ${NEXT}\n")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "29f036eefa708b6cd5eecd18218968ba056f5fd2")
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_version() == "29f036ee"

    def test_git_tag_wins_over_deployed_commit(self, monkeypatch):
        from app.version import get_version
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "fee2bbd1ebcde48c55616be754208de44e2fc63e")
        mock_result = MagicMock(returncode=0, stdout="v0.9.0-1-gfee2bbd1\n")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_version() == "v0.9.0@fee2bbd1 +1"


class TestGetBranch:
    """Test app.version.get_branch()."""

    def test_returns_branch_name(self):
        from app.version import get_branch
        mock_result = MagicMock(returncode=0, stdout="main\n")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_branch() == "main"

    def test_feature_branch(self):
        from app.version import get_branch
        mock_result = MagicMock(returncode=0, stdout="koan.atoomic/fix-thing\n")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_branch() == "koan.atoomic/fix-thing"

    def test_git_failure(self):
        from app.version import get_branch
        mock_result = MagicMock(returncode=128, stdout="")
        with patch("app.version.subprocess.run", return_value=mock_result):
            assert get_branch() == ""

    def test_git_not_found(self):
        from app.version import get_branch
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_branch() == ""

    def test_timeout(self):
        from app.version import get_branch
        with patch("app.version.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git", 5)):
            assert get_branch() == ""

    def test_no_git_checkout_falls_back_to_deployed_branch(self, monkeypatch):
        from app.version import get_branch
        monkeypatch.setenv("RAILWAY_GIT_BRANCH", "dev")
        with patch("app.version.subprocess.run", side_effect=FileNotFoundError):
            assert get_branch() == "dev"


class TestVersionSkill:
    """Test the /version skill handler."""

    def test_returns_version_string(self):
        from skills.core.version.handler import handle
        ctx = MagicMock()
        with patch("app.version.get_version", return_value="v0.76@a456c1e8 +178"):
            assert handle(ctx) == "v0.76@a456c1e8 +178"

    def test_returns_exact_tag(self):
        from skills.core.version.handler import handle
        ctx = MagicMock()
        with patch("app.version.get_version", return_value="v0.76"):
            assert handle(ctx) == "v0.76"

    def test_returns_unknown_on_failure(self):
        from skills.core.version.handler import handle
        ctx = MagicMock()
        with patch("app.version.get_version", return_value=""):
            assert handle(ctx) == "unknown"
