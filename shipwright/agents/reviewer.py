"""Reviewer agent for assessing patch correctness, safety, and test results."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from shipwright.models import Review
from shipwright.providers import Provider

logger = logging.getLogger(__name__)


def _extract_issue_info(issue: Any) -> tuple[str, str, int | None]:
    """Extract (title, body, number) from an issue instance, mapping, or string.

    Args:
        issue: Issue object, dictionary, or string representation.

    Returns:
        A tuple of (title, body, number).
    """
    if issue is None:
        return "", "", None
    if isinstance(issue, Mapping):
        title = str(issue.get("title", "") or "").strip()
        body = str(issue.get("body", "") or "").strip()
        number = issue.get("number")
        return (
            title,
            body,
            int(number) if isinstance(number, (int, str)) and str(number).isdigit() else None,
        )
    if hasattr(issue, "title") or hasattr(issue, "body"):
        title = str(getattr(issue, "title", "") or "").strip()
        body = str(getattr(issue, "body", "") or "").strip()
        number = getattr(issue, "number", None)
        return (
            title,
            body,
            int(number) if isinstance(number, (int, str)) and str(number).isdigit() else None,
        )
    if isinstance(issue, str):
        lines = issue.strip().splitlines()
        if not lines:
            return "", "", None
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        return title, body, None
    return str(issue).strip(), "", None


def _extract_response_text(response: Any) -> str:
    """Safely extract generated text from a provider response object or string."""
    if response is None:
        return ""
    if hasattr(response, "content") and response.content is not None:
        return str(response.content)
    if hasattr(response, "text") and response.text is not None:
        return str(response.text)
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping) and "content" in response:
        return str(response["content"])
    return str(response)


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    """Attempt to extract and parse a JSON dictionary from raw model output."""
    stripped = text.strip()
    if not stripped:
        return None

    # 1. Try parsing full text directly
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 2. Try parsing fenced code blocks (```json ... ``` or ``` ... ```)
    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    for block in fenced_blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict):
                return data
        except Exception:
            continue

    # 3. Try finding outermost JSON object braces { ... }
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = stripped[first_brace : last_brace + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return None


def _parse_confidence(val: Any) -> float:
    """Parse and normalize confidence score to a float within [0.0, 1.0]."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        num = float(val)
    elif isinstance(val, str):
        cleaned = val.strip().rstrip("%")
        try:
            num = float(cleaned)
        except ValueError:
            return 0.0
    else:
        return 0.0

    if isinstance(val, str) and "%" in val:
        num = num / 100.0
    elif num > 1.0:
        if num <= 100.0:
            num = num / 100.0
        else:
            num = 1.0

    return max(0.0, min(1.0, num))


def _parse_review_from_dict(data: dict[str, Any], raw_text: str = "") -> Review:
    """Convert parsed JSON dictionary into a Review model instance."""
    # 1. passed: bool
    raw_passed = data.get("passed")
    if isinstance(raw_passed, bool):
        passed = raw_passed
    elif isinstance(raw_passed, (int, float)):
        passed = bool(raw_passed)
    elif isinstance(raw_passed, str):
        passed = raw_passed.strip().lower() in {"true", "yes", "passed", "pass", "1", "ok", "success"}
    else:
        passed = False

    # 2. confidence: float
    raw_confidence = data.get("confidence")
    confidence = _parse_confidence(raw_confidence)

    # 3. summary: str
    raw_summary = data.get("summary") or data.get("verdict") or data.get("conclusion")
    if isinstance(raw_summary, list):
        summary = " ".join(str(s) for s in raw_summary).strip()
    elif raw_summary is not None:
        summary = str(raw_summary).strip()
    else:
        summary = ""

    if not summary:
        summary = "Patch meets requirements safely." if passed else "Patch did not meet review requirements."

    # 4. findings: list[str]
    raw_findings = data.get("findings") or data.get("notes") or data.get("details") or data.get("issues")
    findings: list[str] = []
    if isinstance(raw_findings, list):
        for item in raw_findings:
            item_str = str(item).strip()
            if item_str:
                findings.append(item_str)
    elif isinstance(raw_findings, str) and raw_findings.strip():
        findings.append(raw_findings.strip())

    return Review(
        passed=passed,
        confidence=confidence,
        summary=summary,
        findings=findings,
    )


def _parse_freeform_review(text: str) -> Review:
    """Parse freeform/unstructured text as a fallback when JSON is malformed."""
    stripped = text.strip()
    if not stripped:
        return Review(
            passed=False,
            confidence=0.0,
            summary="Review failed: empty model response.",
            findings=["Model returned an empty response."],
        )

    # Check for explicit passed / failed patterns
    passed_pattern = re.search(
        r"\b(?:passed|verdict|status|result)\s*[:=]\s*(true|false|pass|passed|fail|failed|yes|no)\b",
        stripped,
        re.IGNORECASE,
    )
    if passed_pattern:
        verdict = passed_pattern.group(1).lower()
        passed = verdict in {"true", "pass", "passed", "yes"}
    else:
        # Check if the overall text indicates approval or rejection
        if re.search(r"\b(?:approved|lgtm|patch looks good|all tests pass(?:ed)?)\b", stripped, re.IGNORECASE):
            passed = True
        elif re.search(
            r"\b(?:rejected|changes requested|tests? failed|regression detected)\b",
            stripped,
            re.IGNORECASE,
        ):
            passed = False
        else:
            passed = False

    # Check for confidence
    conf_pattern = re.search(
        r"\bconfidence\s*[:=]\s*([0-9]+(?:\.[0-9]+)?%?)",
        stripped,
        re.IGNORECASE,
    )
    if conf_pattern:
        confidence = _parse_confidence(conf_pattern.group(1))
    else:
        confidence = 0.5 if passed else 0.0

    # Extract summary
    summary_pattern = re.search(
        r"\bsummary\s*[:=]\s*(.+?)(?:\n\s*[A-Z]|\n\n|\Z)",
        stripped,
        re.IGNORECASE | re.DOTALL,
    )
    if summary_pattern:
        summary = summary_pattern.group(1).strip()
    else:
        summary = (
            "Patch review passed based on freeform evaluation."
            if passed
            else "Review failed: unstructured or malformed model response."
        )

    # Extract findings (bullet points or numbered list)
    findings: list[str] = []
    for line in stripped.splitlines():
        match = re.match(r"^\s*[-*•]\s+(.*)$", line) or re.match(r"^\s*\d+\.\s+(.*)$", line)
        if match:
            finding = match.group(1).strip()
            if finding:
                findings.append(finding)

    if not findings and not passed:
        findings.append(f"Model response was unstructured: {stripped[:150]}")

    return Review(
        passed=passed,
        confidence=confidence,
        summary=summary,
        findings=findings,
    )


