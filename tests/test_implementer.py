"""Unit tests for ImplementerAgent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from shipwright.agents.implementer import ImplementerAgent
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


def test_implementer_init() -> None:
    provider = MockProvider("OK")
    agent = ImplementerAgent(provider)
    assert agent.provider is provider
    assert agent.last_error is None


def test_implementer_markdown_codeblocks(tmp_path: Path) -> None:
    code = (
        "FILE: src/math.py\n"
        "```python\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "```\n\n"
        "FILE: tests/test_math.py\n"
        "```python\n"
        "from src.math import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "```\n"
    )
    provider = MockProvider(response_text=code)
    agent = ImplementerAgent(provider)

    investigation = {
        "summary": "Implement addition helper",
        "relevant_files": ["src/math.py", "tests/test_math.py"],
        "suggested_approach": "Create add function and unit test.",
    }
    issue = Issue(
        number=10,
        title="Add math add function",
        body="We need a reliable add function.",
    )

    modified = agent.implement(investigation, issue, str(tmp_path))

    assert "src/math.py" in modified
    assert "tests/test_math.py" in modified
    assert (tmp_path / "src/math.py").exists()
    assert "def add(a: int, b: int) -> int:" in (tmp_path / "src/math.py").read_text()
    assert (tmp_path / "tests/test_math.py").exists()
    assert "assert add(1, 2) == 3" in (tmp_path / "tests/test_math.py").read_text()


def test_implementer_markdown_comment(tmp_path: Path) -> None:
    code = (
        "```python\n"
        "# FILE: utils/string.py\n"
        "def trim(s: str) -> str:\n"
        "    return s.strip()\n"
        "```\n"
    )
    provider = MockProvider(response_text=code)
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": ["utils/string.py"]},
        issue="Trim string helper",
        worktree_path=str(tmp_path),
    )

    assert modified == ["utils/string.py"]
    written = (tmp_path / "utils/string.py").read_text()
    assert "def trim(s: str) -> str:" in written
    assert "# FILE:" not in written


def test_implementer_json_response(tmp_path: Path) -> None:
    payload = {
        "files": [
            {
                "path": "service/user.py",
                "content": "class UserService:\n    pass\n",
            }
        ]
    }
    provider = MockProvider(response_text=json.dumps(payload))
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"summary": "Add UserService", "relevant_files": ["service/user.py"]},
        issue={"title": "Create user service"},
        worktree_path=str(tmp_path),
    )

    assert modified == ["service/user.py"]
    assert (tmp_path / "service/user.py").read_text() == "class UserService:\n    pass\n"


def test_implementer_json_dict_response(tmp_path: Path) -> None:
    payload = {
        "config/settings.py": "DEBUG = False\n",
    }
    provider = MockProvider(response_text=f"```json\n{json.dumps(payload)}\n```")
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": ["config/settings.py"]},
        issue="Disable debug in settings",
        worktree_path=str(tmp_path),
    )

    assert modified == ["config/settings.py"]
    assert (tmp_path / "config/settings.py").read_text() == "DEBUG = False\n"


def test_implementer_repair_loop_previous_error(tmp_path: Path) -> None:
    code = (
        "FILE: fix.py\n"
        "```python\n"
        "FIXED = True\n"
        "```\n"
    )
    provider = MockProvider(response_text=code)
    agent = ImplementerAgent(provider)

    test_failure = "FAILED tests/test_fix.py::test_fix - AssertionError: assert False"
    modified = agent.implement(
        investigation={"summary": "Fix assertion error"},
        issue="Test failure in CI",
        worktree_path=str(tmp_path),
        previous_error=test_failure,
    )

    assert modified == ["fix.py"]
    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    user_prompt = messages[1]["content"]
    assert "Repair Loop" in user_prompt
    assert test_failure in user_prompt


def test_implementer_reads_existing_worktree_files(tmp_path: Path) -> None:
    existing_file = tmp_path / "src" / "existing.py"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text("def old_impl():\n    return 42\n")

    code = (
        "FILE: src/existing.py\n"
        "```python\n"
        "def new_impl():\n"
        "    return 100\n"
        "```\n"
    )
    provider = MockProvider(response_text=code)
    agent = ImplementerAgent(provider)

    agent.implement(
        investigation={"relevant_files": ["src/existing.py"]},
        issue="Update implementation",
        worktree_path=str(tmp_path),
    )

    messages = provider.calls[0]["messages"]
    user_prompt = messages[1]["content"]
    assert "src/existing.py" in user_prompt
    assert "def old_impl():" in user_prompt
    assert (tmp_path / "src/existing.py").read_text() == "def new_impl():\n    return 100\n"


def test_implementer_directory_traversal_prevention(tmp_path: Path) -> None:
    code = (
        "FILE: ../../outside.py\n"
        "```python\n"
        "malicious = True\n"
        "```\n"
        "FILE: safe.py\n"
        "```python\n"
        "safe = True\n"
        "```\n"
    )
    provider = MockProvider(response_text=code)
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={},
        issue="Security test",
        worktree_path=str(tmp_path),
    )

    assert modified == ["safe.py"]
    assert (tmp_path / "safe.py").exists()
    assert not (tmp_path.parent / "outside.py").exists()


def test_implementer_provider_failure_handled_safely(tmp_path: Path) -> None:
    provider = MockProvider(error=ProviderError("Connection refused"))
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={"relevant_files": ["foo.py"]},
        issue="Fix something",
        worktree_path=str(tmp_path),
    )

    assert modified == []
    assert agent.last_error is not None
    assert "Connection refused" in agent.last_error


def test_implementer_empty_worktree_path() -> None:
    provider = MockProvider("OK")
    agent = ImplementerAgent(provider)

    modified = agent.implement(
        investigation={},
        issue="Test",
        worktree_path="",
    )

    assert modified == []
    assert agent.last_error == "worktree_path must not be empty"
