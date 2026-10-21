"""Verifier agent for executing tests and reporting validation results."""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shipwright.runner import CommandRunner

logger = logging.getLogger(__name__)

__all__ = ["VerifierAgent"]


class VerifierAgent:
    """Agent responsible for running and validating repository tests in a worktree."""

    def __init__(self, runner: Any) -> None:
        """Initialize the VerifierAgent.

        Args:
            runner: CommandRunner instance (from shipwright.runner) used to execute commands.
        """
        self.runner = runner

    def verify(
        self,
        worktree_path: str,
        test_cmd: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Verify the repository worktree by running test commands.

        If test_cmd is None, detect using self.runner.detect_test_command(worktree_path).
        Runs the command via runner.run(test_cmd, cwd=worktree_path, timeout=120.0).

        Args:
            worktree_path: Path to the target repository worktree directory.
            test_cmd: Optional explicit test command argv list. If None, detected automatically.

        Returns:
            A tuple of (passed, output), where passed is True if exit_code == 0,
            and output combines stdout and stderr streams.
        """
        if test_cmd is None:
            test_cmd = self.runner.detect_test_command(worktree_path)

        if isinstance(test_cmd, str):
            test_cmd = shlex.split(test_cmd)
        elif isinstance(test_cmd, tuple):
            test_cmd = list(test_cmd)

        if not test_cmd:
            return False, "No test command detected or provided."

        result = self.runner.run(test_cmd, cwd=worktree_path, timeout=120.0)

        # Determine pass/fail status based on exit code 0
        exit_code = getattr(result, "exit_code", None)
        if exit_code is None or not isinstance(exit_code, int):
            returncode = getattr(result, "returncode", None)
            if isinstance(returncode, int):
                exit_code = returncode
            elif isinstance(result, tuple) and len(result) >= 1 and isinstance(result[0], int):
                exit_code = result[0]
            elif isinstance(result, dict):
                raw_code = result.get("exit_code", result.get("returncode", 1))
                exit_code = raw_code if isinstance(raw_code, int) else 1
            else:
                exit_code = 1

        passed = (exit_code == 0)

        # Extract combined stdout and stderr output
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)

        if isinstance(stdout, str) or isinstance(stderr, str):
            stdout_str = stdout if isinstance(stdout, str) else ""
            stderr_str = stderr if isinstance(stderr, str) else ""
            if stdout_str and stderr_str:
                output = f"{stdout_str}\n{stderr_str}"
            else:
                output = stdout_str or stderr_str
        elif hasattr(result, "combined_output") and isinstance(result.combined_output, str):
            output = result.combined_output
        elif hasattr(result, "output") and isinstance(result.output, str):
            output = result.output
        elif isinstance(result, dict):
            stdout_str = str(result.get("stdout") or "")
            stderr_str = str(result.get("stderr") or "")
            if stdout_str and stderr_str:
                output = f"{stdout_str}\n{stderr_str}"
            else:
                output = stdout_str or stderr_str or str(result.get("output") or "")
        elif isinstance(result, tuple):
            if len(result) == 2:
                output = str(result[1])
            elif len(result) >= 3:
                s1 = str(result[1])
                s2 = str(result[2])
                output = f"{s1}\n{s2}" if s1 and s2 else (s1 or s2)
            else:
                output = ""
        else:
            output = ""

        return passed, output
