"""Workspace management for Shipwright runs using Git worktrees.

Provides isolated worktree creation, file operations, diff inspection,
and cleanup for autonomous agent operations.
"""

from __future__ import annotations

import difflib
import logging
import os
from pathlib import Path
import shutil
import subprocess

logger = logging.getLogger(__name__)

IGNORED_DIRS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    ".venv",
    "build",
    "dist",
    ".worktrees",
    ".shipwright",
    ".pytest_cache",
})


class WorkspaceError(RuntimeError):
    """Raised when a workspace operation fails."""


class WorkspaceManager:
    """Manages isolated Git worktrees and directory fallbacks for Shipwright runs."""

    def __init__(
        self,
        repo_root: str | Path,
        worktrees_dir: str | Path | None = None,
        *,
        mock_mode: bool = False,
    ) -> None:
        """Initialize the workspace manager.

        Args:
            repo_root: Path to the target repository root.
            worktrees_dir: Optional path to directory where worktrees will be created.
                Defaults to `<repo_root>/.worktrees`.
            mock_mode: When True, bypasses git invocations and uses direct directory copying.
        """
        self.repo_root = Path(repo_root).resolve()
        if worktrees_dir is not None:
            self.worktrees_dir = Path(worktrees_dir).resolve()
        else:
            self.worktrees_dir = (self.repo_root / ".worktrees").resolve()

        self.mock_mode = (
            mock_mode
            or os.environ.get("SHIPWRIGHT_MOCK_MODE", "").lower() in ("1", "true", "yes")
            or os.environ.get("SHIPWRIGHT_TEST_MODE", "").lower() in ("1", "true", "yes")
        )
        self._worktree_branches: dict[int, str] = {}

    @property
    def is_git_repo(self) -> bool:
        """Check whether the target repo_root is a valid Git worktree/repository."""
        if self.mock_mode:
            return False
        return self._check_is_git_repo()

    def _check_is_git_repo(self) -> bool:
        if not self.repo_root.is_dir():
            return False
        if not (self.repo_root / ".git").exists():
            return False
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return False

    def _worktree_path_for_run(self, run_number: int) -> Path:
        """Return the resolved worktree path for a given run number."""
        return (self.worktrees_dir / f"run-{run_number}").resolve()

    def _safe_resolve(self, worktree_path: str | Path, rel_path: str) -> Path:
        """Resolve a relative path safely within a worktree path.

        Raises:
            ValueError: If rel_path escapes the worktree directory.
        """
        base = Path(worktree_path).resolve()
        clean = rel_path.lstrip("/\\")
        target = (base / clean).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"Path traversal detected: {rel_path!r} escapes worktree {base}")
        return target

    def create_worktree(self, run_number: int, branch_name: str | None = None) -> str:
        """Create an isolated worktree for a specific run.

        Runs `git worktree add -b <branch_name or shipwright/run-{run_number}> <worktree_path>`.
        If the repo is not a git repo or in mock mode / test mode, handles fallback
        gracefully by copying the repository directory.

        Args:
            run_number: Integer run identifier.
            branch_name: Optional custom branch name. Defaults to `shipwright/run-{run_number}`.

        Returns:
            Absolute path string to the worktree directory.
        """
        branch = branch_name or f"shipwright/run-{run_number}"
        worktree_path = self._worktree_path_for_run(run_number)
        self._worktree_branches[run_number] = branch

        if not self.is_git_repo or self.mock_mode:
            self._copy_repo_fallback(worktree_path)
            return str(worktree_path)

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        if worktree_path.exists():
            self.cleanup_worktree(run_number, remove_branch=False)

        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path)]
        res = subprocess.run(
            cmd,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        if res.returncode != 0:
            if "already exists" in res.stderr.lower():
                retry_cmd = ["git", "worktree", "add", "-B", branch, str(worktree_path)]
                res = subprocess.run(
                    retry_cmd,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )

        if res.returncode != 0:
            logger.warning(
                "git worktree add failed (%s: %s); falling back to direct copy.",
                res.returncode,
                res.stderr.strip(),
            )
            self._copy_repo_fallback(worktree_path)

        return str(worktree_path)

    def _copy_repo_fallback(self, worktree_path: Path) -> None:
        """Fallback implementation: copy repo_root to worktree_path."""
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        worktree_path.mkdir(parents=True, exist_ok=True)

        if not self.repo_root.is_dir():
            return

        def ignore_filter(directory: str, names: list[str]) -> set[str]:
            ignored = set(IGNORED_DIRS)
            try:
                if Path(directory).resolve() == self.repo_root:
                    ignored.add(self.worktrees_dir.name)
            except Exception:
                pass
            return {n for n in names if n in ignored or n.endswith(".pyc")}

        for item in self.repo_root.iterdir():
            if item.name in IGNORED_DIRS or item.name == self.worktrees_dir.name:
                continue
            dest = worktree_path / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, dest, ignore=ignore_filter, symlinks=True)
                else:
                    shutil.copy2(item, dest)
            except OSError as err:
                logger.debug("Failed copying %s to %s: %s", item, dest, err)

    def cleanup_worktree(self, run_number: int, remove_branch: bool = False) -> None:
        """Clean up an isolated worktree for a specific run.

        Runs `git worktree remove --force <worktree_path>` and `git worktree prune`.
        Optionally removes the created branch.

        Args:
            run_number: Integer run identifier.
            remove_branch: Whether to delete the Git branch associated with this run.
        """
        worktree_path = self._worktree_path_for_run(run_number)
        branch = self._worktree_branches.pop(run_number, f"shipwright/run-{run_number}")

        if self.is_git_repo and not self.mock_mode:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if remove_branch:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )

        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    def get_diff(self, worktree_path: str | Path) -> str:
        """Get the diff for the given worktree.

        Runs `git diff HEAD` inside the worktree. Falls back to unified diff in mock mode
        or non-git repositories.

        Args:
            worktree_path: Path to the worktree directory.

        Returns:
            The diff output as a string.
        """
        path = Path(worktree_path).resolve()
        if self.is_git_repo and not self.mock_mode:
            try:
                res = subprocess.run(
                    ["git", "diff", "HEAD"],
                    cwd=path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    return res.stdout
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass

        return self._generate_fallback_diff(path)

    def _generate_fallback_diff(self, worktree_path: Path) -> str:
        """Generate a unified diff between repo_root and worktree_path."""
        if not worktree_path.is_dir() or not self.repo_root.is_dir():
            return ""

        diff_chunks: list[str] = []
        files = set(self.list_files(worktree_path, max_files=500))
        if self.repo_root.is_dir():
            files.update(self.list_files(self.repo_root, max_files=500))

        for rel in sorted(files):
            src_file = self.repo_root / rel
            dst_file = worktree_path / rel

            src_lines = (
                src_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                if src_file.is_file()
                else []
            )
            dst_lines = (
                dst_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                if dst_file.is_file()
                else []
            )

            if src_lines != dst_lines:
                chunk = difflib.unified_diff(
                    src_lines,
                    dst_lines,
                    fromfile=f"a/{rel}" if src_file.is_file() else "/dev/null",
                    tofile=f"b/{rel}" if dst_file.is_file() else "/dev/null",
                )
                diff_chunks.extend(chunk)

        return "".join(diff_chunks)

    def commit_changes(self, worktree_path: str | Path, message: str) -> None:
        """Commit all changes inside the worktree.

        Runs `git add -A` and `git commit -m <message>`.

        Args:
            worktree_path: Path to the worktree directory.
            message: Commit message.

        Raises:
            WorkspaceError: If committing fails unexpectedly.
        """
        path = Path(worktree_path).resolve()
        if not self.is_git_repo or self.mock_mode:
            return

        add_res = subprocess.run(
            ["git", "add", "-A"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_res.returncode != 0:
            raise WorkspaceError(f"Failed to stage changes: {add_res.stderr.strip()}")

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Shipwright"),
            "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "shipwright@local"),
            "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "Shipwright"),
            "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "shipwright@local"),
        }

        commit_res = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=path,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        if commit_res.returncode != 0:
            out = (commit_res.stdout + "\n" + commit_res.stderr).lower()
            if "nothing to commit" in out or "no changes added to commit" in out:
                return
            raise WorkspaceError(f"Failed to commit changes: {commit_res.stderr.strip()}")

    def list_files(self, worktree_path: str | Path, max_files: int = 100) -> list[str]:
        """List relative file paths in the worktree.

        Ignores directories matching .git, __pycache__, .venv, build, dist.

        Args:
            worktree_path: Path to the worktree directory.
            max_files: Maximum number of files to return. Defaults to 100.

        Returns:
            List of POSIX relative file paths.
        """
        if max_files <= 0:
            return []

        path = Path(worktree_path).resolve()
        if not path.is_dir():
            return []

        results: list[str] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted([d for d in dirs if d not in IGNORED_DIRS])
            for f in sorted(files):
                if f in IGNORED_DIRS:
                    continue
                full_path = Path(root, f)
                rel = full_path.relative_to(path).as_posix()
                results.append(rel)
                if len(results) >= max_files:
                    return results

        return results

    def read_file(self, worktree_path: str | Path, rel_path: str) -> str:
        """Read the content of a file within the worktree.

        Args:
            worktree_path: Path to the worktree directory.
            rel_path: Relative path to the file.

        Returns:
            Contents of the file decoded as UTF-8.

        Raises:
            ValueError: If rel_path escapes the worktree directory.
            FileNotFoundError: If the file does not exist.
        """
        target = self._safe_resolve(worktree_path, rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {rel_path} in {worktree_path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, worktree_path: str | Path, rel_path: str, content: str) -> None:
        """Write content to a file within the worktree, creating parents if necessary.

        Args:
            worktree_path: Path to the worktree directory.
            rel_path: Relative path to the file.
            content: String content to write.

        Raises:
            ValueError: If rel_path escapes the worktree directory.
        """
        target = self._safe_resolve(worktree_path, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


__all__ = ["WorkspaceError", "WorkspaceManager"]
