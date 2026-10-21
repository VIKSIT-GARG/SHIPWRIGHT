"""Tests for shipwright.workspace."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import pytest

from shipwright.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Shipwright Tester"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@shipwright.local"],
        cwd=repo,
        check=True,
    )
    initial_file = repo / "README.md"
    initial_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


@pytest.fixture
def non_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "nongit"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pip.py").write_text("# ignored", encoding="utf-8")
    return repo


def test_init_defaults(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    assert wm.repo_root == tmp_path.resolve()
    assert wm.worktrees_dir == (tmp_path / ".worktrees").resolve()
    assert not wm.is_git_repo


def test_init_custom_worktrees_dir(tmp_path: Path) -> None:
    custom_wt = tmp_path / "custom_worktrees"
    wm = WorkspaceManager(tmp_path, worktrees_dir=custom_wt)
    assert wm.worktrees_dir == custom_wt.resolve()


def test_create_and_cleanup_worktree_git(git_repo: Path) -> None:
    wm = WorkspaceManager(git_repo)
    assert wm.is_git_repo

    # Create worktree
    wt_path_str = wm.create_worktree(1)
    wt_path = Path(wt_path_str)
    assert wt_path.is_dir()
    assert (wt_path / "README.md").exists()
    assert (wt_path / "README.md").read_text(encoding="utf-8") == "# Test Repo\n"

    # Verify branch was created
    branch_check = subprocess.run(
        ["git", "branch", "--list", "shipwright/run-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "shipwright/run-1" in branch_check.stdout

    # Cleanup worktree with branch removal
    wm.cleanup_worktree(1, remove_branch=True)
    assert not wt_path.exists()

    branch_check2 = subprocess.run(
        ["git", "branch", "--list", "shipwright/run-1"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "shipwright/run-1" not in branch_check2.stdout


def test_create_worktree_custom_branch(git_repo: Path) -> None:
    wm = WorkspaceManager(git_repo)
    wt_path_str = wm.create_worktree(2, branch_name="feature/test-branch")
    wt_path = Path(wt_path_str)
    assert wt_path.is_dir()

    branch_check = subprocess.run(
        ["git", "branch", "--list", "feature/test-branch"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "feature/test-branch" in branch_check.stdout

    wm.cleanup_worktree(2, remove_branch=True)
    assert not wt_path.exists()


def test_read_write_list_files(git_repo: Path) -> None:
    wm = WorkspaceManager(git_repo)
    wt_path = wm.create_worktree(3)

    # Write new file
    wm.write_file(wt_path, "nested/module/test.py", "def test_ok(): pass\n")
    assert wm.read_file(wt_path, "nested/module/test.py") == "def test_ok(): pass\n"

    # List files
    files = wm.list_files(wt_path)
    assert "README.md" in files
    assert "nested/module/test.py" in files
    assert ".git" not in files

    # Test max_files
    limited = wm.list_files(wt_path, max_files=1)
    assert len(limited) == 1

    wm.cleanup_worktree(3, remove_branch=True)


def test_get_diff_and_commit_changes(git_repo: Path) -> None:
    wm = WorkspaceManager(git_repo)
    wt_path = wm.create_worktree(4)

    # Initially diff should be empty
    diff_initial = wm.get_diff(wt_path)
    assert diff_initial == ""

    # Modify existing file
    wm.write_file(wt_path, "README.md", "# Modified\n")
    diff_modified = wm.get_diff(wt_path)
    assert "-# Test Repo" in diff_modified
    assert "+# Modified" in diff_modified

    # Commit changes
    wm.commit_changes(wt_path, "Update README")

    # After commit, diff against HEAD should be empty
    assert wm.get_diff(wt_path) == ""

    # Committing clean worktree should not raise
    wm.commit_changes(wt_path, "No changes commit")

    wm.cleanup_worktree(4, remove_branch=True)


def test_fallback_non_git_repo(non_git_repo: Path) -> None:
    wm = WorkspaceManager(non_git_repo)
    assert not wm.is_git_repo

    wt_path_str = wm.create_worktree(10)
    wt_path = Path(wt_path_str)
    assert wt_path.is_dir()
    assert (wt_path / "src" / "app.py").is_file()
    assert not (wt_path / ".venv").exists()

    # Modify file in fallback worktree
    wm.write_file(wt_path, "src/app.py", "print('modified')\n")
    diff = wm.get_diff(wt_path)
    assert "-print('hello')" in diff
    assert "+print('modified')" in diff

    # Commit in non-git should be a graceful no-op
    wm.commit_changes(wt_path, "dummy message")

    wm.cleanup_worktree(10)
    assert not wt_path.exists()


def test_mock_mode_flag(git_repo: Path) -> None:
    wm = WorkspaceManager(git_repo, mock_mode=True)
    assert not wm.is_git_repo

    wt_path = Path(wm.create_worktree(20))
    assert wt_path.is_dir()
    assert (wt_path / "README.md").is_file()
    # In mock mode, .git is not created in worktree
    assert not (wt_path / ".git").exists()

    wm.cleanup_worktree(20)
    assert not wt_path.exists()


def test_security_path_traversal(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()

    with pytest.raises(ValueError, match="Path traversal detected"):
        wm.read_file(wt, "../outside.txt")

    with pytest.raises(ValueError, match="Path traversal detected"):
        wm.write_file(wt, "../../outside.txt", "danger")

    with pytest.raises(FileNotFoundError):
        wm.read_file(wt, "missing.txt")
