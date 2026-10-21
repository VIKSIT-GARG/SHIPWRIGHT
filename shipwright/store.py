"""SQLite storage for Shipwright runs, events, artifacts, and reviews."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from shipwright.models import Artifact, Review, Run, RunState, TimelineEvent

def get_default_db_path() -> Path:
    """Return the default database path in the user's home directory."""
    return Path.home() / ".shipwright" / "runs.db"


DEFAULT_DB_DIR = Path.home() / ".shipwright"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "runs.db"


class RunStore:
    """SQLite-backed persistent store for Shipwright runs and related records."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        default_dir: bool = False,
    ) -> None:
        """Initialize SQLite storage.

        Args:
            db_path: Explicit file path, ':memory:', or None.
            default_dir: If True (or if db_path is 'default'), uses ~/.shipwright/runs.db.
                         If db_path is None and default_dir is False, defaults to in-memory ':memory:'.
        """
        if db_path is None:
            if default_dir or os.environ.get("SHIPWRIGHT_USE_DEFAULT_DIR") == "1":
                target_path: str | Path = get_default_db_path()
            else:
                target_path = ":memory:"
        elif str(db_path) == "default":
            target_path = get_default_db_path()
        elif str(db_path) == ":memory:":
            target_path = ":memory:"
        else:
            expanded = os.path.expanduser(str(db_path))
            target_path = Path(expanded)

        if target_path != ":memory:":
            Path(target_path).parent.mkdir(parents=True, exist_ok=True)

        self.db_path: str | Path = target_path
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self.init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        """Direct access to the underlying SQLite connection."""
        return self._conn

    def init_db(self) -> None:
        """Create tables for runs, events, artifacts, and reviews if not already present."""
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    number INT PRIMARY KEY,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    elapsed TEXT NOT NULL DEFAULT '',
                    agents TEXT NOT NULL DEFAULT '',
                    tests TEXT NOT NULL DEFAULT '',
                    repo_path TEXT,
                    worktree_path TEXT,
                    branch TEXT,
                    attempt INT NOT NULL DEFAULT 0,
                    max_attempts INT NOT NULL DEFAULT 3,
                    error TEXT,
                    created_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_number INT NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    message TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_number INT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    run_number INT PRIMARY KEY,
                    passed INT NOT NULL,
                    confidence REAL NOT NULL,
                    summary TEXT NOT NULL,
                    findings_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_events_run_number ON events (run_number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_number ON artifacts (run_number);
                """
            )

    def save_run(self, run: Run) -> None:
        """Persist or update a run, its timeline events, artifacts, and review."""
        state_val = run.state.value if hasattr(run.state, "value") else str(run.state)
        created_at = getattr(run, "created_at", None)
        if created_at is None:
            created_at = time.time()

        elapsed = getattr(run, "elapsed", "") or ""
        agents = getattr(run, "agents", "") or ""
        tests = getattr(run, "tests", "") or ""
        repo_path = getattr(run, "repo_path", "") or ""
        worktree_path = getattr(run, "worktree_path", "") or ""
        branch = getattr(run, "branch", "") or ""
        attempt = getattr(run, "attempt", 0)
        max_attempts = getattr(run, "max_attempts", 3)
        error = getattr(run, "error", None)

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    number, title, state, elapsed, agents, tests,
                    repo_path, worktree_path, branch, attempt,
                    max_attempts, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    title = excluded.title,
                    state = excluded.state,
                    elapsed = excluded.elapsed,
                    agents = excluded.agents,
                    tests = excluded.tests,
                    repo_path = excluded.repo_path,
                    worktree_path = excluded.worktree_path,
                    branch = excluded.branch,
                    attempt = excluded.attempt,
                    max_attempts = excluded.max_attempts,
                    error = excluded.error,
                    created_at = excluded.created_at
                """,
                (
                    run.number,
                    run.title,
                    state_val,
                    elapsed,
                    agents,
                    tests,
                    repo_path,
                    worktree_path,
                    branch,
                    attempt,
                    max_attempts,
                    error,
                    float(created_at),
                ),
            )

            # Persist timeline events if present on run
            if hasattr(run, "events") and run.events:
                cursor = self._conn.execute(
                    "SELECT timestamp, actor, message FROM events WHERE run_number = ?",
                    (run.number,),
                )
                existing_events = {
                    (row["timestamp"], row["actor"], row["message"])
                    for row in cursor.fetchall()
                }
                for event in run.events:
                    key = (event.timestamp, event.actor, event.message)
                    if key not in existing_events:
                        self._conn.execute(
                            "INSERT INTO events (run_number, timestamp, actor, message) VALUES (?, ?, ?, ?)",
                            (run.number, event.timestamp, event.actor, event.message),
                        )
                        existing_events.add(key)

            # Persist artifacts if present on run
            if hasattr(run, "artifacts") and run.artifacts:
                cursor = self._conn.execute(
                    "SELECT name FROM artifacts WHERE run_number = ?",
                    (run.number,),
                )
                existing_artifacts = {row[0] for row in cursor.fetchall()}
                for art in run.artifacts:
                    if isinstance(art, Artifact):
                        art_name = art.name
                        art_content = art.content
                    elif hasattr(art, "name"):
                        art_name = art.name
                        art_content = getattr(art, "content", "")
                    else:
                        art_name = str(art)
                        art_content = ""

                    if art_name not in existing_artifacts:
                        self._conn.execute(
                            "INSERT INTO artifacts (run_number, name, content) VALUES (?, ?, ?)",
                            (run.number, art_name, art_content),
                        )
                        existing_artifacts.add(art_name)
                    elif art_content:
                        self._conn.execute(
                            "UPDATE artifacts SET content = ? WHERE run_number = ? AND name = ?",
                            (art_content, run.number, art_name),
                        )

            # Persist review if present on run
            if getattr(run, "review", None) is not None:
                self.save_review(run.number, run.review)

    def add_event(self, run_number: int, event: TimelineEvent) -> None:
        """Append a timeline event for the given run."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO events (run_number, timestamp, actor, message)
                VALUES (?, ?, ?, ?)
                """,
                (run_number, event.timestamp, event.actor, event.message),
            )

    def add_artifact(self, run_number: int, name: str, content: str) -> None:
        """Add or update an artifact for the given run."""
        with self._conn:
            cursor = self._conn.execute(
                "SELECT id FROM artifacts WHERE run_number = ? AND name = ?",
                (run_number, name),
            )
            row = cursor.fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE artifacts SET content = ? WHERE id = ?",
                    (content, row[0]),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO artifacts (run_number, name, content)
                    VALUES (?, ?, ?)
                    """,
                    (run_number, name, content),
                )

    def save_review(self, run_number: int, review: Review) -> None:
        """Store or update the review record for the given run."""
        passed_int = 1 if getattr(review, "passed", False) else 0
        confidence = float(getattr(review, "confidence", 0.0))
        summary = str(getattr(review, "summary", ""))

        findings_json = getattr(review, "findings_json", None)
        if not findings_json:
            findings = getattr(review, "findings", None)
            findings_json = json.dumps(findings if findings is not None else [])

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO reviews (run_number, passed, confidence, summary, findings_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_number) DO UPDATE SET
                    passed = excluded.passed,
                    confidence = excluded.confidence,
                    summary = excluded.summary,
                    findings_json = excluded.findings_json
                """,
                (run_number, passed_int, confidence, summary, findings_json),
            )

    def get_run(self, number: int) -> Run | None:
        """Retrieve a run by its number, fully loading events, artifacts, and review."""
        cursor = self._conn.execute(
            """
            SELECT
                number, title, state, elapsed, agents, tests,
                repo_path, worktree_path, branch, attempt,
                max_attempts, error, created_at
            FROM runs
            WHERE number = ?
            """,
            (number,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        # Load timeline events
        events_cur = self._conn.execute(
            """
            SELECT timestamp, actor, message
            FROM events
            WHERE run_number = ?
            ORDER BY id ASC
            """,
            (number,),
        )
        events = [
            TimelineEvent(
                timestamp=ev["timestamp"],
                actor=ev["actor"],
                message=ev["message"],
            )
            for ev in events_cur.fetchall()
        ]

        # Load artifacts
        artifacts_cur = self._conn.execute(
            """
            SELECT name, content
            FROM artifacts
            WHERE run_number = ?
            ORDER BY id ASC
            """,
            (number,),
        )
        artifacts: list[Artifact] = [
            Artifact(
                name=art["name"],
                path=art["name"],
                content=art["content"],
                size=len(art["content"]),
            )
            for art in artifacts_cur.fetchall()
        ]

        # Load review
        review_cur = self._conn.execute(
            """
            SELECT passed, confidence, summary, findings_json
            FROM reviews
            WHERE run_number = ?
            """,
            (number,),
        )
        rev_row = review_cur.fetchone()
        review: Review | None = None
        if rev_row is not None:
            review = Review(
                passed=bool(rev_row["passed"]),
                confidence=float(rev_row["confidence"]),
                summary=rev_row["summary"],
                findings_json=rev_row["findings_json"],
                run_number=number,
            )

        raw_state = row["state"]
        try:
            state = RunState(raw_state)
        except ValueError:
            state = raw_state

        run_kwargs: dict[str, Any] = {
            "number": row["number"],
            "title": row["title"],
            "state": state,
            "elapsed": row["elapsed"] or "00:00",
            "agents": row["agents"] or "0 / 4",
            "tests": row["tests"] or "pending",
            "events": events,
            "artifacts": artifacts,
            "repo_path": row["repo_path"] or "",
            "worktree_path": row["worktree_path"] or "",
            "branch": row["branch"] or "",
            "attempt": row["attempt"],
            "max_attempts": row["max_attempts"],
            "review": review,
            "error": row["error"],
            "created_at": row["created_at"],
        }

        # Filter kwargs to only fields accepted by Run dataclass
        if hasattr(Run, "__dataclass_fields__"):
            valid_fields = set(Run.__dataclass_fields__.keys())
            run_kwargs = {k: v for k, v in run_kwargs.items() if k in valid_fields}

        return Run(**run_kwargs)

    def list_runs(self) -> list[Run]:
        """Return all persisted runs ordered by number descending, fully hydrated."""
        cursor = self._conn.execute("SELECT number FROM runs ORDER BY number DESC")
        numbers = [row[0] for row in cursor.fetchall()]
        runs: list[Run] = []
        for num in numbers:
            run = self.get_run(num)
            if run is not None:
                runs.append(run)
        return runs

    def get_review(self, run_number: int) -> Review | None:
        """Fetch the review for a specific run, if present."""
        cursor = self._conn.execute(
            "SELECT passed, confidence, summary, findings_json FROM reviews WHERE run_number = ?",
            (run_number,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return Review(
            passed=bool(row["passed"]),
            confidence=float(row["confidence"]),
            summary=row["summary"],
            findings_json=row["findings_json"],
            run_number=run_number,
        )

    def get_artifacts(self, run_number: int) -> list[Artifact]:
        """Fetch all artifacts for a specific run."""
        cursor = self._conn.execute(
            "SELECT name, content FROM artifacts WHERE run_number = ? ORDER BY id ASC",
            (run_number,),
        )
        return [
            Artifact(
                name=row["name"],
                path=row["name"],
                content=row["content"],
                size=len(row["content"]),
            )
            for row in cursor.fetchall()
        ]

    def get_events(self, run_number: int) -> list[TimelineEvent]:
        """Fetch all timeline events for a specific run."""
        cursor = self._conn.execute(
            "SELECT timestamp, actor, message FROM events WHERE run_number = ? ORDER BY id ASC",
            (run_number,),
        )
        return [
            TimelineEvent(
                timestamp=row["timestamp"],
                actor=row["actor"],
                message=row["message"],
            )
            for row in cursor.fetchall()
        ]

    def delete_run(self, number: int) -> bool:
        """Delete a run and all associated events, artifacts, and reviews."""
        with self._conn:
            self._conn.execute("DELETE FROM events WHERE run_number = ?", (number,))
            self._conn.execute("DELETE FROM artifacts WHERE run_number = ?", (number,))
            self._conn.execute("DELETE FROM reviews WHERE run_number = ?", (number,))
            cursor = self._conn.execute("DELETE FROM runs WHERE number = ?", (number,))
            return cursor.rowcount > 0

    def count_runs(self) -> int:
        """Return the count of runs currently in the database."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM runs")
        return int(cursor.fetchone()[0])

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


__all__ = [
    "RunStore",
    "DEFAULT_DB_PATH",
    "DEFAULT_DB_DIR",
]
