"""Tests for WorkspaceManager and CommandRunner.

Validates:
- CommandRunner:
  - successful command execution
  - timeout handling
  - output truncation
  - test command detection
- WorkspaceManager:
  - list_files, read_file, write_file
  - worktree operations in temporary git repo
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from shipwright.runner import CommandExecution, CommandRunner
from shipwright.workspace import WorkspaceError, WorkspaceManager


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create and initialize a temporary Git repository with an initial commit."""
    repo = tmp_path / "test_repo"
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
    initial_file.write_text("# Test Repository\nInitial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


@pytest.fixture
def temp_dir_structure(tmp_path: Path) -> Path:
    """Create a structured directory with mixed content and ignored directories."""
    base = tmp_path / "workspace"
    base.mkdir()

    # Regular files
    (base / "src").mkdir()
    (base / "src" / "main.py").write_text("print('main')\n", encoding="utf-8")
    (base / "src" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
    (base / "docs").mkdir()
    (base / "docs" / "index.md").write_text("# Documentation\n", encoding="utf-8")

    # Directories that should be ignored by list_files
    (base / ".git").mkdir()
    (base / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (base / "__pycache__").mkdir()
    (base / "__pycache__" / "main.cpython-314.pyc").write_text("bytecode", encoding="utf-8")
    (base / ".venv").mkdir()
    (base / ".venv" / "pyvenv.cfg").write_text("home = ...\n", encoding="utf-8")
    (base / "build").mkdir()
    (base / "build" / "temp.o").write_text("binary", encoding="utf-8")
    (base / "dist").mkdir()
    (base / "dist" / "package.whl").write_text("wheel", encoding="utf-8")

    return base


# ============================================================================
# CommandRunner Tests
# ============================================================================


class TestCommandRunnerExecution:
    """Tests for CommandRunner successful execution and return metrics."""

    def test_successful_command_execution(self, tmp_path: Path) -> None:
        """Verify executing a valid command produces exit_code 0 and captures stdout."""
        runner = CommandRunner()
        cmd = [sys.executable, "-c", "print('Command executed successfully')"]

        result = runner.run(cmd=cmd, cwd=tmp_path)

        assert isinstance(result, CommandExecution)
        assert result.exit_code == 0
        assert result.success is True
        assert result.failed is False
        assert result.stdout.strip() == "Command executed successfully"
        assert result.stderr == ""
        assert result.cwd == str(tmp_path)
        assert result.duration > 0
        assert result.cmd == cmd

    def test_successful_execution_with_environment_variables(self, tmp_path: Path) -> None:
        """Verify environment variables are propagated to the subprocess."""
        runner = CommandRunner()
        cmd = [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('SHIPWRIGHT_KEY', 'default'))",
        ]
        custom_env = {"SHIPWRIGHT_KEY": "special_injected_value"}

        result = runner.run(cmd=cmd, cwd=tmp_path, env=custom_env)

        assert result.exit_code == 0
        assert result.success is True
        assert result.stdout.strip() == "special_injected_value"

    def test_failed_command_execution_non_zero_exit(self, tmp_path: Path) -> None:
        """Verify non-zero exit codes are captured with stderr."""
        runner = CommandRunner()
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Fatal error\\n'); sys.exit(7)",
        ]

        result = runner.run(cmd=cmd, cwd=tmp_path)

        assert result.exit_code == 7
        assert result.success is False
        assert result.failed is True
        assert "Fatal error" in result.stderr
        assert "Fatal error" in result.combined_output

    def test_combined_output_property(self, tmp_path: Path) -> None:
        """Verify combined_output merges stdout and stderr properly."""
        runner = CommandRunner()
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('OUT\\n'); sys.stderr.write('ERR\\n')",
        ]

        result = runner.run(cmd=cmd, cwd=tmp_path)

        assert result.exit_code == 0
        assert "OUT" in result.stdout
        assert "ERR" in result.stderr
        assert "OUT" in result.combined_output
        assert "ERR" in result.combined_output


