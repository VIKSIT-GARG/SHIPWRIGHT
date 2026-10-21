"""Investigator agent for diagnosing issues and recommending repair approaches."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from shipwright.providers import Provider, ProviderResponse

logger = logging.getLogger(__name__)


def _extract_issue_info(issue: Any) -> tuple[str, str]:
    """Extract (title, body) from an issue instance, mapping, or string.

    Args:
        issue: Issue object, dictionary, or string representation.

    Returns:
        A tuple of (title, body) strings.
    """
    if issue is None:
        return "", ""
    if isinstance(issue, Mapping):
        title = str(issue.get("title", "") or "").strip()
        body = str(issue.get("body", "") or "").strip()
        return title, body
    if hasattr(issue, "title") or hasattr(issue, "body"):
        title = str(getattr(issue, "title", "") or "").strip()
        body = str(getattr(issue, "body", "") or "").strip()
        return title, body
    if isinstance(issue, str):
        lines = issue.strip().splitlines()
        if not lines:
            return "", ""
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        return title, body
    return str(issue).strip(), ""


def _build_investigation_messages(
    repo_files: list[str],
    issue_title: str,
    issue_body: str,
    worktree_path: str,
) -> list[dict[str, str]]:
    """Construct chat messages sent to the provider for investigation.

    Args:
        repo_files: List of file paths relative to the repository root.
        issue_title: Title of the issue.
        issue_body: Detailed description of the issue.
        worktree_path: Absolute path to the isolated worktree directory.

    Returns:
        A list of chat messages suitable for Provider.complete().
    """
    formatted_files = (
        "\n".join(f"- {f}" for f in repo_files)
        if repo_files
        else "(No repository files available)"
    )

    system_prompt = (
        "You are Shipwright's autonomous investigator agent.\n"
        "Your task is to analyze a reported software issue and the repository file tree, "
        "diagnose the root cause or required enhancement, select the files that need inspection or modification, "
        "and produce a concrete, step-by-step suggested approach.\n\n"
        "You MUST respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "summary": "<brief problem diagnosis>",\n'
        '  "relevant_files": ["<file_path_1>", "<file_path_2>"],\n'
        '  "suggested_approach": "<recommended plan to resolve the issue>"\n'
        "}\n\n"
        "Do not include any text outside of the JSON object."
    )

    user_content = (
        f"Issue Title:\n{issue_title or '(No title provided)'}\n\n"
        f"Issue Description:\n{issue_body or '(No description provided)'}\n\n"
        f"Worktree Path:\n{worktree_path or '.'}\n\n"
        f"Repository Files ({len(repo_files)} total):\n"
        f"{formatted_files}\n\n"
        "Please analyze the issue and codebase files, and return your diagnosis in the specified JSON format."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


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


def _match_repo_files(repo_files: Sequence[str], *text_sources: str) -> list[str]:
    """Find repository files that are referenced in any of the candidate text sources.

    Args:
        repo_files: Sequence of known repository file paths.
        text_sources: Text strings (issue title, body, freeform model text) to search.

    Returns:
        List of matching file paths.
    """
    if not repo_files:
        return []
    combined = " \n ".join(text_sources).strip()
    if not combined:
        return []

    combined_lower = combined.lower()
    matched: list[str] = []

    # 1. Exact path matches
    for path in repo_files:
        if path in combined or path.lower() in combined_lower:
            if path not in matched:
                matched.append(path)

    # 2. File basename matches if no exact path was found
    if not matched:
        for path in repo_files:
            basename = path.split("/")[-1]
            if basename and len(basename) > 4 and basename not in {"__init__.py", "setup.py"}:
                if basename.lower() in combined_lower:
                    if path not in matched:
                        matched.append(path)

    return matched


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


def _normalize_parsed_dict(
    data: dict[str, Any],
    repo_files: Sequence[str],
    issue_title: str,
    issue_body: str,
    raw_text: str,
) -> dict[str, Any]:
    """Ensure summary, relevant_files, and suggested_approach are normalized.

    Args:
        data: Parsed dictionary from JSON.
        repo_files: Sequence of known repository file paths.
        issue_title: Title of the issue.
        issue_body: Body of the issue.
        raw_text: Original raw text from provider.

    Returns:
        Dictionary with normalized 'summary', 'relevant_files', and 'suggested_approach'.
    """
    summary_raw = (
        data.get("summary")
        or data.get("problem_diagnosis")
        or data.get("diagnosis")
        or data.get("problem")
        or ""
    )
    if isinstance(summary_raw, list):
        summary = "\n".join(str(s) for s in summary_raw).strip()
    else:
        summary = str(summary_raw).strip()

    if not summary:
        summary = f"Issue: {issue_title}" if issue_title else "Issue investigation completed."

    approach_raw = (
        data.get("suggested_approach")
        or data.get("approach")
        or data.get("recommended_plan")
        or data.get("plan")
        or data.get("solution")
        or ""
    )
    if isinstance(approach_raw, list):
        approach = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(approach_raw)).strip()
    else:
        approach = str(approach_raw).strip()

    if not approach:
        approach = "Inspect relevant files and resolve issue according to specification."

    files_raw = (
        data.get("relevant_files")
        or data.get("files")
        or data.get("files_to_inspect")
        or []
    )
    relevant_files: list[str] = []
    if isinstance(files_raw, list):
        for item in files_raw:
            if isinstance(item, str):
                cleaned = item.strip().strip("`'\"")
                if cleaned and cleaned not in relevant_files:
                    relevant_files.append(cleaned)
            elif isinstance(item, dict) and "path" in item:
                path = str(item["path"]).strip()
                if path and path not in relevant_files:
                    relevant_files.append(path)
    elif isinstance(files_raw, str):
        for line in files_raw.splitlines():
            cleaned = line.strip().lstrip("-*1234567890. ").strip("`'\"")
            if cleaned and cleaned not in relevant_files:
                relevant_files.append(cleaned)

    if not relevant_files:
        relevant_files = _match_repo_files(repo_files, raw_text, issue_title, issue_body)

    return {
        "summary": summary,
        "relevant_files": relevant_files,
        "suggested_approach": approach,
    }


def _parse_freeform_text(
    text: str,
    repo_files: Sequence[str],
    issue_title: str,
    issue_body: str,
) -> dict[str, Any]:
    """Extract structured fields from freeform markdown or plain text.

    Args:
        text: Freeform text response from the model.
        repo_files: Sequence of known repository file paths.
        issue_title: Title of the issue.
        issue_body: Body of the issue.

    Returns:
        Dictionary with 'summary', 'relevant_files', and 'suggested_approach'.
    """
    summary_pat = re.compile(
        r"(?:^|\n)(?:#+\s*|\*\*|)(?:Summary|Problem(?:\s+Diagnosis)?|Diagnosis|Overview)(?:\*\*|:)?\s*\n?([\s\S]*?)(?=(?:\n(?:#+\s*|\*\*|)(?:Relevant\s+Files?|Files(?:\s+to\s+inspect)?|Suggested\s+Approach|Approach|Plan|Solution))|\Z)",
        re.IGNORECASE,
    )
    files_pat = re.compile(
        r"(?:^|\n)(?:#+\s*|\*\*|)(?:Relevant\s+Files?|Files(?:\s+to\s+inspect)?|Affected\s+Files?)(?:\*\*|:)?\s*\n?([\s\S]*?)(?=(?:\n(?:#+\s*|\*\*|)(?:Summary|Diagnosis|Suggested\s+Approach|Approach|Plan|Solution))|\Z)",
        re.IGNORECASE,
    )
    approach_pat = re.compile(
        r"(?:^|\n)(?:#+\s*|\*\*|)(?:Suggested\s+Approach|Recommended\s+Plan|Approach|Plan|Solution|Proposed\s+Changes)(?:\*\*|:)?\s*\n?([\s\S]*?)(?=(?:\n(?:#+\s*|\*\*|)(?:Summary|Diagnosis|Relevant\s+Files?|Files))|\Z)",
        re.IGNORECASE,
    )

    summary_match = summary_pat.search(text)
    files_match = files_pat.search(text)
    approach_match = approach_pat.search(text)

    summary = summary_match.group(1).strip() if summary_match else ""
    raw_files_text = files_match.group(1).strip() if files_match else ""
    approach = approach_match.group(1).strip() if approach_match else ""

    relevant_files: list[str] = []
    if raw_files_text:
        for line in raw_files_text.splitlines():
            cleaned = line.strip().lstrip("-*1234567890. ").strip("`'\"")
            if cleaned:
                matching = [rf for rf in repo_files if rf == cleaned or rf.endswith(f"/{cleaned}")]
                if matching:
                    for m in matching:
                        if m not in relevant_files:
                            relevant_files.append(m)
                elif "/" in cleaned or "." in cleaned:
                    if cleaned not in relevant_files:
                        relevant_files.append(cleaned)

    if not relevant_files:
        relevant_files = _match_repo_files(repo_files, text, issue_title, issue_body)

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not summary:
        if paragraphs:
            summary = paragraphs[0]
        elif issue_title:
            summary = f"Issue: {issue_title}"
        else:
            summary = "Investigation completed."

    if not approach:
        if len(paragraphs) > 1:
            approach = "\n\n".join(paragraphs[1:]).strip()
        elif text.strip() and text.strip() != summary:
            approach = text.strip()
        else:
            approach = (
                f"Inspect the relevant files ({', '.join(relevant_files) if relevant_files else 'repository files'}) "
                "and apply changes required by the issue description."
            )

    return {
        "summary": summary,
        "relevant_files": relevant_files,
        "suggested_approach": approach,
    }


class InvestigatorAgent:
    """Agent responsible for analyzing an issue against a repository and planning fixes."""

    def __init__(self, provider: Any) -> None:
        """Initialize the InvestigatorAgent with a model provider.

        Args:
            provider: A model provider instance conforming to shipwright.providers.Provider.
        """
        self.provider = provider

    def investigate(
        self,
        repo_files: list[str],
        issue: Any,
        worktree_path: str,
    ) -> dict[str, Any]:
        """Investigate an issue and produce diagnosis, target files, and a plan.

        Args:
            repo_files: List of file paths relative to the repository worktree root.
            issue: The issue object, dictionary, or text string.
            worktree_path: Absolute path to the isolated worktree directory.

        Returns:
            A dictionary containing:
                - 'summary': brief problem diagnosis
                - 'relevant_files': list of files to inspect or modify
                - 'suggested_approach': recommended plan
        """
        issue_title, issue_body = _extract_issue_info(issue)

        messages = _build_investigation_messages(
            repo_files=repo_files,
            issue_title=issue_title,
            issue_body=issue_body,
            worktree_path=worktree_path,
        )

        try:
            response = self.provider.complete(messages=messages)
            raw_text = _extract_response_text(response)
        except Exception as exc:
            logger.warning("Provider complete call failed during investigation: %s", exc)
            fallback_files = _match_repo_files(repo_files, issue_title, issue_body)
            return {
                "summary": (
                    f"Investigation provider failed ({exc}). "
                    f"Issue: {issue_title or 'Untitled issue'}"
                ),
                "relevant_files": fallback_files,
                "suggested_approach": (
                    f"Manually review issue '{issue_title or 'Untitled'}' and inspect candidate files: "
                    f"{', '.join(fallback_files) if fallback_files else 'repository files'}."
                ),
            }

        # Attempt structured JSON extraction first
        json_data = _extract_json_payload(raw_text)
        if json_data is not None:
            return _normalize_parsed_dict(
                data=json_data,
                repo_files=repo_files,
                issue_title=issue_title,
                issue_body=issue_body,
                raw_text=raw_text,
            )

        # Fallback to freeform text parsing
        return _parse_freeform_text(
            text=raw_text,
            repo_files=repo_files,
            issue_title=issue_title,
            issue_body=issue_body,
        )
