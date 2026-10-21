"""Unit and integration tests for shipwright.orchestrator.Orchestrator.

Tests cover:
- End-to-end loop with MockProvider simulating investigator, implementer, and reviewer.
- Temporary directory with a real Git repository and isolated worktree.
- State transitions: QUEUED -> INVESTIGATING -> PLANNED -> IMPLEMENTING -> VERIFYING -> REVIEWING -> READY_FOR_APPROVAL.
- Persistence of timeline events, artifacts, and reviews in RunStore.
- Repair loop with self-correction on test failure.
- Blocked runs on exhausted retry attempts and reviewer rejection.
- Cancellation handling during run execution and for existing stored runs.
- EventBus event broadcasting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence
from unittest.mock import MagicMock

import pytest

from shipwright.events import (
    AGENT_EVENT,
    ARTIFACT_ADDED,
    RUN_COMPLETED,
    RUN_STATE_CHANGED,
    TEST_OUTPUT,
    Event,
    EventBus,
)
from shipwright.models import Artifact, Issue, Review, Run, RunState, TimelineEvent
from shipwright.orchestrator import Orchestrator
from shipwright.providers import Message, Provider, ProviderResponse
from shipwright.runner import CommandExecution, CommandRunner
from shipwright.store import RunStore
from shipwright.workspace import WorkspaceManager


class MockProvider(Provider):
    """Mock LLM Provider conforming to the Provider protocol.

    Sequences through pre-configured responses or repeats the final response.
    Records all completed calls and messages for inspection.
    """

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        model: str = "mock-orchestrator-model",
    ) -> None:
        self.model = model
        self.responses: list[str] = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self._index: int = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        self.calls.append({
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self._index < len(self.responses):
            resp = self.responses[self._index]
            self._index += 1
        elif self.responses:
            resp = self.responses[-1]
        else:
            resp = "{}"
        return ProviderResponse(content=resp, model=self.model, finish_reason="stop")


@pytest.fixture(autouse=True)
def prevent_bytecode_cache() -> None:
    """Prevent Python bytecode cache collisions during rapid worktree test iterations."""
    old_val = os.environ.get("PYTHONDONTWRITEBYTECODE")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    yield
    if old_val is not None:
        os.environ["PYTHONDONTWRITEBYTECODE"] = old_val
    else:
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create and configure a clean Git repository in a temporary directory.

    Initializes a repository with a buggy module (`calc.py`) and a unit test
    (`test_calc.py`) that fails on the initial commit.
    """
    repo = tmp_path / "test_repo"
    repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Shipwright Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@shipwright.local"], cwd=repo, check=True)

    # calc.py has a bug: subtraction instead of addition
    (repo / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )

    # test_calc.py expects add(2, 3) == 5
    (repo / "test_calc.py").write_text(
        "import unittest\n"
        "from calc import add\n\n"
        "class TestCalculator(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with buggy calc"], cwd=repo, check=True)
    return repo