class TestCommandRunnerTimeout:
    """Tests for CommandRunner timeout handling."""

    def test_timeout_handling(self, tmp_path: Path) -> None:
        """Verify a command exceeding timeout is terminated with exit_code -1 and timeout message."""
        runner = CommandRunner()
        # Sleep command that exceeds the 0.1 second timeout
        cmd = [sys.executable, "-c", "import time; time.sleep(3.0)"]

        result = runner.run(cmd=cmd, cwd=tmp_path, timeout=0.1)

        assert result.exit_code == -1
        assert result.failed is True
        assert result.success is False
        assert result.duration >= 0.09
        assert "timed out after 0.1 seconds" in result.stderr
        assert "timed out" in result.combined_output

    def test_timeout_retains_partial_output_if_available(self, tmp_path: Path) -> None:
        """Verify timeout handling captures timeout notice in stderr."""
        runner = CommandRunner()
        cmd = [
            sys.executable,
            "-c",
            "import sys, time; sys.stderr.write('Initial err\\n'); sys.stderr.flush(); time.sleep(2.0)",
        ]

        result = runner.run(cmd=cmd, cwd=tmp_path, timeout=0.1)

        assert result.exit_code == -1
        assert "timed out after 0.1 seconds" in result.stderr


class TestCommandRunnerTruncation:
    """Tests for CommandRunner output length truncation."""

    def test_output_truncation_stdout(self, tmp_path: Path) -> None:
        """Verify stdout is truncated when exceeding max_output_chars."""
        runner = CommandRunner()
        total_chars = 1000
        limit = 100
        cmd = [sys.executable, "-c", f"print('X' * {total_chars})"]

        result = runner.run(cmd=cmd, cwd=tmp_path, max_output_chars=limit)

        assert len(result.stdout) == limit
        assert result.stdout == "X" * limit
        assert result.exit_code == 0

    def test_output_truncation_stderr(self, tmp_path: Path) -> None:
        """Verify stderr is truncated when exceeding max_output_chars."""
        runner = CommandRunner()
        limit = 50
        cmd = [sys.executable, "-c", f"import sys; sys.stderr.write('Y' * 500)"]

        result = runner.run(cmd=cmd, cwd=tmp_path, max_output_chars=limit)

        assert len(result.stderr) == limit
        assert result.stderr == "Y" * limit

    def test_output_no_truncation_when_below_limit(self, tmp_path: Path) -> None:
        """Verify output is unchanged when below max_output_chars."""
        runner = CommandRunner()
        cmd = [sys.executable, "-c", "print('short')"]

        result = runner.run(cmd=cmd, cwd=tmp_path, max_output_chars=1000)

        assert result.stdout.strip() == "short"
        assert len(result.stdout) < 1000


class TestCommandRunnerTestDetection:
    """Tests for CommandRunner.detect_test_command."""

    def test_detect_test_command_pyproject_toml(self, tmp_path: Path) -> None:
        """Detect pytest when pyproject.toml is present."""
        runner = CommandRunner()
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "pytest", "-q"]

    def test_detect_test_command_setup_py(self, tmp_path: Path) -> None:
        """Detect pytest when setup.py is present."""
        runner = CommandRunner()
        (tmp_path / "setup.py").write_text("from setuptools import setup; setup()", encoding="utf-8")

        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "pytest", "-q"]

    def test_detect_test_command_setup_cfg(self, tmp_path: Path) -> None:
        """Detect pytest when setup.cfg is present."""
        runner = CommandRunner()
        (tmp_path / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")

        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "pytest", "-q"]

    def test_detect_test_command_tests_dir(self, tmp_path: Path) -> None:
        """Detect pytest when tests/ directory is present."""
        runner = CommandRunner()
        (tmp_path / "tests").mkdir()

        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "pytest", "-q"]

    def test_detect_test_command_unittest_files(self, tmp_path: Path) -> None:
        """Detect unittest when test file contains unittest import and no pytest config exists."""
        runner = CommandRunner()
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        test_file = pkg / "test_calculator.py"
        test_file.write_text(
            "import unittest\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(1 + 1, 2)\n",
            encoding="utf-8",
        )

        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "unittest"]

    def test_detect_test_command_empty_dir_default_fallback(self, tmp_path: Path) -> None:
        """Fall back to pytest default when no specific test framework markers exist."""
        runner = CommandRunner()
        cmd = runner.detect_test_command(tmp_path)
        assert cmd == ["python", "-m", "pytest", "-q"]

    def test_detect_test_command_nonexistent_dir(self, tmp_path: Path) -> None:
        """Fall back to pytest default when repo path does not exist."""
        runner = CommandRunner()
        cmd = runner.detect_test_command(tmp_path / "does_not_exist")
        assert cmd == ["python", "-m", "pytest", "-q"]


