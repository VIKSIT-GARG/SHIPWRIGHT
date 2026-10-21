"""Issue loaders for Shipwright supporting GitHub CLI, URLs, files, and text."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
import urllib.error
import urllib.request

from shipwright.models import Issue

__all__ = [
    "load_issue",
    "load_issue_from_file",
    "load_issue_from_number",
    "load_issue_from_text",
    "load_issue_from_url",
]

_GITHUB_ISSUE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<number>\d+)"
)


def _parse_labels(raw: Any) -> list[str]:
    """Normalize label entries from GitHub API/CLI responses into string names."""
    labels: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                labels.append(str(item["name"]))
            elif isinstance(item, str):
                labels.append(item)
    return labels


def _extract_title_from_text(content: str, path: Path) -> str:
    """Extract a title from the first non-empty line or fall back to the filename stem."""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line:
            clean = line.lstrip("#").strip()
            if clean:
                return clean
    return path.stem


def _extract_number_from_path_or_title(path: Path, title: str) -> int:
    """Infer an issue number from the file stem or title, defaulting to 1."""
    match_path = re.search(r"(\d+)", path.stem)
    if match_path:
        try:
            return int(match_path.group(1))
        except ValueError:
            pass
    match_title = re.match(r"(?:Issue\s*#?\s*)(\d+)", title, re.IGNORECASE)
    if match_title:
        try:
            return int(match_title.group(1))
        except ValueError:
            pass
    return 1


def _resolve_file_path(source: str, repo_path: str = ".") -> Path | None:
    """Return an existing Path if source corresponds to a local file, otherwise None."""
    if "\n" in source or "\r" in source or "\x00" in source:
        return None
    if len(source) > 4096:
        return None
    try:
        candidate = Path(source)
        if candidate.is_file():
            return candidate

        candidate_repo = Path(repo_path) / source
        if candidate_repo.is_file():
            return candidate_repo

        candidate_exp = candidate.expanduser()
        if candidate_exp.is_file():
            return candidate_exp
    except (OSError, ValueError):
        return None
    return None


def load_issue_from_number(num: int, repo_path: str = ".") -> Issue:
    """Load an issue by number using gh CLI with fallback to a synthesized Issue."""
    cmd = [
        "gh",
        "issue",
        "view",
        str(num),
        "--json",
        "number,title,body,labels,url",
    ]
    cwd = Path(repo_path).resolve() if Path(repo_path).is_dir() else None
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_stdout = proc.stdout.strip()
            json_start = raw_stdout.find("{")
            data = json.loads(raw_stdout[json_start:] if json_start != -1 else raw_stdout)
            if isinstance(data, dict):
                labels = _parse_labels(data.get("labels"))
                return Issue(
                    number=int(data.get("number", num)),
                    title=str(data.get("title") or f"Issue #{num}"),
                    body=str(data.get("body") or ""),
                    source="github",
                    labels=labels,
                    url=str(data.get("url") or ""),
                )
    except Exception:
        pass

    return Issue(
        number=int(num),
        title=f"Issue #{num}",
        body=f"Work on issue #{num}",
    )


def load_issue_from_url(url: str, repo_path: str = ".") -> Issue:
    """Load an issue from a GitHub URL via gh CLI, REST API fetch, or fallback."""
    owner = ""
    repo = ""
    num = 1

    match = _GITHUB_ISSUE_URL_RE.search(url)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
        num = int(match.group("number"))
    else:
        fallback_match = re.search(r"/issues/(\d+)", url)
        if fallback_match:
            num = int(fallback_match.group(1))

    # 1. Try gh CLI
    cmd = [
        "gh",
        "issue",
        "view",
        url,
        "--json",
        "number,title,body,labels,url",
    ]
    cwd = Path(repo_path).resolve() if Path(repo_path).is_dir() else None
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_stdout = proc.stdout.strip()
            json_start = raw_stdout.find("{")
            data = json.loads(raw_stdout[json_start:] if json_start != -1 else raw_stdout)
            if isinstance(data, dict):
                labels = _parse_labels(data.get("labels"))
                return Issue(
                    number=int(data.get("number", num)),
                    title=str(data.get("title") or f"Issue #{num}"),
                    body=str(data.get("body") or ""),
                    source="github",
                    labels=labels,
                    url=str(data.get("url") or url),
                )
    except Exception:
        pass

    # 2. Try HTTP fetch via GitHub REST API
    if owner and repo:
        try:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
            req = urllib.request.Request(
                api_url,
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Shipwright-Loader",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict):
                        labels = _parse_labels(data.get("labels"))
                        return Issue(
                            number=int(data.get("number", num)),
                            title=str(data.get("title") or f"Issue #{num}"),
                            body=str(data.get("body") or ""),
                            source="github",
                            labels=labels,
                            url=str(data.get("html_url") or url),
                        )
        except Exception:
            pass

    # 3. Fallback
    return Issue(
        number=int(num),
        title=f"Issue #{num}",
        body=f"Work on issue #{num}",
        source="github",
        url=url,
    )


def load_issue_from_file(file_path: Path | str) -> Issue:
    """Load an issue definition from a local .md, .txt, or .json file."""
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                raw_num = data.get("number")
                num = (
                    int(raw_num)
                    if isinstance(raw_num, (int, str)) and str(raw_num).isdigit()
                    else _extract_number_from_path_or_title(path, "")
                )
                title = str(data.get("title") or _extract_title_from_text(content, path))
                body = str(data.get("body") if data.get("body") is not None else content)
                labels = _parse_labels(data.get("labels"))
                url = str(data.get("url") or "")
                source_val = str(data.get("source") or "manual")
                return Issue(
                    number=num,
                    title=title,
                    body=body,
                    source=source_val,
                    labels=labels,
                    url=url,
                )
        except json.JSONDecodeError:
            pass

    title = _extract_title_from_text(content, path)
    num = _extract_number_from_path_or_title(path, title)
    return Issue(
        number=num,
        title=title,
        body=content,
    )


def load_issue_from_text(text: str) -> Issue:
    """Create an issue directly from raw text input."""
    return Issue(
        number=1,
        title=text[:60],
        body=text,
    )


def load_issue(source: str | int, repo_path: str = ".") -> Issue:
    """Load an issue from a number, GitHub URL, local file, or raw text.

    Parameters:
        source: Issue number (int or '#123'), GitHub URL, file path, or raw prompt string.
        repo_path: Path to the target git repository (used for `gh` context and relative files).

    Returns:
        Issue domain model populated from the resolved source.
    """
    if isinstance(source, int):
        return load_issue_from_number(source, repo_path=repo_path)

    if isinstance(source, Path):
        return load_issue_from_file(source)

    if not isinstance(source, str):
        source = str(source)

    cleaned = source.strip()

    # 1. Integer or string of digits (e.g. 123 or "#123")
    num_str = cleaned[1:].strip() if cleaned.startswith("#") else cleaned
    if num_str.isdigit():
        return load_issue_from_number(int(num_str), repo_path=repo_path)

    # 2. URL (github.com/.../issues/123)
    if "github.com/" in cleaned and "/issues/" in cleaned:
        return load_issue_from_url(cleaned, repo_path=repo_path)

    # 3. File path that exists (.md, .txt, .json, etc.)
    resolved_file = _resolve_file_path(cleaned, repo_path=repo_path)
    if resolved_file is not None:
        return load_issue_from_file(resolved_file)

    # 4. Raw text
    return load_issue_from_text(source)
