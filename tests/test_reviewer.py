"""Unit tests for ReviewerAgent."""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest

from shipwright.agents.reviewer import ReviewerAgent
from shipwright.models import Issue, Review
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
        self.calls.append({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self.error:
            raise self.error
        return ProviderResponse(content=self.response_text or "")


def test_reviewer_clean_json_approved() -> None:
    expected_data = {
        "passed": True,
        "confidence": 0.95,
        "summary": "Patch correctly handles retry boundary conditions.",
        "findings": ["Boundary check updated properly", "All tests pass without regression"],
    }
    provider = MockProvider(response_text=json.dumps(expected_data))
    agent = ReviewerAgent(provider)

    issue = Issue(
        number=42,
        title="Fix retry loop boundary",
        body="Off-by-one error when max_retries is reached.",
    )
    diff = "--- a/src/retry.py\n+++ b/src/retry.py\n@@ -10,3 +10,3 @@\n-    while retries < max_retries:\n+    while retries <= max_retries:"
    test_output = "================ 42 passed in 1.23s ================"

    review = agent.review(issue=issue, diff=diff, test_output=test_output)

    assert isinstance(review, Review)
    assert review.passed is True
    assert review.confidence == 0.95
    assert review.summary == "Patch correctly handles retry boundary conditions."
    assert review.findings == [
        "Boundary check updated properly",
        "All tests pass without regression",
    ]

    # Check provider call
    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    user_prompt = messages[1]["content"]
    assert "Issue #42: Fix retry loop boundary" in user_prompt
    assert "Off-by-one error" in user_prompt
    assert "while retries <= max_retries" in user_prompt
    assert "42 passed" in user_prompt


def test_reviewer_clean_json_rejected() -> None:
    expected_data = {
        "passed": False,
        "confidence": 0.88,
        "summary": "Tests failed due to unhandled NoneType.",
        "findings": ["Null pointer exception in test run", "Missing null check in patch"],
    }
    provider = MockProvider(response_text=json.dumps(expected_data))
    agent = ReviewerAgent(provider)

    review = agent.review(
        issue={"title": "Handle null config", "body": "Config can be null on startup"},
        diff="+ if not config: return",
        test_output="FAILED test_config.py::test_null - TypeError: NoneType object is not subscriptable",
    )

    assert review.passed is False
    assert review.confidence == 0.88
    assert "Tests failed" in review.summary
    assert len(review.findings) == 2


def test_reviewer_markdown_fenced_json() -> None:
    fenced_content = """Here is my review evaluation:

```json
{
    "passed": true,
    "confidence": 0.9,
    "summary": "Looks good and safe.",
    "findings": ["Clean code"]
}
```
Hope this helps!"""

    provider = MockProvider(response_text=fenced_content)
    agent = ReviewerAgent(provider)

    review = agent.review(
        issue="Simple bugfix",
        diff="+ fixed",
        test_output="1 passed",
    )

    assert review.passed is True
    assert review.confidence == 0.9
    assert review.summary == "Looks good and safe."
    assert review.findings == ["Clean code"]


def test_reviewer_confidence_normalization() -> None:
    # Test percentage string "85%"
    data = {
        "passed": True,
        "confidence": "85%",
        "summary": "Good",
        "findings": [],
    }
    provider = MockProvider(response_text=json.dumps(data))
    agent = ReviewerAgent(provider)
    review = agent.review("Issue", "diff", "output")
    assert review.confidence == 0.85

    # Test integer scale 90
    data["confidence"] = 90
    provider.response_text = json.dumps(data)
    review = agent.review("Issue", "diff", "output")
    assert review.confidence == 0.9

    # Test clamping > 100
    data["confidence"] = 150
    provider.response_text = json.dumps(data)
    review = agent.review("Issue", "diff", "output")
    assert review.confidence == 1.0

    # Test clamping negative
    data["confidence"] = -0.5
    provider.response_text = json.dumps(data)
    review = agent.review("Issue", "diff", "output")
    assert review.confidence == 0.0


def test_reviewer_freeform_text_fallback() -> None:
    freeform_text = """
Verdict: passed
Confidence: 0.8
Summary: The changes look solid and tests pass cleanly.
- Good edge case coverage
- Follows coding style
"""
    provider = MockProvider(response_text=freeform_text)
    agent = ReviewerAgent(provider)

    review = agent.review("Issue", "diff", "output")
    assert review.passed is True
    assert review.confidence == 0.8
    assert "The changes look solid" in review.summary
    assert len(review.findings) == 2
    assert "Good edge case coverage" in review.findings


def test_reviewer_malformed_response_safe_fallback() -> None:
    garbage_text = "Sorry, I am an AI and cannot evaluate git diffs at this moment."
    provider = MockProvider(response_text=garbage_text)
    agent = ReviewerAgent(provider)

    review = agent.review("Issue", "diff", "output")
    assert review.passed is False
    assert review.confidence == 0.0
    assert "Review" in review.summary
    assert len(review.findings) > 0


def test_reviewer_empty_response_safe_fallback() -> None:
    provider = MockProvider(response_text="   \n   ")
    agent = ReviewerAgent(provider)

    review = agent.review("Issue", "diff", "output")
    assert review.passed is False
    assert review.confidence == 0.0
    assert "empty" in review.summary.lower()


def test_reviewer_provider_exception_safe_fallback() -> None:
    provider = MockProvider(error=ProviderError("Rate limit exceeded"))
    agent = ReviewerAgent(provider)

    review = agent.review("Issue", "diff", "output")
    assert review.passed is False
    assert review.confidence == 0.0
    assert "provider error" in review.summary.lower()
    assert any("Rate limit exceeded" in f for f in review.findings)


def test_reviewer_handles_various_issue_inputs() -> None:
    provider = MockProvider(response_text=json.dumps({"passed": True, "confidence": 1.0, "summary": "OK"}))
    agent = ReviewerAgent(provider)

    # String issue
    rev1 = agent.review(issue="Plain string issue", diff="", test_output="")
    assert rev1.passed is True

    # None issue
    rev2 = agent.review(issue=None, diff="", test_output="")
    assert rev2.passed is True

    # Dict without body
    rev3 = agent.review(issue={"title": "Only title"}, diff="", test_output="")
    assert rev3.passed is True
