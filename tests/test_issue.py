"""Unit tests for shipwright.issue loader functions."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

from shipwright.issue import (
    load_issue,
    load_issue_from_file,
    load_issue_from_number,
    load_issue_from_text,
    load_issue_from_url,
)
from shipwright.models import Issue


def test_load_issue_from_int_fallback() -> None:
    # When gh fails or issue doesn't exist, returns fallback Issue
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="not found"
        )
        issue = load_issue(42)
        assert issue.number == 42
        assert issue.title == "Issue #42"
        assert issue.body == "Work on issue #42"
        assert issue.source == "manual"


def test_load_issue_from_string_number_fallback() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="not found"
        )
        issue_1 = load_issue("42")
        assert issue_1.number == 42
        assert issue_1.title == "Issue #42"

        issue_2 = load_issue("#99")
        assert issue_2.number == 99
        assert issue_2.title == "Issue #99"


def test_load_issue_from_number_gh_success() -> None:
    fake_json = {
        "number": 100,
        "title": "Fix memory leak in buffer",
        "body": "Buffer is never freed after processing.",
        "labels": [{"name": "bug"}, {"name": "priority"}],
        "url": "https://github.com/org/repo/issues/100",
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps(fake_json), stderr=""
        )
        issue = load_issue_from_number(100)
        assert issue.number == 100
        assert issue.title == "Fix memory leak in buffer"
        assert issue.body == "Buffer is never freed after processing."
        assert issue.labels == ["bug", "priority"]
        assert issue.source == "github"
        assert issue.url == "https://github.com/org/repo/issues/100"


def test_load_issue_from_url_gh_success() -> None:
    fake_json = {
        "number": 55,
        "title": "Add retry support",
        "body": "Support exponential backoff.",
        "labels": ["enhancement"],
        "url": "https://github.com/org/repo/issues/55",
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps(fake_json), stderr=""
        )
        issue = load_issue("https://github.com/org/repo/issues/55")
        assert issue.number == 55
        assert issue.title == "Add retry support"
        assert issue.body == "Support exponential backoff."
        assert issue.labels == ["enhancement"]
        assert issue.url == "https://github.com/org/repo/issues/55"
        assert issue.source == "github"


def test_load_issue_from_url_fetch_fallback() -> None:
    # When gh fails and urllib fails, should return fallback Issue
    with patch("subprocess.run") as mock_run, patch("urllib.request.urlopen") as mock_urlopen:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="failed"
        )
        mock_urlopen.side_effect = Exception("network unreachable")

        url = "https://github.com/example/project/issues/77"
        issue = load_issue(url)
        assert issue.number == 77
        assert issue.title == "Issue #77"
        assert issue.body == "Work on issue #77"
        assert issue.source == "github"
        assert issue.url == url


def test_load_issue_from_url_fetch_success() -> None:
    fake_api_response = {
        "number": 88,
        "title": "REST API Issue",
        "body": "Fetched via GitHub REST API.",
        "labels": [{"name": "api"}],
        "html_url": "https://github.com/example/project/issues/88",
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(fake_api_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("subprocess.run") as mock_run, patch("urllib.request.urlopen", return_value=mock_resp):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="gh unavailable"
        )
        issue = load_issue("https://github.com/example/project/issues/88")
        assert issue.number == 88
        assert issue.title == "REST API Issue"
        assert issue.body == "Fetched via GitHub REST API."
        assert issue.labels == ["api"]
        assert issue.url == "https://github.com/example/project/issues/88"


def test_load_issue_from_markdown_file_with_header(tmp_path: Path) -> None:
    file = tmp_path / "issue-33.md"
    file.write_text("# Support dark mode\n\nPlease add dark mode to the TUI.\n", encoding="utf-8")

    issue = load_issue(str(file))
    assert issue.number == 33
    assert issue.title == "Support dark mode"
    assert issue.body == "# Support dark mode\n\nPlease add dark mode to the TUI.\n"


def test_load_issue_from_markdown_file_without_header(tmp_path: Path) -> None:
    file = tmp_path / "bug.txt"
    file.write_text("Database connection timed out after 30 seconds.\nDetails...", encoding="utf-8")

    issue = load_issue(str(file))
    assert issue.number == 1
    assert issue.title == "Database connection timed out after 30 seconds."
    assert "Database connection timed out after 30 seconds." in issue.body


def test_load_issue_from_empty_file(tmp_path: Path) -> None:
    file = tmp_path / "empty_report.txt"
    file.write_text("", encoding="utf-8")

    issue = load_issue(str(file))
    assert issue.number == 1
    assert issue.title == "empty_report"
    assert issue.body == ""


def test_load_issue_from_json_file(tmp_path: Path) -> None:
    file = tmp_path / "issue.json"
    file.write_text(
        json.dumps(
            {
                "number": 512,
                "title": "Fix JSON parsing error",
                "body": "Special characters cause crash.",
                "labels": ["bug", "critical"],
                "url": "https://example.com/issue/512",
            }
        ),
        encoding="utf-8",
    )

    issue = load_issue(str(file))
    assert issue.number == 512
    assert issue.title == "Fix JSON parsing error"
    assert issue.body == "Special characters cause crash."
    assert issue.labels == ["bug", "critical"]
    assert issue.url == "https://example.com/issue/512"


def test_load_issue_relative_to_repo_path(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    issue_file = repo_dir / "task.md"
    issue_file.write_text("Implement feature X\nMore info.", encoding="utf-8")

    issue = load_issue("task.md", repo_path=str(repo_dir))
    assert issue.title == "Implement feature X"


def test_load_issue_from_raw_text() -> None:
    text = "Fix the login bug where session expires prematurely after 5 minutes"
    issue = load_issue(text)
    assert issue.number == 1
    assert issue.title == text[:60]
    assert issue.body == text


def test_load_issue_from_short_raw_text() -> None:
    text = "Crash on start"
    issue = load_issue(text)
    assert issue.number == 1
    assert issue.title == "Crash on start"
    assert issue.body == "Crash on start"