def _build_review_messages(
    issue_title: str,
    issue_body: str,
    issue_number: int | None,
    diff: str,
    test_output: str,
) -> list[dict[str, str]]:
    """Construct chat messages sent to the provider for patch review.

    Args:
        issue_title: Title of the issue.
        issue_body: Body/description of the issue.
        issue_number: Optional issue number.
        diff: Git patch / diff string.
        test_output: Output from the test run execution.

    Returns:
        List of chat message dicts suitable for Provider.complete().
    """
    system_prompt = (
        "You are Shipwright's autonomous code reviewer agent.\n"
        "Your task is to review a proposed git diff to ensure it correctly and safely resolves an issue, "
        "and that the automated test execution results confirm the fix without introducing regressions.\n\n"
        "Guidelines for evaluation:\n"
        "1. Check if the diff addresses the core problem described in the issue.\n"
        "2. Check for security vulnerabilities, safety hazards, resource leaks, or edge cases.\n"
        "3. Inspect the test output to verify that all relevant tests passed and no regressions occurred.\n"
        "4. Assign 'passed': true ONLY if the patch meets requirements safely and tests succeeded.\n"
        "5. Assign 'confidence': a float between 0.0 and 1.0 based on how thoroughly the change is verified.\n\n"
        "You MUST respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "passed": <true or false>,\n'
        '  "confidence": <float between 0.0 and 1.0>,\n'
        '  "summary": "<concise summary of the review verdict>",\n'
        '  "findings": ["<finding 1>", "<finding 2>", ...]\n'
        "}\n\n"
        "Do not include any text outside of the JSON object."
    )

    issue_header = (
        f"Issue #{issue_number}: {issue_title}"
        if issue_number is not None
        else f"Issue: {issue_title or 'Untitled issue'}"
    )
    diff_section = diff.strip() if diff and diff.strip() else "(No diff provided / empty change)"
    test_section = test_output.strip() if test_output and test_output.strip() else "(No test output provided)"

    user_content = (
        f"{issue_header}\n\n"
        f"### Description\n{issue_body or '(No description provided)'}\n\n"
        f"### Git Diff\n```diff\n{diff_section}\n```\n\n"
        f"### Test Execution Output\n```\n{test_section}\n```\n\n"
        "Please evaluate this patch against the issue requirements and test outcome, and return your review strictly in the specified JSON format."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


class ReviewerAgent:
    """Agent responsible for reviewing proposed code changes against issue requirements and test results."""

    def __init__(self, provider: Provider | Any) -> None:
        """Initialize the ReviewerAgent with a model provider.

        Args:
            provider: A Provider instance from shipwright.providers or duck-typed provider.
        """
        self.provider = provider

    def review(
        self,
        issue: Any,
        diff: str,
        test_output: str,
    ) -> Review:
        """Review a git diff and test output against issue specifications.

        Constructs a structured review prompt including issue title/body, git diff,
        and test run outcome/output. Calls the provider and parses the evaluation
        into a Review object.

        Args:
            issue: Issue object, dict, or string description.
            diff: Git patch / diff string representing proposed changes.
            test_output: Captured stdout/stderr and outcome from test execution.

        Returns:
            A Review dataclass instance from shipwright.models containing passed status,
            confidence, summary, and detailed findings.
        """
        issue_title, issue_body, issue_number = _extract_issue_info(issue)

        messages = _build_review_messages(
            issue_title=issue_title,
            issue_body=issue_body,
            issue_number=issue_number,
            diff=diff,
            test_output=test_output,
        )

        try:
            response = self.provider.complete(messages=messages)
            raw_text = _extract_response_text(response)
        except Exception as exc:
            logger.warning("Provider complete call failed during review: %s", exc)
            return Review(
                passed=False,
                confidence=0.0,
                summary=f"Review failed: provider error ({exc})",
                findings=[f"Provider completion raised {type(exc).__name__}: {exc}"],
            )

        if not raw_text.strip():
            logger.warning("Provider returned empty response during review.")
            return Review(
                passed=False,
                confidence=0.0,
                summary="Review failed: empty model response.",
                findings=["Provider returned empty content."],
            )

        json_data = _extract_json_payload(raw_text)
        if json_data is not None:
            return _parse_review_from_dict(json_data, raw_text=raw_text)

        logger.info("Structured JSON extraction failed; falling back to freeform text parsing.")
        return _parse_freeform_review(raw_text)
