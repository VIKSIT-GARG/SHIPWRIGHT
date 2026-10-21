"""Implementer agent for generating and applying code modifications in an isolated worktree."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from shipwright.providers import Provider, ProviderError, ProviderResponse

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
    if isinstance(issue, str):
        lines = issue.strip().splitlines()
        if not lines:
            return "", ""
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        return title, body
    if isinstance(issue, Mapping):
        title = str(issue.get("title", "") or "").strip()
        body = str(issue.get("body", "") or issue.get("description", "") or "").strip()
        number = issue.get("number")
        if number and title and not title.startswith(f"#{number}"):
            title = f"#{number}: {title}"
        return title, body

    title = ""
    body = ""
    if hasattr(issue, "title") and not callable(getattr(issue, "title")):
        title = str(getattr(issue, "title") or "").strip()
    if hasattr(issue, "body") and not callable(getattr(issue, "body")):
        body = str(getattr(issue, "body") or "").strip()
    elif hasattr(issue, "description") and not callable(getattr(issue, "description")):
        body = str(getattr(issue, "description") or "").strip()

    number = getattr(issue, "number", None)
    if number and not callable(number) and title and not str(title).startswith(f"#{number}"):
        title = f"#{number}: {title}"

    if title or body:
        return title, body

    return str(issue).strip(), ""


def _extract_response_text(response: Any) -> str:
    """Safely extract generated text from a provider response object or string.

    Args:
        response: Response from a Provider, ProviderResponse, mapping, or string.

    Returns:
        Generated text string.
    """
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


def _extract_relevant_file_paths(investigation: dict[str, Any]) -> list[str]:
    """Extract list of target file paths from an investigation dictionary.

    Args:
        investigation: Investigation report dictionary.

    Returns:
        List of relative file path strings.
    """
    candidate_keys = (
        "relevant_files",
        "files",
        "target_files",
        "files_to_modify",
        "modified_files",
        "patch_files",
    )
    raw: Any = None
    for key in candidate_keys:
        if key in investigation and investigation[key]:
            raw = investigation[key]
            break

    paths: list[str] = []
    if raw is None:
        return paths

    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                clean = item.strip().strip("`'\"")
                if clean and clean not in paths:
                    paths.append(clean)
            elif isinstance(item, Mapping) and "path" in item:
                clean = str(item["path"]).strip().strip("`'\"")
                if clean and clean not in paths:
                    paths.append(clean)
            elif isinstance(item, Path):
                clean = str(item).strip()
                if clean and clean not in paths:
                    paths.append(clean)
    elif isinstance(raw, Mapping):
        for k in raw:
            clean = str(k).strip().strip("`'\"")
            if clean and clean not in paths:
                paths.append(clean)
    elif isinstance(raw, str):
        for line in raw.splitlines():
            clean = line.strip().lstrip("-*1234567890. ").strip("`'\"")
            if clean and clean not in paths:
                paths.append(clean)
    return paths


def _read_worktree_files(worktree_dir: Path, relative_paths: Sequence[str]) -> dict[str, str]:
    """Read contents of specified files in the worktree directory.

    Args:
        worktree_dir: Base directory of the repository worktree.
        relative_paths: Sequence of relative file paths to read.

    Returns:
        Mapping of relative path to file content string.
    """
    contents: dict[str, str] = {}
    for rel_path in relative_paths:
        clean = rel_path.strip().lstrip("/\\")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        try:
            target = (worktree_dir / clean).resolve()
            if not target.is_relative_to(worktree_dir):
                logger.warning("Skipping file outside worktree: %s", rel_path)
                continue
            if target.is_file():
                try:
                    contents[clean] = target.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    contents[clean] = f"[Error reading file: {exc}]"
            else:
                contents[clean] = "[File does not exist yet - new file]"
        except Exception as exc:
            logger.warning("Error resolving path %s: %s", rel_path, exc)
    return contents


def _extract_json_files(text: str) -> dict[str, str] | None:
    """Attempt to extract file modifications from JSON formatting in model output.

    Args:
        text: Model response text.

    Returns:
        Mapping of relative path to content, or None if no valid JSON file dict found.
    """
    candidates: list[str] = []

    # 1. Search for fenced ```json ... ``` or ``` ... ``` blocks
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE):
        candidate = m.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            candidates.append(candidate)

    # 2. Check entire stripped text
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)

    # 3. Outer braces { ... } or brackets [ ... ]
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(stripped[first_brace : last_brace + 1])

    first_bracket = stripped.find("[")
    last_bracket = stripped.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(stripped[first_bracket : last_bracket + 1])

    for cand in candidates:
        try:
            data = json.loads(cand, strict=False)
            files_dict: dict[str, str] = {}

            if isinstance(data, Mapping):
                if "files" in data:
                    raw_files = data["files"]
                    if isinstance(raw_files, list):
                        for item in raw_files:
                            if isinstance(item, Mapping):
                                p = item.get("path") or item.get("file") or item.get("filename")
                                c = item.get("content") if "content" in item else item.get("code")
                                if p and isinstance(c, str):
                                    files_dict[str(p).strip()] = c
                    elif isinstance(raw_files, Mapping):
                        for p, c in raw_files.items():
                            if isinstance(c, str):
                                files_dict[str(p).strip()] = c
                else:
                    for k, v in data.items():
                        if isinstance(k, str) and isinstance(v, str) and ("." in k or "/" in k):
                            files_dict[k.strip()] = v

            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, Mapping):
                        p = item.get("path") or item.get("file") or item.get("filename")
                        c = item.get("content") if "content" in item else item.get("code")
                        if p and isinstance(c, str):
                            files_dict[str(p).strip()] = c

            if files_dict:
                return files_dict
        except Exception:
            continue

    return None


def _extract_markdown_codeblocks(text: str) -> dict[str, str]:
    """Extract file modifications from markdown code blocks with file headers or comments.

    Args:
        text: Model response text.

    Returns:
        Mapping of relative path to content.
    """
    results: dict[str, str] = {}
    code_block_pattern = re.compile(r"```([^\n]*)\n([\s\S]*?)```")

    last_end = 0
    for match in code_block_pattern.finditer(text):
        preceding = text[last_end : match.start()].strip()
        last_end = match.end()
        info = (match.group(1) or "").strip()
        body = match.group(2)

        filepath: str | None = None

        # 1. Info line inspection: e.g. python:src/foo.py or FILE: src/foo.py or file="src/foo.py"
        info_m = re.search(
            r"(?:file(?:path)?[:=]|FILE:\s*|\bfile=[\"']?)([\w\-\./\\]+)",
            info,
            re.IGNORECASE,
        )
        if info_m:
            filepath = info_m.group(1).strip("\"'`")
        elif ":" in info and not info.startswith("http"):
            parts = info.split(":", 1)
            candidate = parts[1].strip()
            if candidate and ("." in candidate or "/" in candidate):
                filepath = candidate

        # 2. First line of body inspection: e.g. # FILE: src/foo.py
        if not filepath and body:
            first_line, _, rest = body.partition("\n")
            first_line_clean = first_line.strip()
            body_m = re.match(
                r"^(?:#|//|/\*|--|;)\s*(?:FILE(?:PATH)?:\s*|file:\s*)([\w\-\./\\]+)",
                first_line_clean,
                re.IGNORECASE,
            )
            if body_m:
                filepath = body_m.group(1).strip("\"'`* ")
                body = rest
            else:
                simple_m = re.match(
                    r"^(?:#|//)\s*([\w\-\./\\]+\.[a-zA-Z0-9]+)\s*$",
                    first_line_clean,
                )
                if simple_m:
                    filepath = simple_m.group(1).strip()
                    body = rest

        # 3. Preceding text inspection
        if not filepath and preceding:
            lines = [line.strip() for line in preceding.splitlines() if line.strip()]
            for line in reversed(lines[-5:]):
                prec_m = re.search(
                    r"(?:FILE|File|filepath):\s*[`\"']?([\w\-\./\\]+)[`\"']?",
                    line,
                    re.IGNORECASE,
                )
                if prec_m:
                    filepath = prec_m.group(1)
                    break
                header_m = re.search(
                    r"^(?:#{1,6}\s+|\*{2}|`)([\w\-\./\\]+\.[a-zA-Z0-9]+)",
                    line,
                )
                if header_m:
                    filepath = header_m.group(1).strip("`*")
                    break

        if filepath:
            results[filepath] = body

    return results


def _extract_plain_file_blocks(text: str) -> dict[str, str]:
    """Extract files from text with plain 'FILE: <path>' line markers.

    Args:
        text: Model response text.

    Returns:
        Mapping of relative path to content.
    """
    results: dict[str, str] = {}
    plain_pattern = re.compile(r"(?:^|\n)FILE:\s*([^\n]+)\n", re.IGNORECASE)
    splits = list(plain_pattern.finditer(text))
    if not splits:
        return results

    for i, sp in enumerate(splits):
        filepath = sp.group(1).strip().strip("\"'`")
        start = sp.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        content = text[start:end].strip()
        if content.startswith("```") and content.endswith("```"):
            content = re.sub(r"^```[^\n]*\n", "", content)
            content = re.sub(r"\n```$", "", content)
        results[filepath] = content

    return results


def parse_file_modifications(text: str) -> dict[str, str]:
    """Parse file modifications from model response (JSON, markdown blocks, or plain FILE headers).

    Args:
        text: Model generated text.

    Returns:
        Dictionary mapping relative file path to file content.
    """
    # 1. Try structured JSON extraction
    json_result = _extract_json_files(text)
    if json_result:
        return json_result

    # 2. Try markdown code block extraction
    md_result = _extract_markdown_codeblocks(text)
    if md_result:
        return md_result

    # 3. Fallback to plain FILE marker parsing
    return _extract_plain_file_blocks(text)


def _build_implementer_messages(
    issue_title: str,
    issue_body: str,
    investigation: Mapping[str, Any],
    file_contents: Mapping[str, str],
    previous_error: str | None = None,
) -> list[dict[str, str]]:
    """Build chat messages sent to the model provider for implementation.

    Args:
        issue_title: Title of the issue.
        issue_body: Description or body of the issue.
        investigation: Investigation diagnosis and recommendations.
        file_contents: Mapping of existing file paths to their contents.
        previous_error: Optional error/test failure output from prior attempt.

    Returns:
        List of chat messages formatted for Provider.complete().
    """
    summary = ""
    approach = ""
    if isinstance(investigation, Mapping):
        summary = str(
            investigation.get("summary")
            or investigation.get("problem_diagnosis")
            or investigation.get("diagnosis")
            or investigation.get("problem")
            or ""
        ).strip()
        approach = str(
            investigation.get("suggested_approach")
            or investigation.get("approach")
            or investigation.get("recommended_plan")
            or investigation.get("plan")
            or investigation.get("solution")
            or ""
        ).strip()

    system_prompt = (
        "You are Shipwright's autonomous implementer agent.\n"
        "Your task is to implement code changes in the repository to resolve the issue based on the investigation findings and existing code.\n\n"
        "Requirements:\n"
        "1. Provide the complete content for each file to be modified or created. Do not use partial diffs, placeholders, or ellipsis (...).\n"
        "2. Format your response using one of the following formats:\n\n"
        "Format A (Markdown code blocks with a FILE line before each block):\n"
        "FILE: path/to/file.py\n"
        "```python\n"
        "<full file content>\n"
        "```\n\n"
        "Format B (Markdown code block with a FILE comment on the first line):\n"
        "```python\n"
        "# FILE: path/to/file.py\n"
        "<full file content>\n"
        "```\n\n"
        "Format C (JSON object):\n"
        "```json\n"
        "{\n"
        '  "files": [\n'
        "    {\n"
        '      "path": "path/to/file.py",\n'
        '      "content": "<full file content>"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n"
    )

    user_parts: list[str] = []

    # Issue section
    issue_header = f"Issue Title: {issue_title}" if issue_title else "Issue: (Untitled)"
    issue_desc = f"Issue Description:\n{issue_body}" if issue_body else "Issue Description: (None provided)"
    user_parts.append(f"# Issue Details\n{issue_header}\n\n{issue_desc}")

    # Investigation section
    inv_parts: list[str] = []
    if summary:
        inv_parts.append(f"Summary / Diagnosis:\n{summary}")
    if approach:
        inv_parts.append(f"Suggested Plan / Approach:\n{approach}")
    if not inv_parts and investigation:
        inv_parts.append(f"Investigation Details:\n{json.dumps(dict(investigation), indent=2)}")
    if inv_parts:
        user_parts.append("# Investigation Findings\n" + "\n\n".join(inv_parts))

    # Existing Files section
    if file_contents:
        files_text = []
        for file_path, content in file_contents.items():
            files_text.append(f"--- FILE: {file_path} ---\n{content}\n--- END FILE ---")
        user_parts.append("# Existing Repository Files\n" + "\n\n".join(files_text))
    else:
        user_parts.append(
            "# Existing Repository Files\n(No specific existing files identified. Create or modify files as required.)"
        )

    # Previous error / Repair loop section
    if previous_error and str(previous_error).strip():
        user_parts.append(
            "# Previous Attempt Verification Failure (Repair Loop)\n"
            "The previous implementation attempt resulted in the following test/verification failure:\n"
            "```\n"
            f"{str(previous_error).strip()}\n"
            "```\n"
            "Please carefully analyze this failure evidence and adjust your implementation to fix the failing tests."
        )

    user_parts.append(
        "Please provide the complete updated or new file contents in one of the accepted formats."
    )

    user_prompt = "\n\n".join(user_parts)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


class ImplementerAgent:
    """Agent responsible for implementing code changes based on investigation findings."""

    def __init__(self, provider: Any) -> None:
        """Initialize the ImplementerAgent with a model provider.

        Args:
            provider: A model provider conforming to shipwright.providers.Provider.
        """
        self.provider = provider
        self.last_error: str | None = None

    def implement(
        self,
        investigation: dict[str, Any],
        issue: Any,
        worktree_path: str,
        previous_error: str | None = None,
    ) -> list[str]:
        """Read relevant files, prompt the model for modifications, and apply them.

        Args:
            investigation: Diagnosis dictionary from the investigator agent.
            issue: The issue object, mapping, or string description.
            worktree_path: Path to the isolated repository worktree directory.
            previous_error: Optional error or test failure output from a previous attempt.

        Returns:
            List of relative file paths that were modified or created.
        """
        self.last_error = None

        if not worktree_path:
            self.last_error = "worktree_path must not be empty"
            logger.error(self.last_error)
            return []

        try:
            worktree_dir = Path(worktree_path).resolve()
            worktree_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.last_error = f"Failed to initialize worktree directory '{worktree_path}': {exc}"
            logger.error(self.last_error)
            return []

        # 1. Read relevant files from worktree
        relevant_paths = _extract_relevant_file_paths(investigation if isinstance(investigation, Mapping) else {})
        file_contents = _read_worktree_files(worktree_dir, relevant_paths)

        # 2. Extract issue info and build prompt messages
        issue_title, issue_body = _extract_issue_info(issue)
        messages = _build_implementer_messages(
            issue_title=issue_title,
            issue_body=issue_body,
            investigation=investigation if isinstance(investigation, Mapping) else {},
            file_contents=file_contents,
            previous_error=previous_error,
        )

        # 3. Call model provider
        try:
            response = self.provider.complete(messages=messages)
            raw_text = _extract_response_text(response)
        except Exception as exc:
            self.last_error = f"Provider completion failed: {exc}"
            logger.warning("Implementer complete call failed: %s", exc)
            return []

        # 4. Parse model output into file modifications
        modifications = parse_file_modifications(raw_text)
        if not modifications:
            logger.warning("No file modifications found in provider response")
            return []

        # 5. Write updated contents to the corresponding files in worktree
        modified_files: list[str] = []
        for rel_path, content in modifications.items():
            try:
                clean_path = rel_path.strip().strip("\"'`").lstrip("/\\")
                if clean_path.startswith("./"):
                    clean_path = clean_path[2:]
                if not clean_path:
                    continue

                target_path = (worktree_dir / clean_path).resolve()
                if not target_path.is_relative_to(worktree_dir):
                    logger.warning("Security: skipping path outside worktree: %s", rel_path)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)

                normalized_content = content.rstrip("\r\n")
                if normalized_content:
                    normalized_content += "\n"

                target_path.write_text(normalized_content, encoding="utf-8")

                rel_str = target_path.relative_to(worktree_dir).as_posix()
                if rel_str not in modified_files:
                    modified_files.append(rel_str)
            except Exception as exc:
                logger.warning("Failed to write modified file '%s': %s", rel_path, exc)

        return modified_files