# ============================================================================
# WorkspaceManager Tests
# ============================================================================


class TestWorkspaceManagerFileOperations:
    """Tests for WorkspaceManager file operations: list_files, read_file, write_file."""

    def test_list_files_filters_ignored_directories(self, temp_dir_structure: Path) -> None:
        """Verify list_files excludes .git, __pycache__, .venv, build, dist."""
        wm = WorkspaceManager(temp_dir_structure)
        files = wm.list_files(temp_dir_structure)

        # Expected files from regular dirs
        assert "docs/index.md" in files
        assert "src/main.py" in files
        assert "src/utils.py" in files

        # Ignored dirs must not appear
        for f in files:
            assert not f.startswith(".git")
            assert not f.startswith("__pycache__")
            assert not f.startswith(".venv")
            assert not f.startswith("build")
            assert not f.startswith("dist")

    def test_list_files_respects_max_files(self, temp_dir_structure: Path) -> None:
        """Verify max_files limit is respected."""
        wm = WorkspaceManager(temp_dir_structure)
        files = wm.list_files(temp_dir_structure, max_files=2)
        assert len(files) <= 2

    def test_list_files_non_existent_directory(self, tmp_path: Path) -> None:
        """Verify list_files returns empty list for non-existent directories."""
        wm = WorkspaceManager(tmp_path)
        assert wm.list_files(tmp_path / "nonexistent") == []

    def test_list_files_zero_or_negative_max(self, temp_dir_structure: Path) -> None:
        """Verify list_files returns empty list when max_files <= 0."""
        wm = WorkspaceManager(temp_dir_structure)
        assert wm.list_files(temp_dir_structure, max_files=0) == []
        assert wm.list_files(temp_dir_structure, max_files=-5) == []

    def test_read_and_write_file(self, tmp_path: Path) -> None:
        """Verify write_file creates parent directories and read_file retrieves exact content."""
        wm = WorkspaceManager(tmp_path)
        rel_path = "nested/sub/module.py"
        content = "def test_func():\n    return 42\n"

        wm.write_file(tmp_path, rel_path, content)

        # Check file exists on filesystem
        assert (tmp_path / "nested" / "sub" / "module.py").is_file()

        # Read back via WorkspaceManager
        read_back = wm.read_file(tmp_path, rel_path)
        assert read_back == content

    def test_write_file_overwrite(self, tmp_path: Path) -> None:
        """Verify write_file overwrites existing content."""
        wm = WorkspaceManager(tmp_path)
        rel_path = "config.json"
        wm.write_file(tmp_path, rel_path, '{"version": 1}')
        wm.write_file(tmp_path, rel_path, '{"version": 2}')

        assert wm.read_file(tmp_path, rel_path) == '{"version": 2}'

    def test_read_missing_file_raises_not_found(self, tmp_path: Path) -> None:
        """Verify read_file raises FileNotFoundError for missing files."""
        wm = WorkspaceManager(tmp_path)
        with pytest.raises(FileNotFoundError, match="File not found"):
            wm.read_file(tmp_path, "missing_file.txt")

    def test_path_traversal_prevention_read(self, tmp_path: Path) -> None:
        """Verify read_file prevents path traversal attacks outside workspace."""
        wm = WorkspaceManager(tmp_path)
        with pytest.raises(ValueError, match="Path traversal detected"):
            wm.read_file(tmp_path, "../outside.txt")

        with pytest.raises(ValueError, match="Path traversal detected"):
            wm.read_file(tmp_path, "nested/../../outside.txt")

    def test_path_traversal_prevention_write(self, tmp_path: Path) -> None:
        """Verify write_file prevents path traversal attacks outside workspace."""
        wm = WorkspaceManager(tmp_path)
        with pytest.raises(ValueError, match="Path traversal detected"):
            wm.write_file(tmp_path, "../escape.txt", "malicious payload")


