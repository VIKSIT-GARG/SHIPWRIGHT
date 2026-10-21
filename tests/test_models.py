from shipwright.models import (
    Artifact,
    Issue,
    Repository,
    Review,
    Run,
    RunState,
    TimelineEvent,
    fixture_runs,
)


def test_fixture_runs_have_stable_demo_data() -> None:
    runs = fixture_runs()

    assert isinstance(runs, list)
    assert [run.number for run in runs] == [1842, 1839, 1828]
    assert runs[0].state is RunState.VERIFYING
    assert runs[0].tests == "42 passed"
    assert len(runs[0].events) == 4


def test_run_state_enum_members() -> None:
    expected = [
        "QUEUED",
        "INVESTIGATING",
        "PLANNED",
        "IMPLEMENTING",
        "VERIFYING",
        "REPAIRING",
        "REVIEWING",
        "READY_FOR_APPROVAL",
        "EXPORTED",
        "BLOCKED",
        "CANCELLED",
    ]
    assert [s.value for s in RunState] == expected


def test_timeline_event_creation() -> None:
    event = TimelineEvent(timestamp="10:00:00", actor="tester", message="test started")
    assert event.timestamp == "10:00:00"
    assert event.actor == "tester"
    assert event.message == "test started"


def test_issue_defaults_and_fields() -> None:
    issue = Issue(number=42, title="Bug in auth", body="Tokens expire too fast")
    assert issue.number == 42
    assert issue.title == "Bug in auth"
    assert issue.body == "Tokens expire too fast"
    assert issue.source == "manual"
    assert issue.labels == []
    assert issue.url == ""

    custom_issue = Issue(
        number=43,
        title="Custom",
        body="Custom body",
        source="github",
        labels=["bug", "security"],
        url="https://github.com/org/repo/issues/43",
    )
    assert custom_issue.source == "github"
    assert custom_issue.labels == ["bug", "security"]
    assert custom_issue.url == "https://github.com/org/repo/issues/43"


def test_repository_defaults_and_fields() -> None:
    repo = Repository(root="/tmp/repo")
    assert repo.root == "/tmp/repo"
    assert repo.default_branch == "main"
    assert repo.test_cmd is None

    repo_custom = Repository(root="/tmp/repo2", default_branch="develop", test_cmd="pytest -v")
    assert repo_custom.default_branch == "develop"
    assert repo_custom.test_cmd == "pytest -v"


def test_artifact_defaults_and_fields() -> None:
    artifact = Artifact(name="diff.patch", path="patches/diff.patch")
    assert artifact.name == "diff.patch"
    assert artifact.path == "patches/diff.patch"
    assert artifact.content == ""
    assert artifact.checksum == ""
    assert artifact.size == 0
    assert artifact.artifact_type == "text"


def test_review_defaults_and_fields() -> None:
    review = Review(passed=True, confidence=0.95, summary="Looks good")
    assert review.passed is True
    assert review.confidence == 0.95
    assert review.summary == "Looks good"
    assert review.findings == []


def test_run_defaults_and_fields() -> None:
    run = Run(number=101, title="New run", state=RunState.QUEUED)
    assert run.number == 101
    assert run.title == "New run"
    assert run.state is RunState.QUEUED
    assert run.elapsed == "00:00"
    assert run.agents == "0 / 4"
    assert run.tests == "pending"
    assert run.events == []
    assert run.artifacts == []
    assert run.issue is None
    assert run.repo_path == ""
    assert run.worktree_path == ""
    assert run.branch == ""
    assert run.attempt == 0
    assert run.max_attempts == 3
    assert run.review is None
    assert run.error is None


def test_models_use_slots() -> None:
    event = TimelineEvent("00:00:00", "test", "msg")
    assert hasattr(event, "__slots__")

    issue = Issue(1, "title", "body")
    assert hasattr(issue, "__slots__")

    repo = Repository("/root")
    assert hasattr(repo, "__slots__")

    artifact = Artifact("a", "p")
    assert hasattr(artifact, "__slots__")

    review = Review(True, 1.0, "ok")
    assert hasattr(review, "__slots__")

    run = Run(1, "title", RunState.QUEUED)
    assert hasattr(run, "__slots__")
