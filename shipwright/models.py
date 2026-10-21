"""Domain models for Shipwright runs, timeline events, artifacts, and reviews."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunState(StrEnum):
    QUEUED = "QUEUED"
    INVESTIGATING = "INVESTIGATING"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    REVIEWING = "REVIEWING"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    EXPORTED = "EXPORTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class TimelineEvent:
    timestamp: str
    actor: str
    message: str


@dataclass(slots=True)
class Issue:
    number: int
    title: str
    body: str
    source: str = "manual"
    labels: list[str] = field(default_factory=list)
    url: str = ""


@dataclass(slots=True)
class Repository:
    root: str
    default_branch: str = "main"
    test_cmd: str | None = None
    remote_url: str | None = None


@dataclass(slots=True)
class Artifact:
    name: str
    path: str = ""
    content: str = ""
    checksum: str = ""
    size: int = 0
    artifact_type: str = "text"

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class Review:
    passed: bool
    confidence: float
    summary: str
    findings: list[Any] = field(default_factory=list)
    findings_json: str = "[]"
    run_number: int | None = None

    def __post_init__(self) -> None:
        if self.findings and (self.findings_json == "[]" or not self.findings_json):
            object.__setattr__(self, "findings_json", json.dumps(self.findings))
        elif not self.findings and self.findings_json and self.findings_json != "[]":
            try:
                object.__setattr__(self, "findings", json.loads(self.findings_json))
            except Exception:
                object.__setattr__(self, "findings", [])


@dataclass(slots=True)
class Run:
    number: int
    title: str
    state: RunState
    elapsed: str = "00:00"
    agents: str = "0 / 4"
    tests: str = "pending"
    events: list[TimelineEvent] = field(default_factory=list)
    artifacts: list[Artifact | str] = field(default_factory=list)
    issue: Issue | None = None
    repo_path: str = ""
    worktree_path: str = ""
    branch: str = ""
    attempt: int = 0
    max_attempts: int = 3
    review: Review | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


def fixture_runs() -> list[Run]:
    return [
        Run(
            1842,
            "Fix flaky retry test",
            RunState.VERIFYING,
            "02:14",
            "3 / 3",
            "42 passed",
            [
                TimelineEvent("12:14:03", "investigator", "identified src/retry.py"),
                TimelineEvent("12:14:19", "implementer", "patched backoff boundary"),
                TimelineEvent("12:15:02", "verifier", "running pytest tests/retry"),
                TimelineEvent("12:15:18", "reviewer", "waiting for verifier"),
            ],
            ["git diff · 18 lines", "pytest log · 42 passed", "review report · pending"],
        ),
        Run(1839, "Handle null config", RunState.REVIEWING, "08:51", "3 / 3", "18 passed"),
        Run(1828, "Pin dependency", RunState.READY_FOR_APPROVAL, "12:03", "3 / 3", "96 passed"),
    ]


__all__ = [
    "RunState",
    "TimelineEvent",
    "Issue",
    "Repository",
    "Artifact",
    "Review",
    "Run",
    "fixture_runs",
]
