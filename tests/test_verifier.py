"""Unit tests for VerifierAgent."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from shipwright.agents.verifier import VerifierAgent
from shipwright.runner import CommandExecution, CommandRunner


def test_verifier_init() -> None:
    runner = Mock()
    agent = VerifierAgent(runner)
    assert agent.runner is runner


def test_verifier_with_explicit_command_success() -> None:
    runner = Mock()
    runner.run.return_value = CommandExecution(
        cmd=["pytest", "-q"],
        cwd="/tmp/worktree",
        exit_code=0,
        stdout="5 passed in 0.2s\n",
        stderr="",
        duration=0.2,
    )
    agent = VerifierAgent(runner)

    passed, output = agent.verify("/tmp/worktree", test_cmd=["pytest", "-q"])

    assert passed is True
    assert output == "5 passed in 0.2s\n"
    runner.detect_test_command.assert_not_called()
    runner.run.assert_called_once_with(["pytest", "-q"], cwd="/tmp/worktree", timeout=120.0)


def test_verifier_with_explicit_command_failure() -> None:
    runner = Mock()
    runner.run.return_value = CommandExecution(
        cmd=["pytest", "-q"],
        cwd="/tmp/worktree",
        exit_code=1,
        stdout="1 failed, 4 passed\n",
        stderr="AssertionError: values differ",
        duration=0.5,
    )
    agent = VerifierAgent(runner)

    passed, output = agent.verify("/tmp/worktree", test_cmd=["pytest", "-q"])

    assert passed is False
    assert "1 failed, 4 passed" in output
    assert "AssertionError: values differ" in output
    runner.detect_test_command.assert_not_called()
    runner.run.assert_called_once_with(["pytest", "-q"], cwd="/tmp/worktree", timeout=120.0)


def test_verifier_auto_detect_command() -> None:
    runner = Mock()
    runner.detect_test_command.return_value = ["python", "-m", "pytest", "-q"]
    runner.run.return_value = CommandExecution(
        cmd=["python", "-m", "pytest", "-q"],
        cwd="/tmp/repo",
        exit_code=0,
        stdout="all green\n",
        stderr="",
        duration=0.3,
    )
    agent = VerifierAgent(runner)

    passed, output = agent.verify("/tmp/repo")

    assert passed is True
    assert output == "all green\n"
    runner.detect_test_command.assert_called_once_with("/tmp/repo")
    runner.run.assert_called_once_with(
        ["python", "-m", "pytest", "-q"],
        cwd="/tmp/repo",
        timeout=120.0,
    )


def test_verifier_output_combining() -> None:
    runner = Mock()
    runner.run.return_value = Mock(
        exit_code=0,
        stdout="Standard output line",
        stderr="Standard error warning",
    )
    agent = VerifierAgent(runner)

    passed, output = agent.verify("/tmp", test_cmd=["test"])

    assert passed is True
    assert output == "Standard output line\nStandard error warning"


def test_verifier_no_command_detected() -> None:
    runner = Mock()
    runner.detect_test_command.return_value = None
    agent = VerifierAgent(runner)

    passed, output = agent.verify("/tmp")

    assert passed is False
    assert "No test command" in output
    runner.run.assert_not_called()


def test_verifier_end_to_end_with_real_runner(tmp_path: Path) -> None:
    runner = CommandRunner()
    agent = VerifierAgent(runner)

    # Run a command that succeeds
    passed, output = agent.verify(
        str(tmp_path),
        test_cmd=[sys.executable, "-c", "print('tests OK')"],
    )
    assert passed is True
    assert output.strip() == "tests OK"

    # Run a command that fails
    passed, output = agent.verify(
        str(tmp_path),
        test_cmd=[sys.executable, "-c", "import sys; sys.stderr.write('failed!'); sys.exit(2)"],
    )
    assert passed is False
    assert "failed!" in output
