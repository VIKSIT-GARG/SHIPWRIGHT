"""Safe command runner and repository test framework detection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from dataclasses import dataclass

__all__ = [
    "CommandExecution",
    "CommandRunner",
    "detect_test_command",
]

_EXCLUDED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        ".env",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
    }
)


def _truncate_output(text: str, max_chars: int) -> str:
    """Truncate text to max_chars characters if exceeded."""
    if max_chars >= 0 and len(text) > max_chars:
        return text[:max_chars]
    return text


@dataclass(slots=True)
class CommandExecution:
    """Captured result and performance metrics of an executed command."""

    cmd: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float

    @property
    def success(self) -> bool:
        """Return True if command finished with exit code 0."""
        return self.exit_code == 0

    @property
    def failed(self) -> bool:
        """Return True if command finished with non-zero exit code."""
        return self.exit_code != 0

    @property
    def combined_output(self) -> str:
        """Return combined stdout and stderr streams."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


class CommandRunner:
    """Safely executes shell commands with timeouts, limits, and test detection."""

    def run(
        self,
        cmd: list[str],
        cwd: str | Path,
        timeout: float = 60.0,
        max_output_chars: int = 50000,
        env: dict[str, str] | None = None,
    ) -> CommandExecution:
        """Execute a command bounded by timeout and maximum output capture length.

        Args:
            cmd: Command arguments list.
            cwd: Working directory as string or Path.
            timeout: Maximum allowed execution time in seconds.
            max_output_chars: Maximum characters retained for stdout and stderr.
            env: Optional environment variable overrides.

        Returns:
            CommandExecution containing captured outputs, returncode, and timing.
        """
        cmd_list = [str(arg) for arg in cmd]
        cwd_str = str(cwd) if str(cwd) else "."

        merged_env: dict[str, str] | None = None
        if env is not None:
            merged_env = os.environ.copy()
            merged_env.update({str(k): str(v) for k, v in env.items()})

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd_list,
                cwd=cwd_str,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout,
                env=merged_env,
            )
            duration = time.perf_counter() - start_time
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start_time
            raw_stdout = exc.stdout or ""
            raw_stderr = exc.stderr or ""
            if isinstance(raw_stdout, bytes):
                raw_stdout = raw_stdout.decode("utf-8", errors="replace")
            if isinstance(raw_stderr, bytes):
                raw_stderr = raw_stderr.decode("utf-8", errors="replace")

            timeout_msg = f"Command timed out after {timeout} seconds"
            stderr = f"{raw_stderr}\n{timeout_msg}".strip() if raw_stderr else timeout_msg
            stdout = raw_stdout
            exit_code = -1

        stdout = _truncate_output(stdout, max_output_chars)
        stderr = _truncate_output(stderr, max_output_chars)

        return CommandExecution(
            cmd=cmd_list,
            cwd=cwd_str,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
        )

    def detect_test_command(self, repo_path: str | Path) -> list[str]:
        """Detect the appropriate test command for a repository.

        Args:
            repo_path: Root path of repository to inspect.

        Returns:
            List of command arguments to run tests.
        """
        return detect_test_command(repo_path)

    def _has_unittest_files(self, repo_path: Path) -> bool:
        """Scan repository for presence of unittest test files."""
        return _has_unittest_files(repo_path)


def _detect_makefile_test_command(makefile_path: Path) -> list[str]:
    """Detect whether Makefile uses 'make test' or 'make check'."""
    try:
        content = makefile_path.read_text(encoding="utf-8", errors="ignore")
        has_test = bool(re.search(r"^[ \t]*test\b[^:=]*:", content, re.MULTILINE))
        has_check = bool(re.search(r"^[ \t]*check\b[^:=]*:", content, re.MULTILINE))
        if has_check and not has_test:
            return ["make", "check"]
    except OSError:
        pass
    return ["make", "test"]


def _has_unittest_files(repo_path: Path) -> bool:
    """Scan repository for presence of unittest test files."""
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and d not in _EXCLUDED_SCAN_DIRS
        ]
        for filename in files:
            if not filename.endswith(".py"):
                continue

            is_test_name = (
                filename.startswith("test_")
                or filename.endswith("_test.py")
                or filename.startswith("test")
                or filename in ("test.py", "tests.py")
            )
            file_path = Path(root) / filename
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            has_unittest_import = (
                "import unittest" in content
                or "from unittest" in content
                or "unittest.TestCase" in content
            )
            has_pytest_import = (
                "import pytest" in content or "from pytest" in content
            )

            if has_unittest_import:
                return True
            if is_test_name and not has_pytest_import:
                return True

    return False


