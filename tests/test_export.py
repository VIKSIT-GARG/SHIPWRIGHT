"""Tests for shipwright.export module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shipwright.export import create_draft_pr, export_bundle
from shipwright.models import (
    Artifact,
    Issue,
    Review,
    Run,
    RunState,
    TimelineEvent,
    fixture_runs,
)


def test_export_bundle_fixture_run(tmp_path: Path) -> None:
    runs = fixture_runs()
    run = runs[0]

    out_dir = tmp_path / "bundle_1"
    returned_path = export_bundle(run, out_dir)

    assert returned_path == out_dir
    assert out_dir.is_dir()

    # Check 5 required files
    patch_file = out_dir / "patch.diff"
    json_file = out_dir / "run.json"
    events_file = out_dir / "events.log"
    test_file = out_dir / "test_output.txt"
    review_file = out_dir / "review.md"

    assert patch_file.exists()
    assert json_file.exists()
    assert events_file.exists()
    assert test_file.exists()
    assert review_file.exists()

    # Verify run.json content
    meta = json.loads(json_file.read_text(encoding="utf-8"))
    assert meta["number"] == 1842
    assert meta["title"] == "Fix flaky retry test"
    assert meta["status"] == "VERIFYING"
    assert meta["state"] == "VERIFYING"
    assert meta["attempts"] == 0
    assert meta["elapsed"] == "02:14"
    assert "metadata" in meta
    assert meta["metadata"]["title"] == "Fix flaky retry test"

    # Verify events.log
    events_content = events_file.read_text(encoding="utf-8")
    assert "investigator" in events_content
    assert "identified src/retry.py" in events_content
    assert "verifier" in events_content

    # Verify test_output.txt
    test_content = test_file.read_text(encoding="utf-8")
    assert "42 passed" in test_content

    # Verify review.md
    review_content = review_file.read_text(encoding="utf-8")
    assert "# Review Report" in review_content


def test_export_bundle_with_rich_run(tmp_path: Path) -> None:
    run = Run(
        number=42,
        title="Add robust retry",
        state=RunState.READY_FOR_APPROVAL,
        elapsed="05:22",
        agents="4 / 4",
        tests="10 passed",
        attempt=2,
        max_attempts=3,
        branch="shipwright/issue-42",
        issue=Issue(
            number=42,
            title="Retry logic needed",
            body="Implement exponential backoff",
            source="github",
            labels=["enhancement"],
            url="https://github.com/org/repo/issues/42",
        ),
        review=Review(
            passed=True,
            confidence=0.98,
            summary="All tests pass and edge cases handled.",
            findings=["Cleaned up logging", "Added backoff test"],
        ),
        events=[
            TimelineEvent("10:00:00", "planner", "Plan generated"),
            TimelineEvent("10:01:00", "implementer", "Patch applied"),
        ],
        artifacts=[
            Artifact(name="diff.patch", path="diff.patch", content="diff --git a/test b/test\n+line"),
            Artifact(name="test.log", path="test.log", content="PASSED 10 tests in 0.4s"),
        ],
    )

    out_dir = tmp_path / "bundle_rich"
    export_bundle(run, out_dir)

    # Check patch.diff has artifact content
    diff_text = (out_dir / "patch.diff").read_text(encoding="utf-8")
    assert "diff --git a/test b/test" in diff_text

    # Check test_output.txt has artifact content
    test_text = (out_dir / "test_output.txt").read_text(encoding="utf-8")
    assert "PASSED 10 tests in 0.4s" in test_text

    # Check review.md has summary and findings
    review_text = (out_dir / "review.md").read_text(encoding="utf-8")
    assert "All tests pass and edge cases handled." in review_text
    assert "Cleaned up logging" in review_text
    assert "Added backoff test" in review_text
    assert "PASSED" in review_text
    assert "0.98" in review_text

    # Check run.json
    run_json = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    assert run_json["number"] == 42
    assert run_json["status"] == "READY_FOR_APPROVAL"
    assert run_json["attempts"] == 2
    assert run_json["elapsed"] == "05:22"
    assert run_json["issue"]["url"] == "https://github.com/org/repo/issues/42"
    assert run_json["review"]["confidence"] == 0.98


def test_export_bundle_from_dict(tmp_path: Path) -> None:
    data = {
        "number": 99,
        "title": "Dict run",
        "state": "EXPORTED",
        "elapsed": "01:00",
        "attempt": 1,
        "patch": "diff --git a/a b/b\n",
        "test_output": "All 5 tests succeeded",
        "review": {
            "passed": False,
            "confidence": 0.4,
            "summary": "Flaky failure",
            "findings": ["Test timed out once"],
        },
        "events": [
            {"timestamp": "09:00:00", "actor": "tester", "message": "Started run"},
        ],
    }

    out_dir = tmp_path / "bundle_dict"
    export_bundle(data, out_dir)

    assert (out_dir / "patch.diff").read_text(encoding="utf-8") == "diff --git a/a b/b\n"
    assert (out_dir / "test_output.txt").read_text(encoding="utf-8") == "All 5 tests succeeded"
    assert "Flaky failure" in (out_dir / "review.md").read_text(encoding="utf-8")
    assert "FAILED" in (out_dir / "review.md").read_text(encoding="utf-8")
    assert "Started run" in (out_dir / "events.log").read_text(encoding="utf-8")


def test_create_draft_pr_success() -> None:
    run = Run(
        number=10,
        title="Add auth middleware",
        state=RunState.READY_FOR_APPROVAL,
        branch="feat/auth-middleware",
    )

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/owner/repo/pull/123\n"
        mock_run.return_value = mock_proc

        url = create_draft_pr(run, repo_path="/tmp/fake-repo")

        assert url == "https://github.com/owner/repo/pull/123"
        mock_run.assert_called_once_with(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                "[Shipwright] Add auth middleware",
                "--body",
                "Automated PR by Shipwright...",
                "--head",
                "feat/auth-middleware",
            ],
            cwd="/tmp/fake-repo",
            capture_output=True,
            text=True,
            check=False,
        )


def test_create_draft_pr_gh_failure() -> None:
    run = Run(
        number=11,
        title="Failing PR",
        state=RunState.READY_FOR_APPROVAL,
        branch="feat/failing",
    )

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Could not resolve to a Repository\n"
        mock_run.return_value = mock_proc

        url = create_draft_pr(run, repo_path=".")
        assert url is None


def test_create_draft_pr_gh_not_installed() -> None:
    run = Run(
        number=12,
        title="Missing GH",
        state=RunState.READY_FOR_APPROVAL,
        branch="feat/test",
    )

    with patch("shutil.which", return_value=None):
        url = create_draft_pr(run, repo_path=".")
        assert url is None


def test_create_draft_pr_subprocess_error() -> None:
    run = Run(
        number=13,
        title="Error PR",
        state=RunState.READY_FOR_APPROVAL,
        branch="feat/err",
    )

    with patch("subprocess.run", side_effect=OSError("Command failed")):
        url = create_draft_pr(run, repo_path=".")
        assert url is None
