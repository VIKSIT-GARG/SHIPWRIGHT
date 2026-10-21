"""Comprehensive tests for Shipwright CLI."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shipwright.cli import (
    KNOWN_SUBCOMMANDS,
    ShipwrightParser,
    _normalize_argv,
    build_parser,
    infer_repo_from_issue_url,
    is_git_url,
    is_github_shorthand,
    main,
    resolve_repository,
)
from shipwright.models import (
    Artifact,
    Issue,
    Review,
    Run,
    RunState,
    TimelineEvent,
    fixture_runs,
)
from shipwright.providers import ProviderError, ProviderResponse
from shipwright.store import RunStore


# ---------------------------------------------------------------------------
# Argument Normalization and Parser Tests
# ---------------------------------------------------------------------------


def test_normalize_argv_empty() -> None:
    # shipwright with no args -> empty (triggers TUI)
    assert _normalize_argv([], prog="shipwright") == []
    # shipwright-chat with no args -> chat with default prompt
    assert _normalize_argv([], prog="shipwright-chat") == ["chat"]


def test_normalize_argv_subcommands_and_help() -> None:
    for cmd in KNOWN_SUBCOMMANDS:
        assert _normalize_argv([cmd, "arg1"], prog="shipwright") == [cmd, "arg1"]
    assert _normalize_argv(["--help"], prog="shipwright") == ["--help"]
    assert _normalize_argv(["-h"], prog="shipwright") == ["-h"]


def test_normalize_argv_direct_prompts_and_flags() -> None:
    assert _normalize_argv(["Hello world"], prog="shipwright") == ["chat", "Hello world"]
    assert _normalize_argv(["Hello world"], prog="shipwright-chat") == ["chat", "Hello world"]
    assert _normalize_argv(["--provider", "ollama", "Hello"], prog="shipwright-chat") == [
        "chat",
        "--provider",
        "ollama",
        "Hello",
    ]


def test_build_parser_options() -> None:
    parser = build_parser()
    assert isinstance(parser, ShipwrightParser)

    # run subcommand
    args_run = parser.parse_args(["run", "1842", "--repo", "/tmp/repo", "--max-attempts", "5"])
    assert args_run.subcommand == "run"
    assert args_run.issue_or_text == "1842"
    assert args_run.repo == "/tmp/repo"
    assert args_run.max_attempts == 5

    # list subcommand
    args_list = parser.parse_args(["list", "--state", "QUEUED", "--limit", "10", "--json"])
    assert args_list.subcommand == "list"
    assert args_list.state == "QUEUED"
    assert args_list.limit == 10
    assert args_list.json is True

    # status subcommand
    args_status = parser.parse_args(["status", "1842", "--json"])
    assert args_status.subcommand == "status"
    assert args_status.run_number == 1842
    assert args_status.json is True

    # export subcommand
    args_export = parser.parse_args(["export", "1842", "--out", "/tmp/export", "--approve"])
    assert args_export.subcommand == "export"
    assert args_export.run_number == 1842
    assert args_export.out == "/tmp/export"
    assert args_export.approve is True


# ---------------------------------------------------------------------------
# Default Launch (TUI) Tests
# ---------------------------------------------------------------------------


def test_cli_default_no_arguments_launches_tui() -> None:
    with patch("shipwright.tui.main") as mock_tui:
        exit_code = main([])
        assert exit_code == 0
        mock_tui.assert_called_once()


# ---------------------------------------------------------------------------
# Chat Subcommand & Direct Prompt Tests (Backwards Compatibility)
# ---------------------------------------------------------------------------


def test_cli_chat_direct_string(capsys: pytest.CaptureFixture[str]) -> None:
    mock_provider = MagicMock()
    mock_provider.complete.return_value = ProviderResponse(content="AI response here")

    with patch("shipwright.cli.create_provider", return_value=mock_provider):
        # Direct string without 'chat' keyword
        exit_code = main(["Explain quantum computing"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "AI response here" in captured.out
        mock_provider.complete.assert_called_once_with(
            [{"role": "user", "content": "Explain quantum computing"}]
        )


def test_cli_chat_subcommand_explicit(capsys: pytest.CaptureFixture[str]) -> None:
    mock_provider = MagicMock()
    mock_provider.complete.return_value = ProviderResponse(content="Explicit chat response")

    with patch("shipwright.cli.create_provider", return_value=mock_provider):
        exit_code = main(["chat", "Hello provider"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Explicit chat response" in captured.out


def test_cli_chat_backwards_compatible_shipwright_chat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_provider = MagicMock()
    mock_provider.complete.return_value = ProviderResponse(content="SHIPWRIGHT_OK")

    monkeypatch.setattr(sys, "argv", ["/bin/shipwright-chat"])
    with patch("shipwright.cli.create_provider", return_value=mock_provider):
        exit_code = main([])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "SHIPWRIGHT_OK" in captured.out
        mock_provider.complete.assert_called_once_with(
            [{"role": "user", "content": "Reply with exactly: SHIPWRIGHT_OK"}]
        )


def test_cli_chat_provider_and_model_overrides() -> None:
    mock_provider = MagicMock()
    mock_provider.complete.return_value = ProviderResponse(content="ok")

    passed_envs: list[dict[str, str] | None] = []

    def mock_create_provider(env: dict[str, str] | None = None) -> Any:
        passed_envs.append(env)
        return mock_provider

    with patch("shipwright.cli.create_provider", side_effect=mock_create_provider):
        exit_code = main(["chat", "prompt", "--provider", "openai", "--model", "gpt-4o"])
        assert exit_code == 0
        assert len(passed_envs) == 1
        assert passed_envs[0] is not None
        assert passed_envs[0]["SHIPWRIGHT_PROVIDER"] == "openai"
        assert passed_envs[0]["SHIPWRIGHT_MODEL"] == "gpt-4o"


def test_cli_chat_handles_provider_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "shipwright.cli.create_provider",
        side_effect=ProviderError("Rate limit reached", status_code=429),
    ):
        exit_code = main(["chat", "hello"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Rate limit reached" in captured.err


# ---------------------------------------------------------------------------
# Run Subcommand Tests
# ---------------------------------------------------------------------------


def test_cli_run_headless_success(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    test_run = Run(
        number=101,
        title="Fix NPE in auth handler",
        state=RunState.READY_FOR_APPROVAL,
        elapsed="01:23",
        agents="4 / 4",
        tests="12 passed",
        branch="shipwright/run-101",
        review=Review(
            passed=True,
            confidence=0.96,
            summary="Clean null check added.",
            findings=["Verified with regression test"],
        ),
        artifacts=[Artifact(name="patch.diff", path="patch.diff", content="+nullcheck")],
    )

    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = test_run

    with patch("shipwright.cli.Orchestrator", return_value=mock_orch):
        exit_code = main(["run", "101", "--db", str(tmp_path / "test.db")])
        assert exit_code == 0
        mock_orch.run_issue.assert_called_once()
        # Verify issue was passed to run_issue
        call_args = mock_orch.run_issue.call_args[0]
        assert isinstance(call_args[0], Issue)
        assert call_args[0].number == 101

        captured = capsys.readouterr()
        assert "SHIPWRIGHT HEADLESS RUN" in captured.out
        assert "RUN SUMMARY" in captured.out
        assert "Run Number:  #101" in captured.out
        assert "State:       READY_FOR_APPROVAL" in captured.out
        assert "Review:      PASSED (Confidence: 0.96)" in captured.out
        assert "patch.diff" in captured.out


def test_cli_run_headless_failure(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    blocked_run = Run(
        number=102,
        title="Failing issue",
        state=RunState.BLOCKED,
        elapsed="03:00",
        agents="3 / 4",
        tests="1 failed",
        error="Tests failed after 3 attempts",
    )

    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = blocked_run

    with patch("shipwright.cli.Orchestrator", return_value=mock_orch):
        exit_code = main(["run", "102", "--db", str(tmp_path / "test.db")])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "State:       BLOCKED" in captured.out
        assert "Error:       Tests failed after 3 attempts" in captured.out


def test_cli_run_auto_approve(tmp_path: Path) -> None:
    test_run = Run(
        number=103,
        title="Auto-approve issue",
        state=RunState.READY_FOR_APPROVAL,
        review=Review(passed=True, confidence=1.0, summary="ok"),
    )

    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = test_run

    with patch("shipwright.cli.Orchestrator", return_value=mock_orch), patch(
        "shipwright.cli.export_bundle"
    ) as mock_export:
        exit_code = main(["run", "103", "--auto-approve", "--db", str(tmp_path / "test.db")])
        assert exit_code == 0
        assert test_run.state == RunState.EXPORTED
        mock_export.assert_called_once()


# ---------------------------------------------------------------------------
# List Subcommand Tests
# ---------------------------------------------------------------------------


def test_cli_list_runs_table(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "runs.db"
    store = RunStore(db_file)
    store.save_run(Run(number=10, title="First run", state=RunState.QUEUED, elapsed="00:10", tests="pending"))
    store.save_run(Run(number=20, title="Second run", state=RunState.VERIFYING, elapsed="01:20", tests="5 passed"))
    store.close()

    exit_code = main(["list", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "RUN" in captured.out
    assert "#20" in captured.out
    assert "Second run" in captured.out
    assert "#10" in captured.out
    assert "First run" in captured.out


def test_cli_list_runs_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "runs.db"
    store = RunStore(db_file)
    store.save_run(Run(number=30, title="JSON run", state=RunState.READY_FOR_APPROVAL))
    store.close()

    exit_code = main(["list", "--json", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["number"] == 30
    assert data[0]["state"] == "READY_FOR_APPROVAL"


def test_cli_list_empty(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "empty.db"
    exit_code = main(["list", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No runs found." in captured.out


def test_cli_list_filter_state(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "filter.db"
    store = RunStore(db_file)
    store.save_run(Run(number=1, title="Queued run", state=RunState.QUEUED))
    store.save_run(Run(number=2, title="Verifying run", state=RunState.VERIFYING))
    store.close()

    exit_code = main(["list", "--state", "VERIFYING", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "#2" in captured.out
    assert "#1" not in captured.out


# ---------------------------------------------------------------------------
# Status Subcommand Tests
# ---------------------------------------------------------------------------


def test_cli_status_existing_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "status.db"
    store = RunStore(db_file)
    run = Run(
        number=555,
        title="Check status test",
        state=RunState.INVESTIGATING,
        elapsed="00:45",
        agents="1 / 4",
        tests="pending",
        branch="shipwright/run-555",
        events=[TimelineEvent("12:00:00", "investigator", "reading files")],
        artifacts=[Artifact(name="plan.json", path="plan.json", content="{}")],
    )
    store.save_run(run)
    store.close()

    exit_code = main(["status", "555", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "RUN #555: Check status test" in captured.out
    assert "State:       INVESTIGATING" in captured.out
    assert "TIMELINE EVENTS:" in captured.out
    assert "reading files" in captured.out
    assert "ARTIFACTS:" in captured.out
    assert "plan.json" in captured.out


def test_cli_status_json_format(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "status_json.db"
    store = RunStore(db_file)
    store.save_run(Run(number=777, title="Status JSON", state=RunState.EXPORTED))
    store.close()

    exit_code = main(["status", "777", "--json", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["number"] == 777
    assert data["state"] == "EXPORTED"
    assert "events" in data
    assert "artifacts" in data


def test_cli_status_nonexistent_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "missing.db"
    exit_code = main(["status", "99999", "--db", str(db_file)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Run #99999 not found" in captured.err


# ---------------------------------------------------------------------------
# Export Subcommand Tests
# ---------------------------------------------------------------------------


def test_cli_export_subcommand(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "export.db"
    store = RunStore(db_file)
    run = Run(
        number=1842,
        title="Fix flaky retry test",
        state=RunState.READY_FOR_APPROVAL,
        elapsed="02:14",
        tests="42 passed",
        events=[TimelineEvent("10:00:00", "verifier", "tests passed")],
        artifacts=[Artifact(name="patch.diff", path="patch.diff", content="diff --git a b\n")],
    )
    store.save_run(run)
    store.close()

    export_dir = tmp_path / "bundle_export"
    exit_code = main(["export", "1842", "--out", str(export_dir), "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Exported run #1842 bundle to:" in captured.out
    assert export_dir.is_dir()
    assert (export_dir / "patch.diff").exists()
    assert (export_dir / "run.json").exists()
    assert (export_dir / "events.log").exists()


def test_cli_export_approve_flag(tmp_path: Path) -> None:
    db_file = tmp_path / "approve.db"
    store = RunStore(db_file)
    run = Run(number=888, title="Approve test", state=RunState.READY_FOR_APPROVAL)
    store.save_run(run)
    store.close()

    out_dir = tmp_path / "bundle_approved"
    exit_code = main(["export", "888", "--out", str(out_dir), "--approve", "--db", str(db_file)])
    assert exit_code == 0

    reloaded_store = RunStore(db_file)
    updated_run = reloaded_store.get_run(888)
    assert updated_run is not None
    assert updated_run.state == RunState.EXPORTED
    reloaded_store.close()


def test_cli_export_patch_format_stdout(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    db_file = tmp_path / "patch.db"
    store = RunStore(db_file)
    diff_text = "diff --git a/test.py b/test.py\n+added line\n"
    run = Run(
        number=999,
        title="Patch format test",
        state=RunState.READY_FOR_APPROVAL,
        artifacts=[Artifact(name="patch.diff", content=diff_text)],
    )
    store.save_run(run)
    store.close()

    out_dir = tmp_path / "bundle_patch"
    exit_code = main(["export", "999", "--out", str(out_dir), "--format", "patch", "--db", str(db_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert diff_text in captured.out


# ---------------------------------------------------------------------------
# Repository Resolution and Auto-Inference Tests
# ---------------------------------------------------------------------------


def test_is_git_url() -> None:
    assert is_git_url("https://github.com/pallets/flask") is True
    assert is_git_url("https://github.com/pallets/flask.git") is True
    assert is_git_url("http://github.com/pallets/flask") is True
    assert is_git_url("git@github.com:pallets/flask.git") is True
    assert is_git_url("git://github.com/pallets/flask.git") is True
    assert is_git_url("ssh://git@github.com/pallets/flask.git") is True
    assert is_git_url("custom://server/repo.git") is True
    assert is_git_url("pallets/flask") is False
    assert is_git_url(".") is False
    assert is_git_url("/tmp/project") is False


def test_is_github_shorthand(tmp_path: Path) -> None:
    assert is_github_shorthand("pallets/flask") is True
    assert is_github_shorthand("owner/repo") is True
    assert is_github_shorthand("my-org/my_repo.py") is True
    assert is_github_shorthand(".") is False
    assert is_github_shorthand("/path/to/project") is False
    assert is_github_shorthand("./local/folder") is False
    assert is_github_shorthand("~/project") is False

    # Existing local git directory should not be treated as shorthand
    local_dir = tmp_path / "existing" / "repo"
    local_dir.mkdir(parents=True)
    (local_dir / ".git").mkdir()
    with patch("pathlib.Path.is_dir", return_value=True), patch("pathlib.Path.exists", return_value=True):
        assert is_github_shorthand("existing/repo") is False


def test_infer_repo_from_issue_url() -> None:
    assert (
        infer_repo_from_issue_url("https://github.com/pallets/flask/issues/42")
        == "pallets/flask"
    )
    assert (
        infer_repo_from_issue_url("http://github.com/django/django/issues/100")
        == "django/django"
    )
    assert (
        infer_repo_from_issue_url("https://github.com/org/repo.git/issues/999#comment-1")
        == "org/repo"
    )
    assert (
        infer_repo_from_issue_url("github.com/owner/sub-repo/issues/1")
        == "owner/sub-repo"
    )
    assert infer_repo_from_issue_url("1842") is None
    assert infer_repo_from_issue_url("https://example.com/not-github/issues/1") is None
    assert infer_repo_from_issue_url(None) is None


def test_resolve_repository_local(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    local_dir = tmp_path / "my_project"
    local_dir.mkdir()
    resolved = resolve_repository(str(local_dir))
    assert resolved == local_dir.resolve()
    captured = capsys.readouterr()
    assert f"Resolving target repository: {local_dir}..." in captured.out
    assert f"Resolved target repository to: {local_dir.resolve()}" in captured.out


def test_resolve_repository_local_nonexistent() -> None:
    with pytest.raises(FileNotFoundError):
        resolve_repository("/nonexistent/directory/that/does/not/exist")


def test_resolve_repository_shorthand_and_cache(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"

    # 1. First resolution: clones in mock mode
    with patch.dict("os.environ", {"SHIPWRIGHT_MOCK_MODE": "1"}):
        resolved = resolve_repository("pallets/flask", cache_dir=cache_dir)
        expected = cache_dir / "pallets" / "flask"
        assert resolved == expected.resolve()
        assert (expected / ".git").exists()
        captured = capsys.readouterr()
        assert "Resolving target repository: pallets/flask..." in captured.out
        assert f"Repository cloned to: {expected}" in captured.out

    # 2. Second resolution: reuses cached directory
    resolved2 = resolve_repository("pallets/flask", cache_dir=cache_dir)
    assert resolved2 == expected.resolve()
    captured2 = capsys.readouterr()
    assert "Resolving target repository: pallets/flask..." in captured2.out
    assert f"Using cached repository: {expected}" in captured2.out


def test_resolve_repository_remote_url(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    url = "https://github.com/pallets/flask.git"
    with patch.dict("os.environ", {"SHIPWRIGHT_MOCK_MODE": "1"}):
        resolved = resolve_repository(url, cache_dir=cache_dir)
        expected = cache_dir / "pallets" / "flask"
        assert resolved == expected.resolve()
        captured = capsys.readouterr()
        assert f"Resolving target repository: {url}..." in captured.out
        assert f"Repository cloned to: {expected}" in captured.out


def test_resolve_repository_git_clone_failure(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stderr="Repository not found", stdout="")
        with pytest.raises(RuntimeError, match="git clone failed"):
            resolve_repository("nonexistent/repo", cache_dir=cache_dir)


def test_cli_run_auto_infer_from_issue_arg(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    mock_run = Run(
        number=42,
        title="Inferred issue run",
        state=RunState.READY_FOR_APPROVAL,
    )
    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = mock_run

    with patch.dict("os.environ", {"SHIPWRIGHT_MOCK_MODE": "1", "SHIPWRIGHT_REPOS_CACHE": str(tmp_path / "repos")}), \
         patch("shipwright.cli.Orchestrator", return_value=mock_orch):
        exit_code = main(["run", "https://github.com/pallets/flask/issues/42", "--db", str(tmp_path / "test.db")])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Resolving target repository: pallets/flask..." in captured.out
        assert "Repository cloned to:" in captured.out


def test_cli_run_auto_infer_from_issue_url_property(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    issue_file = tmp_path / "task.json"
    issue_file.write_text(
        json.dumps({
            "number": 99,
            "title": "Loaded from json",
            "body": "Fix bug",
            "url": "https://github.com/pallets/flask/issues/99"
        }),
        encoding="utf-8"
    )

    mock_run = Run(number=99, title="Loaded from json", state=RunState.READY_FOR_APPROVAL)
    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = mock_run

    with patch.dict("os.environ", {"SHIPWRIGHT_MOCK_MODE": "1", "SHIPWRIGHT_REPOS_CACHE": str(tmp_path / "repos")}), \
         patch("shipwright.cli.Orchestrator", return_value=mock_orch):
        exit_code = main(["run", str(issue_file), "--db", str(tmp_path / "test.db")])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Resolving target repository: pallets/flask..." in captured.out


def test_cli_run_explicit_repo_overrides_issue_url(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    local_repo = tmp_path / "my_custom_repo"
    local_repo.mkdir()

    mock_run = Run(number=42, title="Explicit repo run", state=RunState.READY_FOR_APPROVAL)
    mock_orch = MagicMock()
    mock_orch.run_issue.return_value = mock_run

    with patch("shipwright.cli.Orchestrator", return_value=mock_orch):
        exit_code = main([
            "run",
            "https://github.com/pallets/flask/issues/42",
            "--repo",
            str(local_repo),
            "--db",
            str(tmp_path / "test.db")
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert f"Resolving target repository: {local_repo}..." in captured.out
        assert f"Resolved target repository to: {local_repo.resolve()}" in captured.out

