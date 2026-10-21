"""Integration and unit tests for Shipwright multi-agent implementations.

Tests cover:
- MockProvider conforming to the Provider protocol and returning ProviderResponse.
- InvestigatorAgent: investigate returns summary, relevant_files, suggested_approach.
- ImplementerAgent: implement modifies files in the target worktree.
- VerifierAgent: runs test command and returns (passed, output).
- ReviewerAgent: review returns Review object with passed, confidence, findings.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from unittest.mock import MagicMock

import pytest

from shipwright.agents import (
    ImplementerAgent,
    InvestigatorAgent,
    ReviewerAgent,
    VerifierAgent,
)
from shipwright.models import Issue, Review
from shipwright.providers import Message, Provider, ProviderError, ProviderResponse
from shipwright.runner import CommandExecution, CommandRunner


class MockProvider:
    """Mock LLM provider implementing the Provider protocol."""

    def __init__(
        self,
        responses: Sequence[str] | str = "mock response",
        *,
        model: str = "mock-model",
        error: Exception | None = None,
    ) -> None:
        """Initialize mock provider with responses or an error.

        Args:
            responses: A single response string or a sequence of response strings.
            model: Model name string.
            error: Optional exception to raise when complete() is called.
        """
        self.model: str = model
        self.error: Exception | None = error
        self.calls: list[dict[str, Any]] = []

        if isinstance(responses, str):
            self._responses: list[str] = [responses]
        else:
            self._responses = list(responses)
        self._index: int = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Record the call and return a configured ProviderResponse."""
        self.calls.append(
            {
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.error is not None:
            raise self.error

        if self._index < len(self._responses):
            content = self._responses[self._index]
            self._index += 1
        elif self._responses:
            content = self._responses[-1]
        else:
            content = ""

        return ProviderResponse(
            content=content,
            model=self.model,
            finish_reason="stop",
        )


# ============================================================================
# MockProvider & Protocol Compliance Tests
# ============================================================================


def test_mock_provider_implements_protocol() -> None:
    """Verify that MockProvider implements the Provider protocol and returns ProviderResponse."""
    provider: Provider = MockProvider("hello world", model="test-provider-model")
    assert provider.model == "test-provider-model"

    messages = [{"role": "user", "content": "ping"}]
    response = provider.complete(messages, temperature=0.7, max_tokens=100)

    assert isinstance(response, ProviderResponse)
    assert response.content == "hello world"
    assert response.text == "hello world"
    assert response.model == "test-provider-model"
    assert response.finish_reason == "stop"

    mock_inst = provider  # type: ignore[assignment]
    assert len(mock_inst.calls) == 1
    assert mock_inst.calls[0]["messages"] == messages
    assert mock_inst.calls[0]["temperature"] == 0.7
    assert mock_inst.calls[0]["max_tokens"] == 100


def test_mock_provider_multiple_responses() -> None:
    """Verify that MockProvider can sequence through multiple responses."""
    provider = MockProvider(["first", "second", "third"])
    messages = [{"role": "user", "content": "next"}]

    assert provider.complete(messages).content == "first"
    assert provider.complete(messages).content == "second"
    assert provider.complete(messages).content == "third"
    # When exhausted, repeats the last response
    assert provider.complete(messages).content == "third"
    assert len(provider.calls) == 4


# ============================================================================
# InvestigatorAgent Tests
# ============================================================================


def test_investigator_returns_summary_relevant_files_suggested_approach() -> None:
    """Verify InvestigatorAgent.investigate returns summary, relevant_files, suggested_approach."""
    diagnosis = {
        "summary": "Fix off-by-one boundary check in pagination offset",
        "relevant_files": ["shipwright/models.py", "tests/test_models.py"],
        "suggested_approach": "Adjust the boundary index check and add unit tests.",
    }
    provider = MockProvider(json.dumps(diagnosis))
    agent = InvestigatorAgent(provider)

    repo_files = [
        "shipwright/__init__.py",
        "shipwright/models.py",
        "shipwright/runner.py",
        "tests/test_models.py",
    ]
    issue = Issue(
        number=42,
        title="Pagination off by one on last page",
        body="When requesting the final slice of results, an IndexError occurs.",
    )

    result = agent.investigate(
        repo_files=repo_files,
        issue=issue,
        worktree_path="/workspace/repo",
    )

    assert isinstance(result, dict)
    assert result["summary"] == "Fix off-by-one boundary check in pagination offset"
    assert result["relevant_files"] == ["shipwright/models.py", "tests/test_models.py"]
    assert result["suggested_approach"] == "Adjust the boundary index check and add unit tests."

    # Verify provider call captured context
    assert len(provider.calls) == 1
    call_messages = provider.calls[0]["messages"]
    prompt_text = " ".join(str(m.get("content", "")) for m in call_messages)
    assert "Pagination off by one on last page" in prompt_text
    assert "shipwright/models.py" in prompt_text


def test_investigator_parses_markdown_json_block() -> None:
    """Verify InvestigatorAgent parses JSON embedded inside markdown code blocks."""
    json_text = json.dumps(
        {
            "summary": "Handle None in config parser",
            "relevant_files": ["shipwright/config.py"],
            "suggested_approach": "Add default empty dictionary fallback.",
        }
    )
    markdown_response = f"Here is the investigation diagnosis:\n```json\n{json_text}\n```\nHope this helps!"
    provider = MockProvider(markdown_response)
    agent = InvestigatorAgent(provider)

    result = agent.investigate(
        repo_files=["shipwright/config.py"],
        issue="Config parsing fails when file is null",
        worktree_path="/tmp/worktree",
    )

    assert result["summary"] == "Handle None in config parser"
    assert result["relevant_files"] == ["shipwright/config.py"]
    assert result["suggested_approach"] == "Add default empty dictionary fallback."


def test_investigator_freeform_text_fallback() -> None:
    """Verify InvestigatorAgent extracts structure when the model returns freeform text."""
    freeform_response = (
        "### Summary\n"
        "The cache key generation crashes on non-hashable arguments.\n\n"
        "### Relevant Files\n"
        "- shipwright/cache.py\n\n"
        "### Suggested Approach\n"
        "Serialize dictionary arguments using sorted JSON before hashing.\n"
    )
    provider = MockProvider(freeform_response)
    agent = InvestigatorAgent(provider)

    result = agent.investigate(
        repo_files=["shipwright/cache.py", "shipwright/models.py"],
        issue={"title": "Cache key error", "body": "Unhashable type dict"},
        worktree_path="/tmp/worktree",
    )

    assert isinstance(result, dict)
    assert "cache key" in result["summary"].lower()
    assert "shipwright/cache.py" in result["relevant_files"]
    assert "suggested_approach" in result
    assert len(result["suggested_approach"]) > 0


def test_investigator_handles_provider_error() -> None:
    """Verify InvestigatorAgent handles provider exceptions gracefully."""
    provider = MockProvider(error=ProviderError("Rate limit exceeded", status_code=429))
    agent = InvestigatorAgent(provider)

    result = agent.investigate(
        repo_files=["shipwright/models.py"],
        issue=Issue(1, "Fix typo", "Typo in models"),
        worktree_path="/tmp/worktree",
    )

    assert isinstance(result, dict)
    assert "summary" in result
    assert "relevant_files" in result
    assert "suggested_approach" in result
    assert "failed" in result["summary"].lower() or "typo" in result["summary"].lower()


# ============================================================================
# ImplementerAgent Tests
# ============================================================================


def test_implementer_modifies_file_in_worktree(tmp_path: Path) -> None:
    """Verify ImplementerAgent modifies target files in the worktree directory."""
    # Setup initial file in the isolated worktree
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    target_file = src_dir / "calculator.py"
    target_file.write_text("def multiply(a: int, b: int) -> int:\n    return a + b\n")

    # Provider response proposing fix in JSON files format
    new_content = "def multiply(a: int, b: int) -> int:\n    return a * b\n"
    provider_response = json.dumps(
        {
            "files": [
                {
                    "path": "src/calculator.py",
                    "content": new_content,
                }
            ]
        }
    )
    provider = MockProvider(provider_response)
    agent = ImplementerAgent(provider)

    investigation = {
        "summary": "Fix multiplication operator bug",
        "relevant_files": ["src/calculator.py"],
        "suggested_approach": "Change + operator to * in multiply() function.",
    }
    issue = Issue(
        number=7,
        title="Multiply returns sum",
        body="multiply(2, 3) returns 5 instead of 6.",
    )

    modified_files = agent.implement(
        investigation=investigation,
        issue=issue,
        worktree_path=str(tmp_path),
    )

    assert "src/calculator.py" in modified_files
    # Verify the file on disk was modified with new contents
    updated_disk_content = target_file.read_text()
    assert updated_disk_content == new_content


def test_implementer_modifies_file_using_markdown_blocks(tmp_path: Path) -> None:
    """Verify ImplementerAgent supports markdown codeblock file patches."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True)
    test_file = tests_dir / "test_calc.py"
    test_file.write_text("# old test\n")

    markdown_patch = (
        "FILE: tests/test_calc.py\n"
        "```python\n"
        "def test_multiply():\n"
        "    assert multiply(2, 3) == 6\n"
        "```\n"
    )
    provider = MockProvider(markdown_patch)
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": ["tests/test_calc.py"]},
        issue="Add test for multiply",
        worktree_path=str(tmp_path),
    )

    assert "tests/test_calc.py" in modified
    assert "assert multiply(2, 3) == 6" in test_file.read_text()


def test_implementer_creates_new_file_in_worktree(tmp_path: Path) -> None:
    """Verify ImplementerAgent can create new files and parent directories in worktree."""
    new_rel_path = "pkg/subpkg/helper.py"
    content = "def helper() -> str:\n    return 'ready'\n"
    provider_response = json.dumps(
        {
            "files": [
                {
                    "path": new_rel_path,
                    "content": content,
                }
            ]
        }
    )
    provider = MockProvider(provider_response)
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": [new_rel_path]},
        issue=Issue(12, "Add helper", "Need helper module"),
        worktree_path=str(tmp_path),
    )

    assert new_rel_path in modified
    created_file = tmp_path / "pkg" / "subpkg" / "helper.py"
    assert created_file.exists()
    assert created_file.read_text() == content


def test_implementer_includes_previous_error_in_repair_loop(tmp_path: Path) -> None:
    """Verify ImplementerAgent includes previous failure evidence in repair loop prompt."""
    file_path = tmp_path / "fix.py"
    file_path.write_text("v = 1\n")

    provider = MockProvider(json.dumps({"files": [{"path": "fix.py", "content": "v = 2\n"}]}))
    agent = ImplementerAgent(provider)

    previous_error = "AssertionError: expected 2 but got 1 in tests/test_fix.py:10"
    agent.implement(
        investigation={"relevant_files": ["fix.py"]},
        issue="Fix v value",
        worktree_path=str(tmp_path),
        previous_error=previous_error,
    )

    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    prompt = " ".join(str(m.get("content", "")) for m in messages)
    assert previous_error in prompt


def test_implementer_handles_provider_error(tmp_path: Path) -> None:
    """Verify ImplementerAgent returns empty list and logs error when provider fails."""
    provider = MockProvider(error=RuntimeError("Provider timed out"))
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": ["sample.py"]},
        issue="Any issue",
        worktree_path=str(tmp_path),
    )

    assert modified == []
    assert agent.last_error is not None
    assert "Provider timed out" in agent.last_error


# ============================================================================
# VerifierAgent Tests
# ============================================================================


def test_verifier_runs_test_command_and_returns_passed_output() -> None:
    """Verify VerifierAgent.verify runs test command and returns (passed, output)."""
    # Mock runner with successful execution
    mock_runner = MagicMock()
    mock_runner.run.return_value = CommandExecution(
        cmd=["pytest", "tests/test_calculator.py"],
        cwd="/workspace/repo",
        exit_code=0,
        stdout="collected 3 items\n3 passed in 0.12s\n",
        stderr="",
        duration=0.12,
    )

    agent = VerifierAgent(mock_runner)
    passed, output = agent.verify(
        worktree_path="/workspace/repo",
        test_cmd=["pytest", "tests/test_calculator.py"],
    )

    assert passed is True
    assert "3 passed" in output
    mock_runner.run.assert_called_once_with(
        ["pytest", "tests/test_calculator.py"],
        cwd="/workspace/repo",
        timeout=120.0,
    )


def test_verifier_returns_failed_status_on_nonzero_exit() -> None:
    """Verify VerifierAgent returns passed=False on non-zero exit code."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = CommandExecution(
        cmd=["pytest"],
        cwd="/workspace/repo",
        exit_code=1,
        stdout="FAILED tests/test_calculator.py::test_add\n1 failed in 0.25s\n",
        stderr="ERROR: test run encountered failures\n",
        duration=0.25,
    )

    agent = VerifierAgent(mock_runner)
    passed, output = agent.verify(
        worktree_path="/workspace/repo",
        test_cmd=["pytest"],
    )

    assert passed is False
    assert "FAILED tests/test_calculator.py" in output
    assert "ERROR: test run encountered failures" in output


def test_verifier_detects_test_command_when_none_provided() -> None:
    """Verify VerifierAgent calls runner.detect_test_command when test_cmd is None."""
    mock_runner = MagicMock()
    mock_runner.detect_test_command.return_value = ["uv", "run", "pytest", "-q"]
    mock_runner.run.return_value = CommandExecution(
        cmd=["uv", "run", "pytest", "-q"],
        cwd="/tmp/worktree",
        exit_code=0,
        stdout="5 passed\n",
        stderr="",
        duration=0.4,
    )

    agent = VerifierAgent(mock_runner)
    passed, output = agent.verify(worktree_path="/tmp/worktree", test_cmd=None)

    assert passed is True
    assert "5 passed" in output
    mock_runner.detect_test_command.assert_called_once_with("/tmp/worktree")
    mock_runner.run.assert_called_once_with(
        ["uv", "run", "pytest", "-q"],
        cwd="/tmp/worktree",
        timeout=120.0,
    )


def test_verifier_with_real_command_runner(tmp_path: Path) -> None:
    """Verify VerifierAgent works with real CommandRunner execution."""
    runner = CommandRunner()
    agent = VerifierAgent(runner)

    # Test a command that passes
    pass_cmd = [sys.executable, "-c", "print('all checks passed')"]
    passed, out = agent.verify(str(tmp_path), test_cmd=pass_cmd)
    assert passed is True
    assert "all checks passed" in out

    # Test a command that fails
    fail_cmd = [sys.executable, "-c", "import sys; sys.stderr.write('failed!\\n'); sys.exit(2)"]
    failed, out_err = agent.verify(str(tmp_path), test_cmd=fail_cmd)
    assert failed is False
    assert "failed!" in out_err


# ============================================================================
# ReviewerAgent Tests
# ============================================================================


def test_reviewer_returns_review_object_with_passed_confidence_findings() -> None:
    """Verify ReviewerAgent.review returns Review dataclass with passed, confidence, findings."""
    review_data = {
        "passed": True,
        "confidence": 0.95,
        "summary": "Patch correctly implements bounds checking and passes all tests.",
        "findings": [
            "Boundary condition handled safely",
            "No regression in existing behavior",
        ],
    }
    provider = MockProvider(json.dumps(review_data))
    agent = ReviewerAgent(provider)

    issue = Issue(
        number=101,
        title="Check array bounds",
        body="Avoid index error on empty input",
    )
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x[0]\n+x[0] if x else None\n"
    test_output = "================ 1 passed in 0.05s ================"

    review = agent.review(
        issue=issue,
        diff=diff,
        test_output=test_output,
    )

    assert isinstance(review, Review)
    assert review.passed is True
    assert review.confidence == 0.95
    assert review.summary == "Patch correctly implements bounds checking and passes all tests."
    assert review.findings == [
        "Boundary condition handled safely",
        "No regression in existing behavior",
    ]

    # Check prompt included issue, diff, and test output
    assert len(provider.calls) == 1
    call_prompt = " ".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    assert "Check array bounds" in call_prompt
    assert "diff" in call_prompt
    assert "1 passed in 0.05s" in call_prompt


def test_reviewer_returns_failing_review() -> None:
    """Verify ReviewerAgent handles rejected/failed patch reviews."""
    review_data = {
        "passed": False,
        "confidence": 0.88,
        "summary": "Patch introduces an unhandled division by zero.",
        "findings": [
            "Division by zero when total_count is 0",
            "Missing regression test for zero-count case",
        ],
    }
    provider = MockProvider(json.dumps(review_data))
    agent = ReviewerAgent(provider)

    review = agent.review(
        issue="Calculate average score",
        diff="--- a/math.py\n+++ b/math.py\n+ return total / count",
        test_output="FAILED test_zero_count - ZeroDivisionError",
    )

    assert isinstance(review, Review)
    assert review.passed is False
    assert review.confidence == 0.88
    assert "division by zero" in review.summary.lower()
    assert len(review.findings) == 2


def test_reviewer_freeform_text_fallback() -> None:
    """Verify ReviewerAgent parses freeform review text into a Review object."""
    freeform_text = (
        "### Evaluation\n"
        "PASSED: Yes\n"
        "Confidence: 0.90\n\n"
        "### Summary\n"
        "The change is safe and meets requirements.\n\n"
        "### Findings\n"
        "- Clean patch without side effects\n"
        "- Test results confirm fix\n"
    )
    provider = MockProvider(freeform_text)
    agent = ReviewerAgent(provider)

    review = agent.review(
        issue="Simple bugfix",
        diff="+ safe_call()",
        test_output="passed",
    )

    assert isinstance(review, Review)
    assert isinstance(review.passed, bool)
    assert isinstance(review.confidence, float)
    assert 0.0 <= review.confidence <= 1.0
    assert isinstance(review.findings, list)
    assert isinstance(review.summary, str)


def test_reviewer_handles_provider_error() -> None:
    """Verify ReviewerAgent returns failing Review when provider raises an exception."""
    provider = MockProvider(error=ProviderError("Model overloaded", status_code=503))
    agent = ReviewerAgent(provider)

    review = agent.review(
        issue="Any issue",
        diff="diff --git a/f b/f",
        test_output="tests ran",
    )

    assert isinstance(review, Review)
    assert review.passed is False
    assert review.confidence == 0.0
    assert "error" in review.summary.lower() or "failed" in review.summary.lower()
    assert len(review.findings) > 0