def detect_test_command(repo_path: str | Path) -> list[str]:
    """Detect the appropriate test command for a repository.

    Supports multiple languages and frameworks:
    1. Python:
       - if pyproject.toml, setup.cfg, setup.py, or tests/ directory -> ["python", "-m", "pytest", "-q"]
       - if unittest test files -> ["python", "-m", "unittest"]
    2. JavaScript / TypeScript:
       - package.json -> check if test script exists (read package.json scripts.test):
         - if pnpm-lock.yaml -> ["pnpm", "test"]
         - if yarn.lock -> ["yarn", "test"]
         - if bun.lockb / bun.lock -> ["bun", "test"]
         - default -> ["npm", "test"]
    3. Rust:
       - Cargo.toml -> ["cargo", "test"]
    4. Go:
       - go.mod -> ["go", "test", "./..."]
    5. Ruby:
       - Gemfile / Rakefile -> ["bundle", "exec", "rake", "test"] or ["bundle", "exec", "rspec"]
    6. Java / Kotlin:
       - gradlew -> ["./gradlew", "test"]
       - pom.xml -> ["mvn", "test"]
    7. C / C++:
       - Makefile -> ["make", "test"] or ["make", "check"]
       - CMakeLists.txt -> ["ctest"]
    8. Fallback:
       - ["python", "-m", "pytest", "-q"]

    Args:
        repo_path: Root path of repository to inspect.

    Returns:
        List of command arguments to run tests.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return ["python", "-m", "pytest", "-q"]

    # 1. Python
    if (
        (repo / "pyproject.toml").is_file()
        or (repo / "setup.cfg").is_file()
        or (repo / "setup.py").is_file()
        or (repo / "tests").is_dir()
    ):
        return ["python", "-m", "pytest", "-q"]

    if _has_unittest_files(repo):
        return ["python", "-m", "unittest"]

    # 2. JavaScript / TypeScript
    package_json = repo / "package.json"
    if package_json.is_file():
        try:
            content = package_json.read_text(encoding="utf-8", errors="ignore")
            data = json.loads(content)
            if isinstance(data, dict):
                scripts = data.get("scripts")
                if isinstance(scripts, dict) and "test" in scripts and bool(scripts.get("test")):
                    if (repo / "pnpm-lock.yaml").is_file():
                        return ["pnpm", "test"]
                    if (repo / "yarn.lock").is_file():
                        return ["yarn", "test"]
                    if (repo / "bun.lockb").is_file() or (repo / "bun.lock").is_file():
                        return ["bun", "test"]
                    return ["npm", "test"]
        except Exception:
            pass

    # 3. Rust
    if (repo / "Cargo.toml").is_file():
        return ["cargo", "test"]

    # 4. Go
    if (repo / "go.mod").is_file():
        return ["go", "test", "./..."]

    # 5. Ruby
    gemfile = repo / "Gemfile"
    rakefile = repo / "Rakefile"
    rakefile_lower = repo / "rakefile"
    if gemfile.is_file() or rakefile.is_file() or rakefile_lower.is_file():
        if (repo / ".rspec").is_file() or (repo / "spec").is_dir():
            return ["bundle", "exec", "rspec"]
        for rpath in (gemfile, rakefile, rakefile_lower):
            if rpath.is_file():
                try:
                    rcontent = rpath.read_text(encoding="utf-8", errors="ignore").lower()
                    if "rspec" in rcontent:
                        return ["bundle", "exec", "rspec"]
                except OSError:
                    pass
        return ["bundle", "exec", "rake", "test"]

    # 6. Java / Kotlin
    if (repo / "gradlew").is_file():
        return ["./gradlew", "test"]
    if (repo / "pom.xml").is_file():
        return ["mvn", "test"]

    # 7. C / C++
    makefile = repo / "Makefile"
    makefile_lower = repo / "makefile"
    gnumakefile = repo / "GNUmakefile"
    for mf in (makefile, makefile_lower, gnumakefile):
        if mf.is_file():
            return _detect_makefile_test_command(mf)

    if (repo / "CMakeLists.txt").is_file():
        return ["ctest"]

    # 8. Fallback
    return ["python", "-m", "pytest", "-q"]