class TestWorkspaceManagerGitWorktree:
    """Tests for WorkspaceManager worktree operations in temporary Git repositories."""

    def test_git_repo_detection(self, temp_git_repo: Path, tmp_path: Path) -> None:
        """Verify is_git_repo correctly identifies git and non-git directories."""
        wm_git = WorkspaceManager(temp_git_repo)
        assert wm_git.is_git_repo is True

        non_git = tmp_path / "plain_dir"
        non_git.mkdir()
        wm_nongit = WorkspaceManager(non_git)
        assert wm_nongit.is_git_repo is False

    def test_worktree_create_and_cleanup(self, temp_git_repo: Path) -> None:
        """Verify creating and cleaning up an isolated Git worktree."""
        wm = WorkspaceManager(temp_git_repo)
        run_number = 101

        # 1. Create worktree
        worktree_path_str = wm.create_worktree(run_number)
        wt_path = Path(worktree_path_str)

        assert wt_path.is_dir()
        assert (wt_path / "README.md").is_file()
        assert (wt_path / "README.md").read_text(encoding="utf-8") == "# Test Repository\nInitial content\n"

        # Verify branch was created in Git
        branch_res = subprocess.run(
            ["git", "branch", "--list", f"shipwright/run-{run_number}"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert f"shipwright/run-{run_number}" in branch_res.stdout

        # 2. Cleanup worktree with branch removal
        wm.cleanup_worktree(run_number, remove_branch=True)

        assert not wt_path.exists()
        branch_check = subprocess.run(
            ["git", "branch", "--list", f"shipwright/run-{run_number}"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert f"shipwright/run-{run_number}" not in branch_check.stdout

    def test_worktree_custom_branch(self, temp_git_repo: Path) -> None:
        """Verify creating a worktree with a custom branch name."""
        wm = WorkspaceManager(temp_git_repo)
        custom_branch = "feature/custom-isolation-branch"

        worktree_path_str = wm.create_worktree(run_number=202, branch_name=custom_branch)
        wt_path = Path(worktree_path_str)

        assert wt_path.is_dir()

        # Check git branch exists
        branch_res = subprocess.run(
            ["git", "branch", "--list", custom_branch],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert custom_branch in branch_res.stdout

        # Cleanup
        wm.cleanup_worktree(run_number=202, remove_branch=True)
        assert not wt_path.exists()

    def test_worktree_get_diff_and_commit(self, temp_git_repo: Path) -> None:
        """Verify get_diff captures changes and commit_changes clears diff."""
        wm = WorkspaceManager(temp_git_repo)
        run_number = 303
        worktree_path_str = wm.create_worktree(run_number)
        wt_path = Path(worktree_path_str)

        try:
            # Initially no diff
            initial_diff = wm.get_diff(wt_path)
            assert initial_diff.strip() == ""

            # Modify tracked file and add a new file
            wm.write_file(wt_path, "README.md", "# Test Repository\nUpdated in worktree\n")
            wm.write_file(wt_path, "new_module.py", "NEW_VAR = 99\n")

            # Check diff captures changes to tracked files
            diff = wm.get_diff(wt_path)
            assert "Updated in worktree" in diff
            assert "Initial content" in diff

            # Commit all changes (including untracked files)
            wm.commit_changes(wt_path, "Apply worktree patch")

            # After commit, diff against HEAD should be empty
            diff_after_commit = wm.get_diff(wt_path)
            assert diff_after_commit.strip() == ""

            # Verify the commit exists in worktree log and committed both files
            log_res = subprocess.run(
                ["git", "show", "--stat", "HEAD"],
                cwd=wt_path,
                capture_output=True,
                text=True,
                check=True,
            )
            assert "Apply worktree patch" in log_res.stdout
            assert "README.md" in log_res.stdout
            assert "new_module.py" in log_res.stdout

        finally:
            wm.cleanup_worktree(run_number, remove_branch=True)

    def test_worktree_fallback_for_non_git_repo(self, tmp_path: Path) -> None:
        """Verify worktree creation falls back to direct copy in non-git repos."""
        source_dir = tmp_path / "non_git_source"
        source_dir.mkdir()
        (source_dir / "app.py").write_text("print('hello fallback')", encoding="utf-8")
        (source_dir / "notes.txt").write_text("some notes", encoding="utf-8")

        wm = WorkspaceManager(source_dir)
        assert wm.is_git_repo is False

        worktree_path_str = wm.create_worktree(run_number=404)
        wt_path = Path(worktree_path_str)

        assert wt_path.is_dir()
        assert (wt_path / "app.py").is_file()
        assert wm.read_file(wt_path, "app.py") == "print('hello fallback')"

        # Modify file and verify unified fallback diff
        wm.write_file(wt_path, "app.py", "print('hello modified fallback')")
        diff = wm.get_diff(wt_path)
        assert "hello modified fallback" in diff

        # Cleanup
        wm.cleanup_worktree(run_number=404)
        assert not wt_path.exists()
