"""Unit tests for repository resolution, parsing, and cloning in shipwright.repo."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from shipwright.models import Repository
from shipwright.repo import (
    RepoError,
    infer_repo_from_issue_url,
    parse_repo_source,
    resolve_repo,
)
from shipwright.runner import CommandRunner, detect_test_command


def test_infer_repo_from_issue_url() -> None:
    assert (
        infer_repo_from_issue_url("https://github.com/owner/repo/issues/42")
        == "https://github.com/owner/repo.git"
    )
    assert (
        infer_repo_from_issue_url("https://github.com/owner/repo/issues/123")
        == "https://github.com/owner/repo.git"
    )
    assert (
        infer_repo_from_issue_url("https://github.com/pallets/flask/issues/456#comment-1")
        == "https://github.com/pallets/flask.git"
    )
    assert (
        infer_repo_from_issue_url("http://github.com/owner/repo/issues/1")
        == "https://github.com/owner/repo.git"
    )
    assert (
        infer_repo_from_issue_url("https://www.github.com/owner/repo/issues/99")
        == "https://github.com/owner/repo.git"
    )
    assert (
        infer_repo_from_issue_url("https://github.com/owner/repo/pull/50")
        == "https://github.com/owner/repo.git"
    )
    # Non-issue URLs return None
    assert infer_repo_from_issue_url("https://github.com/owner/repo") is None
    assert infer_repo_from_issue_url("https://github.com/owner/repo/blob/main/README.md") is None
    assert infer_repo_from_issue_url("https://example.com/not/github/issues/1") is None
    assert infer_repo_from_issue_url("owner/repo") is None
    assert infer_repo_from_issue_url("") is None
    assert infer_repo_from_issue_url(None) is None


def test_parse_repo_source_shorthand() -> None:
    res = parse_repo_source("owner/repo")
    assert res == {
        "type": "remote",
        "url": "https://github.com/owner/repo.git",
        "name": "repo",
    }

    res2 = parse_repo_source("octocat/Hello-World.git")
    assert res2 == {
        "type": "remote",
        "url": "https://github.com/octocat/Hello-World.git",
        "name": "Hello-World",
    }


def test_parse_repo_source_github_urls() -> None:
    https_res = parse_repo_source("https://github.com/pallets/flask")
    assert https_res == {
        "type": "remote",
        "url": "https://github.com/pallets/flask.git",
        "name": "flask",
    }

    https_git_res = parse_repo_source("https://github.com/pallets/flask.git")
    assert https_git_res == {
        "type": "remote",
        "url": "https://github.com/pallets/flask.git",
        "name": "flask",
    }

    ssh_res = parse_repo_source("git@github.com:psf/black.git")
    assert ssh_res == {
        "type": "remote",
        "url": "git@github.com:psf/black.git",
        "name": "black",
    }

    ssh_no_git = parse_repo_source("git@github.com:psf/black")
    assert ssh_no_git == {
        "type": "remote",
        "url": "git@github.com:psf/black.git",
        "name": "black",
    }


def test_parse_repo_source_local_paths(tmp_path: Path) -> None:
    dot_res = parse_repo_source(".")
    assert dot_res["type"] == "local"
    assert dot_res["url"] == str(Path.cwd())
    assert dot_res["name"] == Path.cwd().name

    sub_dir = tmp_path / "my_project"
    sub_dir.mkdir()
    local_res = parse_repo_source(str(sub_dir))
    assert local_res == {
        "type": "local",
        "url": str(sub_dir.resolve()),
        "name": "my_project",
    }

    path_res = parse_repo_source(sub_dir)
    assert path_res == {
        "type": "local",
        "url": str(sub_dir.resolve()),
        "name": "my_project",
    }


def test_parse_repo_source_empty_raises() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_repo_source("")
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_repo_source("   ")


def test_resolve_repo_local_non_git_initializes(tmp_path: Path) -> None:
    sample_dir = tmp_path / "plain_dir"
    sample_dir.mkdir()
    (sample_dir / "README.md").write_text("# Plain Dir\n")

    repo = resolve_repo(str(sample_dir), default_branch="main")
    assert isinstance(repo, Repository)
    assert repo.root == str(sample_dir.resolve())
    assert repo.default_branch == "main"
    assert (sample_dir / ".git").exists()

    # Verify worktrees can be created since there is an initial commit
    worktree_target = tmp_path / "wt1"
    cmd = subprocess.run(
        ["git", "worktree", "add", str(worktree_target), "-b", "feature-test"],
        cwd=sample_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cmd.returncode == 0
    assert (worktree_target / "README.md").exists()


def test_resolve_repo_local_existing_git(tmp_path: Path) -> None:
    git_dir = tmp_path / "git_dir"
    git_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=git_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@e.com", "commit", "--allow-empty", "-m", "init"],
        cwd=git_dir,
        check=True,
    )

    repo = resolve_repo(str(git_dir))
    assert repo.root == str(git_dir.resolve())
    assert repo.default_branch == "master"
    assert repo.remote_url is None


def test_resolve_repo_local_nonexistent_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "missing_folder"
    with pytest.raises(FileNotFoundError):
        resolve_repo(str(nonexistent))


def test_resolve_repo_remote_clone(tmp_path: Path) -> None:
    target_cache = tmp_path / "cache"

    with (
        patch("shipwright.repo.subprocess.run") as mock_run,
        patch("shipwright.repo._is_git_repo", return_value=False),
        patch("shipwright.repo._detect_default_branch", return_value="main"),
    ):
        # Simulate successful git clone
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "clone"],
            returncode=0,
            stdout="",
            stderr="",
        )

        repo = resolve_repo("owner/myrepo", cache_dir=target_cache)
        assert repo.remote_url == "https://github.com/owner/myrepo.git"
        assert repo.default_branch == "main"
        assert repo.root == str(target_cache.resolve())
        # Verify git clone was called
        mock_run.assert_any_call(
            ["git", "clone", "https://github.com/owner/myrepo.git", str(target_cache.resolve())],
            capture_output=True,
            text=True,
            check=False,
        )


def test_resolve_repo_remote_already_exists_runs_fetch(tmp_path: Path) -> None:
    repo_dir = tmp_path / "existing_remote"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    with (
        patch("shipwright.repo.subprocess.run") as mock_run,
        patch("shipwright.repo._is_git_repo", return_value=True),
        patch("shipwright.repo._detect_default_branch", return_value="main"),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )

        repo = resolve_repo("owner/existing_remote", cache_dir=repo_dir)
        assert repo.remote_url == "https://github.com/owner/existing_remote.git"
        mock_run.assert_any_call(
            ["git", "fetch", "--all", "--prune"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )


def test_resolve_repo_remote_clone_failure_raises(tmp_path: Path) -> None:
    target_cache = tmp_path / "fail_cache"

    with (
        patch("shipwright.repo.subprocess.run") as mock_run,
        patch("shipwright.repo._is_git_repo", return_value=False),
        pytest.raises(RepoError, match="Failed to clone"),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "clone"],
            returncode=128,
            stdout="",
            stderr="fatal: repository not found",
        )

        resolve_repo("owner/notfound", cache_dir=target_cache)


def test_runner_multi_lang_detection_package_json_npm(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "mocha"}}', encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["npm", "test"]
    assert detect_test_command(tmp_path) == ["npm", "test"]


def test_runner_multi_lang_detection_package_json_pnpm(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").touch()
    assert runner.detect_test_command(tmp_path) == ["pnpm", "test"]
    assert detect_test_command(tmp_path) == ["pnpm", "test"]


def test_runner_multi_lang_detection_package_json_yarn(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    (tmp_path / "yarn.lock").touch()
    assert runner.detect_test_command(tmp_path) == ["yarn", "test"]
    assert detect_test_command(tmp_path) == ["yarn", "test"]


def test_runner_multi_lang_detection_package_json_bun(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "package.json").write_text('{"scripts": {"test": "bun test"}}', encoding="utf-8")
    (tmp_path / "bun.lockb").touch()
    assert runner.detect_test_command(tmp_path) == ["bun", "test"]
    assert detect_test_command(tmp_path) == ["bun", "test"]


def test_runner_multi_lang_detection_cargo_toml(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "mycrate"\nversion = "0.1.0"\n', encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["cargo", "test"]
    assert detect_test_command(tmp_path) == ["cargo", "test"]


def test_runner_multi_lang_detection_go_mod(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "go.mod").write_text("module example.com/mymod\n\ngo 1.21\n", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["go", "test", "./..."]
    assert detect_test_command(tmp_path) == ["go", "test", "./..."]


def test_runner_multi_lang_detection_makefile(tmp_path: Path) -> None:
    runner = CommandRunner()
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\ntest:\n\t@echo testing\n", encoding="utf-8")
    assert runner.detect_test_command(tmp_path) == ["make", "test"]
    assert detect_test_command(tmp_path) == ["make", "test"]