def test_orchestrator_initialization(tmp_path: Path) -> None:
    """Test Orchestrator initializes defaults and custom dependencies properly."""
    store = RunStore()
    event_bus = EventBus()
    provider = MockProvider()

    orchestrator = Orchestrator(
        repo_root=str(tmp_path),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    assert orchestrator.repo_root == tmp_path.resolve()
    assert orchestrator.provider is provider
    assert orchestrator.store is store
    assert orchestrator.event_bus is event_bus
    assert isinstance(orchestrator.workspace_mgr, WorkspaceManager)
    assert isinstance(orchestrator.runner, CommandRunner)


def test_orchestrator_end_to_end_loop(git_repo: Path) -> None:
    """Test Orchestrator end-to-end loop:

    - MockProvider simulating investigator, implementer, reviewer responses.
    - Setup a temporary directory with a git repo.
    - Run an issue through Orchestrator.run_issue().
    - Verify transitions: QUEUED -> INVESTIGATING -> PLANNED -> IMPLEMENTING -> VERIFYING -> REVIEWING -> READY_FOR_APPROVAL.
    - Verify artifacts and timeline events are recorded in RunStore.
    """
    # 1. Investigator response
    investigator_payload = json.dumps({
        "summary": "Fix subtraction bug in calc.py add function",
        "relevant_files": ["calc.py"],
        "suggested_approach": "Change operator from - to + in calc.py",
    })

    # 2. Implementer response: modifies calc.py with fixed implementation
    implementer_payload = json.dumps({
        "files": [
            {
                "path": "calc.py",
                "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
            }
        ]
    })

    # 3. Reviewer response: approves the patch and test results
    reviewer_payload = json.dumps({
        "passed": True,
        "confidence": 0.98,
        "summary": "Fix correctly implements addition and satisfies unit test requirements.",
        "findings": ["Clean implementation", "No regressions detected"],
    })

    provider = MockProvider([
        investigator_payload,
        implementer_payload,
        reviewer_payload,
    ])

    store = RunStore()
    event_bus = EventBus()

    # Capture all state transitions broadcast on the event bus
    recorded_transitions: list[RunState] = []
    event_bus.subscribe(
        RUN_STATE_CHANGED,
        lambda event: recorded_transitions.append(RunState(event.data["state"])),
    )

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(
        number=42,
        title="Fix addition operator in calc.py",
        body="calc.add(2, 3) returns -1 instead of 5.",
    )

    # Run the issue through Orchestrator
    run = orchestrator.run_issue(issue, max_attempts=3)

    # Verify run metadata
    assert run.number == 42
    assert run.title == "Fix addition operator in calc.py"
    assert run.state == RunState.READY_FOR_APPROVAL
    assert run.attempt == 1
    assert "OK" in run.tests or "passed" in run.tests.lower()
    assert run.review is not None
    assert run.review.passed is True
    assert run.review.confidence == 0.98

    # Verify the exact sequence of state transitions:
    # QUEUED -> INVESTIGATING -> PLANNED -> IMPLEMENTING -> VERIFYING -> REVIEWING -> READY_FOR_APPROVAL
    expected_transitions = [
        RunState.QUEUED,
        RunState.INVESTIGATING,
        RunState.PLANNED,
        RunState.IMPLEMENTING,
        RunState.VERIFYING,
        RunState.REVIEWING,
        RunState.READY_FOR_APPROVAL,
    ]
    assert recorded_transitions == expected_transitions

    # Verify artifacts and timeline events are recorded in RunStore
    stored_run = store.get_run(42)
    assert stored_run is not None
    assert stored_run.state == RunState.READY_FOR_APPROVAL
    assert stored_run.title == "Fix addition operator in calc.py"

    # Verify timeline events recorded in RunStore
    assert len(stored_run.events) >= 7
    event_actors = [e.actor for e in stored_run.events]
    assert "orchestrator" in event_actors
    assert "investigator" in event_actors
    assert "implementer" in event_actors
    assert "verifier" in event_actors
    assert "reviewer" in event_actors

    event_messages = [e.message for e in stored_run.events]
    assert any("queued" in msg.lower() for msg in event_messages)
    assert any("investigating" in msg.lower() or "analyzing" in msg.lower() for msg in event_messages)
    assert any("plan" in msg.lower() for msg in event_messages)
    assert any("modifications" in msg.lower() for msg in event_messages)
    assert any("tests" in msg.lower() for msg in event_messages)
    assert any("review" in msg.lower() for msg in event_messages)
    assert any("ready for approval" in msg.lower() for msg in event_messages)

    # Verify artifacts recorded in RunStore
    stored_artifact_map: dict[str, str] = {
        art.name: art.content for art in stored_run.artifacts
    }
    assert "plan.json" in stored_artifact_map
    assert "patch.diff" in stored_artifact_map
    assert "diff.patch" in stored_artifact_map
    assert "test_output.txt" in stored_artifact_map
    assert "review.md" in stored_artifact_map

    # Validate contents of recorded artifacts
    plan_data = json.loads(stored_artifact_map["plan.json"])
    assert plan_data["summary"] == "Fix subtraction bug in calc.py add function"
    assert "calc.py" in plan_data["relevant_files"]

    assert "OK" in stored_artifact_map["test_output.txt"] or "Ran 1 test" in stored_artifact_map["test_output.txt"]
    assert "PASSED" in stored_artifact_map["review.md"]
    assert "0.98" in stored_artifact_map["review.md"]

    # Verify review recorded in RunStore
    assert stored_run.review is not None
    assert stored_run.review.passed is True
    assert stored_run.review.confidence == 0.98
    assert "addition" in stored_run.review.summary.lower()
    assert "Clean implementation" in stored_run.review.findings


def test_orchestrator_repair_loop(git_repo: Path) -> None:
    """Test multi-attempt self-correction repair loop in a real Git worktree.

    Attempt 1: implementer introduces incorrect fix (* instead of +) -> tests fail -> REPAIRING.
    Attempt 2: implementer introduces correct fix (+ operator) -> tests pass -> REVIEWING -> READY_FOR_APPROVAL.
    """
    # 1. Investigator
    inv_payload = json.dumps({
        "summary": "Diagnose arithmetic operator in calc.py",
        "relevant_files": ["calc.py"],
        "suggested_approach": "Repair addition operator",
    })

    # 2. Implementer attempt 1 (fails test: 2 * 3 == 6 != 5)
    imp1_payload = json.dumps({
        "files": [
            {
                "path": "calc.py",
                "content": "def add(a: int, b: int) -> int:\n    return a * b\n",
            }
        ]
    })

    # 3. Implementer attempt 2 (passes test: 2 + 3 == 5)
    imp2_payload = json.dumps({
        "files": [
            {
                "path": "calc.py",
                "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
            }
        ]
    })

    # 4. Reviewer approves
    rev_payload = json.dumps({
        "passed": True,
        "confidence": 0.95,
        "summary": "Repaired and verified successfully on attempt 2.",
        "findings": ["Tests pass after repair"],
    })

    provider = MockProvider([
        inv_payload,
        imp1_payload,
        imp2_payload,
        rev_payload,
    ])

    store = RunStore()
    event_bus = EventBus()

    recorded_transitions: list[RunState] = []
    event_bus.subscribe(
        RUN_STATE_CHANGED,
        lambda event: recorded_transitions.append(RunState(event.data["state"])),
    )

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(
        number=43,
        title="Repair addition function",
        body="calc.add returns incorrect sum",
    )

    run = orchestrator.run_issue(issue, max_attempts=3)

    assert run.state == RunState.READY_FOR_APPROVAL
    assert run.attempt == 2

    # Transitions must include REPAIRING and a second cycle of IMPLEMENTING -> VERIFYING
    expected_transitions = [
        RunState.QUEUED,
        RunState.INVESTIGATING,
        RunState.PLANNED,
        RunState.IMPLEMENTING,
        RunState.VERIFYING,
        RunState.REPAIRING,
        RunState.IMPLEMENTING,
        RunState.VERIFYING,
        RunState.REVIEWING,
        RunState.READY_FOR_APPROVAL,
    ]
    assert recorded_transitions == expected_transitions

    # Verify RunStore recorded the run and events
    stored_run = store.get_run(43)
    assert stored_run is not None
    assert stored_run.state == RunState.READY_FOR_APPROVAL
    assert stored_run.attempt == 2
    assert any("repair mode" in ev.message.lower() for ev in stored_run.events)


def test_orchestrator_exhausted_attempts_blocked(git_repo: Path) -> None:
    """Test run transitions to BLOCKED when test verification attempts are exhausted."""
    inv_payload = json.dumps({
        "summary": "Fix add",
        "relevant_files": ["calc.py"],
        "suggested_approach": "Attempt fix",
    })

    # Implementer always returns code that fails the test
    failing_payload = json.dumps({
        "files": [
            {
                "path": "calc.py",
                "content": "def add(a: int, b: int) -> int:\n    return 0\n",
            }
        ]
    })

    provider = MockProvider([inv_payload, failing_payload])
    store = RunStore()
    event_bus = EventBus()

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(number=44, title="Failing issue", body="Tests will not pass")
    run = orchestrator.run_issue(issue, max_attempts=2)

    assert run.state == RunState.BLOCKED
    assert run.attempt == 2
    assert "Tests failed after 2 attempts" in (run.error or "")

    stored_run = store.get_run(44)
    assert stored_run is not None
    assert stored_run.state == RunState.BLOCKED
    assert "Tests failed after 2 attempts" in (stored_run.error or "")


def test_orchestrator_review_rejected_blocked(git_repo: Path) -> None:
    """Test run transitions to BLOCKED if reviewer rejects the proposed patch."""
    inv_payload = json.dumps({
        "summary": "Fix add",
        "relevant_files": ["calc.py"],
        "suggested_approach": "Change to add",
    })
    imp_payload = json.dumps({
        "files": [
            {
                "path": "calc.py",
                "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
            }
        ]
    })
    # Reviewer rejects changes due to unacceptable findings
    rev_payload = json.dumps({
        "passed": False,
        "confidence": 0.99,
        "summary": "Security policy violation: unacceptable side-effect in calc module.",
        "findings": ["Policy violation", "Unsanitized operation"],
    })

    provider = MockProvider([inv_payload, imp_payload, rev_payload])
    store = RunStore()
    event_bus = EventBus()

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(number=45, title="Security review issue", body="Check review rejection")
    run = orchestrator.run_issue(issue, max_attempts=2)

    assert run.state == RunState.BLOCKED
    assert run.review is not None
    assert run.review.passed is False
    assert "Security policy violation" in (run.error or "")

    stored_run = store.get_run(45)
    assert stored_run is not None
    assert stored_run.state == RunState.BLOCKED
    assert stored_run.review is not None
    assert stored_run.review.passed is False


def test_orchestrator_cancellation(git_repo: Path) -> None:
    """Test cancelling an active run mid-flight transitions to CANCELLED state."""
    provider = MockProvider()
    store = RunStore()
    event_bus = EventBus()

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(number=46, title="Cancel mid-flight", body="Testing cancellation")

    # Hook investigator to cancel the run while it's in progress
    def cancel_during_investigation(repo_files: Any, issue: Any, worktree_path: Any) -> dict[str, Any]:
        orchestrator.cancel_run(46)
        return {"summary": "plan", "relevant_files": [], "suggested_approach": ""}

    orchestrator.investigator.investigate = cancel_during_investigation

    run = orchestrator.run_issue(issue)

    assert run.state == RunState.CANCELLED
    stored_run = store.get_run(46)
    assert stored_run is not None
    assert stored_run.state == RunState.CANCELLED


def test_orchestrator_cancel_run_external(tmp_path: Path) -> None:
    """Test cancelling an existing stored run directly via cancel_run."""
    store = RunStore()
    run = Run(
        number=200,
        title="Pre-existing stored run",
        state=RunState.QUEUED,
    )
    store.save_run(run)

    orchestrator = Orchestrator(
        repo_root=str(tmp_path),
        provider=MockProvider(),
        store=store,
    )

    orchestrator.cancel_run(200)

    stored = store.get_run(200)
    assert stored is not None
    assert stored.state == RunState.CANCELLED


def test_orchestrator_event_bus_topics_emitted(git_repo: Path) -> None:
    """Test that all standard topics are published during a successful run."""
    inv_payload = json.dumps({
        "summary": "Fix add",
        "relevant_files": ["calc.py"],
        "suggested_approach": "Fix add",
    })
    imp_payload = json.dumps({
        "files": [{"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}]
    })
    rev_payload = json.dumps({
        "passed": True,
        "confidence": 0.95,
        "summary": "All good",
        "findings": [],
    })

    provider = MockProvider([inv_payload, imp_payload, rev_payload])
    store = RunStore()
    event_bus = EventBus()

    emitted_topics: list[str] = []
    event_bus.subscribe("*", lambda e: emitted_topics.append(e.topic))

    orchestrator = Orchestrator(
        repo_root=str(git_repo),
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    issue = Issue(number=47, title="Event emission test", body="Verify events")
    run = orchestrator.run_issue(issue)

    assert run.state == RunState.READY_FOR_APPROVAL
    assert RUN_STATE_CHANGED in emitted_topics
    assert AGENT_EVENT in emitted_topics
    assert ARTIFACT_ADDED in emitted_topics
    assert TEST_OUTPUT in emitted_topics
    assert RUN_COMPLETED in emitted_topics


def test_orchestrator_repo_source_and_repository(git_repo: Path) -> None:
    """Test that repo_source is stored and repository is resolved."""
    orchestrator = Orchestrator(repo_root=str(git_repo), provider=MockProvider())
    assert orchestrator.repo_source == str(git_repo)
    assert orchestrator.repository is not None
    assert orchestrator.repository.root == str(git_repo.resolve())
    assert orchestrator.repo_root == git_repo.resolve()


def test_orchestrator_repo_override(git_repo: Path, tmp_path: Path) -> None:
    """Test run_issue with repo_override resolving to a different repository."""
    override_repo = tmp_path / "override_repo"
    override_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=override_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=override_repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@local"], cwd=override_repo, check=True)
    (override_repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
    (override_repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(2, 3), 5)\nif __name__ == '__main__':\n    unittest.main()\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=override_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=override_repo, check=True)

    provider = MockProvider([
        json.dumps({"summary": "plan", "relevant_files": ["calc.py"], "suggested_approach": "fix"}),
        json.dumps({"files": [{"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}]}),
        json.dumps({"passed": True, "confidence": 0.9, "summary": "looks good", "findings": []}),
    ])
    store = RunStore()
    orchestrator = Orchestrator(repo_root=str(git_repo), provider=provider, store=store)

    issue = Issue(number=48, title="Override issue", body="Testing repo_override")
    run = orchestrator.run_issue(issue, repo_override=str(override_repo))

    assert run.state == RunState.READY_FOR_APPROVAL
    assert run.repo_path == str(override_repo.resolve())
    assert str(override_repo.resolve()) in run.worktree_path

    stored_run = store.get_run(48)
    assert stored_run is not None
    assert stored_run.repo_path == str(override_repo.resolve())


def test_orchestrator_inferred_repo_from_issue_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that issue.url triggers inference when repo_source is default '.'."""
    inferred_repo = tmp_path / "inferred_repo"
    inferred_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=inferred_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=inferred_repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@local"], cwd=inferred_repo, check=True)
    (inferred_repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
    (inferred_repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(2, 3), 5)\nif __name__ == '__main__':\n    unittest.main()\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=inferred_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=inferred_repo, check=True)

    import shipwright.orchestrator
    orig_resolve = shipwright.orchestrator.resolve_repo

    def mock_resolve(src: str, **kwargs: Any) -> Any:
        if "mock-org/mock-repo" in src:
            from shipwright.models import Repository
            return Repository(root=str(inferred_repo.resolve()), default_branch="main")
        return orig_resolve(src, **kwargs)

    monkeypatch.setattr(shipwright.orchestrator, "resolve_repo", mock_resolve)

    provider = MockProvider([
        json.dumps({"summary": "plan", "relevant_files": ["calc.py"], "suggested_approach": "fix"}),
        json.dumps({"files": [{"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}]}),
        json.dumps({"passed": True, "confidence": 0.9, "summary": "ok", "findings": []}),
    ])
    store = RunStore()
    orchestrator = Orchestrator(repo_root=".", provider=provider, store=store)

    issue = Issue(
        number=49,
        title="Inferred issue",
        body="From github url",
        url="https://github.com/mock-org/mock-repo/issues/49",
    )
    run = orchestrator.run_issue(issue)

    assert run.state == RunState.READY_FOR_APPROVAL
    assert run.repo_path == str(inferred_repo.resolve())
    assert str(inferred_repo.resolve()) in run.worktree_path

    stored_run = store.get_run(49)
    assert stored_run is not None
    assert stored_run.repo_path == str(inferred_repo.resolve())

