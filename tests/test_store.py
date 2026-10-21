"""Unit tests for shipwright.store.RunStore SQLite storage."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shipwright.models import (
    Artifact,
    Review,
    Run,
    RunState,
    TimelineEvent,
    fixture_runs,
)
from shipwright.store import DEFAULT_DB_PATH, RunStore


def test_run_store_in_memory_default() -> None:
    store = RunStore()
    assert store.db_path == ":memory:"
    assert store.count_runs() == 0
    store.close()


def test_run_store_file_path(tmp_path: Path) -> None:
    db_file = tmp_path / "sub" / "custom_runs.db"
    store = RunStore(db_file)
    assert db_file.exists()
    assert store.count_runs() == 0
    store.close()


def test_run_store_default_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    store = RunStore(default_dir=True)
    expected_path = fake_home / ".shipwright" / "runs.db"
    assert store.db_path == expected_path
    assert expected_path.exists()
    store.close()


def test_init_db_is_idempotent() -> None:
    store = RunStore()
    store.init_db()
    store.init_db()
    assert store.count_runs() == 0
    store.close()


def test_save_and_get_run() -> None:
    store = RunStore()
    run = Run(
        number=1001,
        title="Refactor auth token parser",
        state=RunState.INVESTIGATING,
        elapsed="01:15",
        agents="1 / 4",
        tests="2 passed",
        repo_path="/repos/omnihack",
        worktree_path="/tmp/worktree_1001",
        branch="shipwright/fix-1001",
        attempt=1,
        max_attempts=3,
        error=None,
    )

    store.save_run(run)
    assert store.count_runs() == 1

    loaded = store.get_run(1001)
    assert loaded is not None
    assert loaded.number == 1001
    assert loaded.title == "Refactor auth token parser"
    assert loaded.state is RunState.INVESTIGATING
    assert loaded.elapsed == "01:15"
    assert loaded.agents == "1 / 4"
    assert loaded.tests == "2 passed"
    assert loaded.repo_path == "/repos/omnihack"
    assert loaded.worktree_path == "/tmp/worktree_1001"
    assert loaded.branch == "shipwright/fix-1001"
    assert loaded.attempt == 1
    assert loaded.max_attempts == 3
    assert loaded.error is None
    assert loaded.events == []
    assert loaded.artifacts == []
    assert loaded.review is None

    store.close()


def test_save_run_upsert() -> None:
    store = RunStore()
    run = Run(
        number=1002,
        title="Handle socket timeout",
        state=RunState.INVESTIGATING,
        elapsed="00:30",
        agents="1 / 4",
        tests="pending",
    )
    store.save_run(run)

    # Update state and tests
    run.state = RunState.VERIFYING
    run.elapsed = "02:45"
    run.tests = "12 passed"
    run.error = "Minor warning"
    store.save_run(run)

    assert store.count_runs() == 1
    loaded = store.get_run(1002)
    assert loaded is not None
    assert loaded.state is RunState.VERIFYING
    assert loaded.elapsed == "02:45"
    assert loaded.tests == "12 passed"
    assert loaded.error == "Minor warning"
    store.close()


def test_add_events() -> None:
    store = RunStore()
    run = Run(number=2001, title="Test Events", state=RunState.INVESTIGATING)
    store.save_run(run)

    ev1 = TimelineEvent("10:00:01", "investigator", "analyzed AST")
    ev2 = TimelineEvent("10:00:15", "implementer", "created patch")

    store.add_event(2001, ev1)
    store.add_event(2001, ev2)

    loaded = store.get_run(2001)
    assert loaded is not None
    assert len(loaded.events) == 2
    assert loaded.events[0].actor == "investigator"
    assert loaded.events[0].message == "analyzed AST"
    assert loaded.events[1].actor == "implementer"
    assert loaded.events[1].message == "created patch"

    direct_events = store.get_events(2001)
    assert len(direct_events) == 2
    assert direct_events[0] == ev1

    store.close()


def test_add_artifacts() -> None:
    store = RunStore()
    run = Run(number=3001, title="Test Artifacts", state=RunState.VERIFYING)
    store.save_run(run)

    store.add_artifact(3001, "diff.patch", "diff --git a/file b/file\n+new line")
    store.add_artifact(3001, "pytest.log", "================ 5 passed in 0.1s ================")

    loaded = store.get_run(3001)
    assert loaded is not None
    assert len(loaded.artifacts) == 2
    assert str(loaded.artifacts[0]) == "diff.patch"
    assert loaded.artifacts[0].content == "diff --git a/file b/file\n+new line"
    assert str(loaded.artifacts[1]) == "pytest.log"
    assert loaded.artifacts[1].content == "================ 5 passed in 0.1s ================"

    # Updating existing artifact content
    store.add_artifact(3001, "diff.patch", "diff --git a/file b/file\n+updated line")
    loaded2 = store.get_run(3001)
    assert loaded2 is not None
    assert len(loaded2.artifacts) == 2
    assert loaded2.artifacts[0].content == "diff --git a/file b/file\n+updated line"

    store.close()


def test_save_and_get_review() -> None:
    store = RunStore()
    run = Run(number=4001, title="Test Review", state=RunState.REVIEWING)
    store.save_run(run)

    review = Review(
        passed=True,
        confidence=0.92,
        summary="High-quality patch with tests.",
        findings=["Add docstring", "Clean up imports"],
    )
    store.save_review(4001, review)

    loaded = store.get_run(4001)
    assert loaded is not None
    assert loaded.review is not None
    assert loaded.review.passed is True
    assert pytest.approx(loaded.review.confidence, 0.01) == 0.92
    assert loaded.review.summary == "High-quality patch with tests."
    assert loaded.review.findings == ["Add docstring", "Clean up imports"]

    direct_review = store.get_review(4001)
    assert direct_review is not None
    assert direct_review.passed is True

    # Update review
    updated_review = Review(
        passed=False,
        confidence=0.45,
        summary="Found regression.",
        findings=["Regression in auth flow"],
    )
    store.save_review(4001, updated_review)
    loaded_updated = store.get_run(4001)
    assert loaded_updated is not None
    assert loaded_updated.review is not None
    assert loaded_updated.review.passed is False
    assert loaded_updated.review.findings == ["Regression in auth flow"]

    store.close()


def test_list_runs_ordered_descending() -> None:
    store = RunStore()
    r1 = Run(number=10, title="Run 10", state=RunState.QUEUED)
    r2 = Run(number=30, title="Run 30", state=RunState.INVESTIGATING)
    r3 = Run(number=20, title="Run 20", state=RunState.VERIFYING)

    store.save_run(r1)
    store.save_run(r2)
    store.save_run(r3)

    runs = store.list_runs()
    assert [r.number for r in runs] == [30, 20, 10]
    store.close()


def test_get_nonexistent_run() -> None:
    store = RunStore()
    assert store.get_run(99999) is None
    assert store.get_review(99999) is None
    assert store.get_artifacts(99999) == []
    assert store.get_events(99999) == []
    store.close()


def test_delete_run() -> None:
    store = RunStore()
    run = Run(number=5001, title="To Be Deleted", state=RunState.QUEUED)
    store.save_run(run)
    store.add_event(5001, TimelineEvent("00:00:00", "actor", "msg"))
    store.add_artifact(5001, "log.txt", "content")
    store.save_review(5001, Review(True, 1.0, "ok"))

    assert store.count_runs() == 1
    assert store.delete_run(5001) is True
    assert store.count_runs() == 0
    assert store.get_run(5001) is None
    assert store.get_events(5001) == []
    assert store.get_artifacts(5001) == []
    assert store.get_review(5001) is None

    assert store.delete_run(5001) is False
    store.close()


def test_fixture_runs_storage() -> None:
    store = RunStore()
    fixtures = fixture_runs()
    for run in fixtures:
        store.save_run(run)

    assert store.count_runs() == len(fixtures)
    stored_runs = store.list_runs()
    assert [r.number for r in stored_runs] == [1842, 1839, 1828]

    first_run = store.get_run(1842)
    assert first_run is not None
    assert len(first_run.events) == 4
    assert len(first_run.artifacts) == 3
    store.close()


def test_context_manager() -> None:
    with RunStore() as store:
        run = Run(number=7001, title="Context Test", state=RunState.QUEUED)
        store.save_run(run)
        assert store.count_runs() == 1
