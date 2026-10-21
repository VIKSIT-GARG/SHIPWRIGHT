"""Unit tests for InvestigatorAgent."""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest

from shipwright.agents.investigator import InvestigatorAgent
from shipwright.models import Issue
from shipwright.providers import Message, ProviderError, ProviderResponse


class MockProvider:
    """Mock provider recording calls and returning configured responses."""

    def __init__(
        self,
        response_text: str | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.model = "test-model"
        self.response_text = response_text
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return ProviderResponse(content=self.response_text or "")


def test_investigator_clean_json_response() -> None:
    expected_data = {
        "summary": "Fix flaky retry boundary check",
        "relevant_files": ["shipwright/models.py", "tests/test_models.py"],
        "suggested_approach": "Update retry loop bounds and add regression test.",
    }
    provider = MockProvider(response_text=json.dumps(expected_data))
    agent = InvestigatorAgent(provider)

    repo_files = ["shipwright/models.py", "shipwright/providers.py", "tests/test_models.py"]
    issue = Issue(
        number=1,
        title="Flaky retry boundary",
        body="Retry loop fails on boundary condition.",
    )

    result = agent.investigate(repo_files, issue, "/tmp/worktree")

    assert result["summary"] == "Fix flaky retry boundary check"
    assert result["relevant_files"] == ["shipwright/models.py", "tests/test_models.py"]
    assert result["suggested_approach"] == "Update retry loop bounds and add regression test."
    assert len(provider.calls) == 1
    # Verify prompt construction
    messages = provider.calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "Flaky retry boundary" in messages[1]["content"]
    assert "shipwright/models.py" in messages[1]["content"]
    assert "/tmp/worktree" in messages[1]["content"]


def test_investigator_fenced_json_response() -> None:
    response_content = """Here is the investigation result:
```json
{
  "summary": "Missing None check in config parser",
  "relevant_files": ["shipwright/config.py"],
  "suggested_approach": "Add default fallback when config key is None."
}
```
Let me know if you need more details.
"""
    provider = MockProvider(response_text=response_content)
    agent = InvestigatorAgent(provider)

    result = agent.investigate(
        repo_files=["shipwright/config.py", "shipwright/cli.py"],
        issue={"title": "None config crash", "body": "Parser crashes on None"},
        worktree_path="/tmp/worktree",
    )

    assert result["summary"] == "Missing None check in config parser"
    assert result["relevant_files"] == ["shipwright/config.py"]
    assert "Add default fallback" in result["suggested_approach"]


def test_investigator_freeform_markdown_response() -> None:
    freeform_text = """### Summary
The token refresh mechanism expires earlier than expected.

### Relevant Files
- shipwright/auth.py
- tests/test_auth.py

### Suggested Approach
1. Increase token TTL buffer.
2. Add pytest case for edge timestamp.
"""
    provider = MockProvider(response_text=freeform_text)
    agent = InvestigatorAgent(provider)

    repo_files = ["shipwright/auth.py", "shipwright/models.py", "tests/test_auth.py"]
    issue = Issue(number=42, title="Auth token expiry", body="Tokens expire too early")

    result = agent.investigate(repo_files, issue, "/tmp/worktree")

    assert "expires earlier than expected" in result["summary"]
    assert "shipwright/auth.py" in result["relevant_files"]
    assert "tests/test_auth.py" in result["relevant_files"]
    assert "Increase token TTL buffer" in result["suggested_approach"]


def test_investigator_unstructured_text_fallback() -> None:
    freeform_text = (
        "The problem is located in shipwright/providers.py where timeout handling "
        "does not catch socket errors properly. We need to catch socket.timeout."
    )
    provider = MockProvider(response_text=freeform_text)
    agent = InvestigatorAgent(provider)

    repo_files = ["shipwright/providers.py", "shipwright/models.py"]
    issue = "Socket timeout in providers"

    result = agent.investigate(repo_files, issue, "/tmp/worktree")

    assert "shipwright/providers.py" in result["summary"]
    assert "shipwright/providers.py" in result["relevant_files"]
    assert result["suggested_approach"]


def test_investigator_provider_error_fallback() -> None:
    provider = MockProvider(error=ProviderError("Rate limit exceeded", status_code=429))
    agent = InvestigatorAgent(provider)

    repo_files = ["shipwright/models.py", "tests/test_models.py"]
    issue = {"title": "models test failure", "body": "test_models.py is failing"}

    result = agent.investigate(repo_files, issue, "/tmp/worktree")

    assert "Rate limit exceeded" in result["summary"]
    assert "tests/test_models.py" in result["relevant_files"] or "shipwright/models.py" in result["relevant_files"]
    assert "Manually review issue" in result["suggested_approach"]


def test_investigator_empty_response_fallback() -> None:
    provider = MockProvider(response_text="")
    agent = InvestigatorAgent(provider)

    repo_files = ["shipwright/cli.py"]
    issue = Issue(number=5, title="CLI argument parsing", body="cli.py lacks verbose flag")

    result = agent.investigate(repo_files, issue, "/tmp/worktree")

    assert "CLI argument parsing" in result["summary"]
    assert "shipwright/cli.py" in result["relevant_files"]
    assert result["suggested_approach"]
