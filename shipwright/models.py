from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class RunState(StrEnum):
    QUEUED = "QUEUED"
    INVESTIGATING = "INVESTIGATING"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    BLOCKED = "BLOCKED"
    EXPORTED = "EXPORTED"


@dataclass(slots=True)
class TimelineEvent:
    timestamp: str
    actor: str
    message: str


@dataclass(slots=True)
class Run:
    number: int
    title: str
    state: RunState
    elapsed: str
    agents: str
    tests: str
    events: list[TimelineEvent] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


def fixture_runs() -> Iterable[Run]:
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
