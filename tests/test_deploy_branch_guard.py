"""Tests for the deploy branch guards in tools/deploy.py (Issue #1230; 0.8.x maintenance line 2026-11-08).

A deploy from a long-lived development branch must already contain every commit on ``origin/main``. No branch is
long-lived since 0.9.x was promoted to main, so these tests configure ``0.9.x`` as one to keep the mechanism pinned.
A deploy from the ``0.8.x`` maintenance line is refused (it would roll the house back) unless overridden; ``main``
and other branches pass. The tests run real git in a throwaway repository so the guard's git commands are exercised.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import deploy  # noqa: E402


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", name)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone whose origin has main; the clone has 0.9.x branched from it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q", "-b", "main")
    _commit(origin, "base")
    clone = tmp_path / "clone"
    _run(tmp_path, "clone", "-q", str(origin), str(clone))
    _run(clone, "checkout", "-q", "-b", "0.9.x")
    monkeypatch.setattr(deploy, "REPO_ROOT", clone)
    monkeypatch.setattr(deploy, "LONG_LIVED_BRANCHES", ("0.9.x",))
    return clone


def test_branch_with_main_merged_passes(repo: Path) -> None:
    _commit(repo, "feature")
    assert deploy.current_branch() == "0.9.x"
    ok, message = deploy.check_main_merged("0.9.x")
    assert ok
    assert "merged" in message


def test_branch_missing_main_commits_is_refused(repo: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "fix-on-main")
    ok, message = deploy.check_main_merged("0.9.x")
    assert not ok
    assert "missing 1 commit" in message
    assert "--allow-stale-main" in message


def test_merging_main_clears_the_refusal(repo: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "fix-on-main")
    assert not deploy.check_main_merged("0.9.x")[0]
    _run(repo, "merge", "-q", "--no-edit", "origin/main")
    assert deploy.check_main_merged("0.9.x")[0]


def test_main_and_other_branches_are_never_checked(repo: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "fix-on-main")
    for branch in ("main", "fix/1-something", ""):
        assert deploy.check_main_merged(branch) == (True, "")


def test_fetch_failure_warns_and_uses_last_fetched_main(repo: Path) -> None:
    _run(repo, "remote", "set-url", "origin", str(repo.parent / "does-not-exist"))
    ok, message = deploy.check_main_merged("0.9.x")
    assert ok
    assert "could not fetch origin/main" in message


def test_detached_head_reports_no_branch(repo: Path) -> None:
    _run(repo, "checkout", "-q", "--detach")
    assert deploy.current_branch() == ""


def test_local_version_reads_const() -> None:
    assert deploy.local_version().count(".") == 2


def test_no_branch_is_long_lived_since_0_9_was_promoted_to_main() -> None:
    assert deploy.LONG_LIVED_BRANCHES == ()
    assert deploy.check_main_merged("0.9.x") == (True, "")


def test_the_0_8_maintenance_line_is_refused_and_main_passes() -> None:
    ok, message = deploy.check_maintenance_branch("0.8.x")
    assert not ok
    assert "roll the house back" in message
    assert "--allow-maintenance-branch" in message
    for branch in ("main", "fix/1-something", ""):
        assert deploy.check_maintenance_branch(branch) == (True, "")
