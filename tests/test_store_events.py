"""Tests for RunStore and EventBus in Shipwright.

Covers:
RunStore:
- in-memory database creation
- saving and retrieving a Run
- adding events to a run
- adding artifacts to a run
- saving and retrieving a review
- listing runs
EventBus:
- subscribe and publish
- standard topics
- unsubscribe
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
import pytest

from shipwright.events import (
    AGENT_EVENT,
    ARTIFACT_ADDED,
    RUN_COMPLETED,
    RUN_STATE_CHANGED,
    STANDARD_TOPICS,
    TEST_OUTPUT,
    Event,
    EventBus,
)
from shipwright.models import (
    Artifact,
    Review,
    Run,
    RunState,
    TimelineEvent,
)
from shipwright.store import RunStore


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def store() -> RunStore:
    """Provide a fresh in-memory RunStore instance for testing."""
    s = RunStore()
    yield s
    s.close()


@pytest.fixture
def sample_run() -> Run:
    """Provide a sample Run model instance."""
    return Run(
        number=42,
        title="Fix authentication timeout bug",
        state=RunState.INVESTIGATING,
        elapsed="01:30",
        agents="1 / 4",
        tests="12 passed",
        repo_path="/tmp/repo",
        worktree_path="/tmp/worktree/42",
        branch="shipwright/fix-42",
        attempt=1,
        max_attempts=3,
        error=None,
    )


# ==============================================================================
# RunStore Tests: In-Memory Database Creation
# ==============================================================================

class TestRunStoreInMemoryCreation:
    """Tests for in-memory database initialization and schema setup."""

    def test_default_init_is_in_memory(self) -> None:
        """RunStore() without args should default to in-memory ':memory:'."""
        store = RunStore()
        try:
            assert store.db_path == ":memory:"
            # Ensure tables exist in sqlite_master
            cursor = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            assert {"runs", "events", "artifacts", "reviews"}.issubset(tables)
        finally:
            store.close()

    def test_explicit_memory_arg(self) -> None:
        """RunStore(':memory:') creates an in-memory database."""
        store = RunStore(":memory:")
        try:
            assert store.db_path == ":memory:"
            assert store.count_runs() == 0
        finally:
            store.close()

    def test_in_memory_isolation(self) -> None:
        """Two separate in-memory stores should be completely isolated."""
        store1 = RunStore()
        store2 = RunStore()
        try:
            run = Run(number=1, title="Isolation Test", state=RunState.QUEUED)
            store1.save_run(run)
            assert store1.get_run(1) is not None
            assert store2.get_run(1) is None
            assert store1.count_runs() == 1
            assert store2.count_runs() == 0
        finally:
            store1.close()
            store2.close()

    def test_context_manager_support(self) -> None:
        """RunStore supports context manager protocol and closes on exit."""
        with RunStore() as store:
            assert store.count_runs() == 0
            store.save_run(Run(number=100, title="Ctx Test", state=RunState.QUEUED))
            assert store.count_runs() == 1
        # Connection should be closed after exiting context manager
        with pytest.raises(Exception):
            store.conn.execute("SELECT 1")


# ==============================================================================
# RunStore Tests: Saving and Retrieving a Run
# ==============================================================================

class TestRunStoreSaveAndRetrieve:
    """Tests for saving and retrieving runs."""

    def test_save_and_retrieve_basic_run(self, store: RunStore, sample_run: Run) -> None:
        """Persist a run and verify all attributes are faithfully reconstructed."""
        store.save_run(sample_run)
        retrieved = store.get_run(sample_run.number)

        assert retrieved is not None
        assert retrieved.number == sample_run.number
        assert retrieved.title == sample_run.title
        assert retrieved.state == RunState.INVESTIGATING
        assert retrieved.elapsed == sample_run.elapsed
        assert retrieved.agents == sample_run.agents
        assert retrieved.tests == sample_run.tests
        assert retrieved.repo_path == sample_run.repo_path
        assert retrieved.worktree_path == sample_run.worktree_path
        assert retrieved.branch == sample_run.branch
        assert retrieved.attempt == sample_run.attempt
        assert retrieved.max_attempts == sample_run.max_attempts
        assert retrieved.error is None
        assert retrieved.events == []
        assert retrieved.artifacts == []
        assert retrieved.review is None

    def test_save_run_update_existing(self, store: RunStore, sample_run: Run) -> None:
        """Saving an updated Run should update the row instead of creating duplicate."""
        store.save_run(sample_run)
        assert store.count_runs() == 1

        # Modify fields and save again
        sample_run.state = RunState.VERIFYING
        sample_run.elapsed = "03:45"
        sample_run.attempt = 2
        sample_run.tests = "48 passed"
        sample_run.error = "Minor warning"
        store.save_run(sample_run)

        assert store.count_runs() == 1
        updated = store.get_run(sample_run.number)
        assert updated is not None
        assert updated.state == RunState.VERIFYING
        assert updated.elapsed == "03:45"
        assert updated.attempt == 2
        assert updated.tests == "48 passed"
        assert updated.error == "Minor warning"

    def test_get_nonexistent_run_returns_none(self, store: RunStore) -> None:
        """Retrieving a non-existent run number returns None."""
        assert store.get_run(99999) is None

    def test_run_state_enum_preservation(self, store: RunStore) -> None:
        """States are correctly stored as text and restored as RunState instances."""
        for state in RunState:
            run = Run(number=int(hash(state.value) % 100000), title=f"Run {state}", state=state)
            store.save_run(run)
            retrieved = store.get_run(run.number)
            assert retrieved is not None
            assert retrieved.state == state
            assert isinstance(retrieved.state, RunState)


# ==============================================================================
# RunStore Tests: Adding Events to a Run
# ==============================================================================

class TestRunStoreEvents:
    """Tests for adding timeline events to a run."""

    def test_add_event_method(self, store: RunStore, sample_run: Run) -> None:
        """add_event should append timeline events to the given run."""
        store.save_run(sample_run)

        event1 = TimelineEvent(timestamp="10:00:01", actor="investigator", message="analyzed codebase")
        event2 = TimelineEvent(timestamp="10:00:15", actor="implementer", message="applied patch")

        store.add_event(sample_run.number, event1)
        store.add_event(sample_run.number, event2)

        # Verify via get_events
        events = store.get_events(sample_run.number)
        assert len(events) == 2
        assert events[0].timestamp == "10:00:01"
        assert events[0].actor == "investigator"
        assert events[0].message == "analyzed codebase"
        assert events[1].timestamp == "10:00:15"
        assert events[1].actor == "implementer"
        assert events[1].message == "applied patch"

        # Verify via get_run hydration
        hydrated_run = store.get_run(sample_run.number)
        assert hydrated_run is not None
        assert len(hydrated_run.events) == 2
        assert hydrated_run.events[0] == event1
        assert hydrated_run.events[1] == event2

    def test_save_run_with_embedded_events(self, store: RunStore) -> None:
        """save_run persists events defined on the Run object and deduplicates on re-save."""
        run = Run(
            number=77,
            title="Embedded Events",
            state=RunState.PLANNED,
            events=[
                TimelineEvent("11:00:00", "actor_a", "step 1"),
                TimelineEvent("11:01:00", "actor_b", "step 2"),
            ],
        )
        store.save_run(run)

        retrieved = store.get_run(77)
        assert retrieved is not None
        assert len(retrieved.events) == 2
        assert retrieved.events[0].actor == "actor_a"
        assert retrieved.events[1].actor == "actor_b"

        # Re-saving the same run should not duplicate events
        store.save_run(retrieved)
        after_resave = store.get_run(77)
        assert after_resave is not None
        assert len(after_resave.events) == 2

    def test_events_for_nonexistent_run_returns_empty_list(self, store: RunStore) -> None:
        """get_events for unknown run returns an empty list."""
        assert store.get_events(404) == []


# ==============================================================================
# RunStore Tests: Adding Artifacts to a Run
# ==============================================================================

class TestRunStoreArtifacts:
    """Tests for adding artifacts to a run."""

    def test_add_artifact_method(self, store: RunStore, sample_run: Run) -> None:
        """add_artifact persists artifact name and content for a run."""
        store.save_run(sample_run)

        store.add_artifact(sample_run.number, "diff.patch", "--- a\n+++ b\n+test\n")
        store.add_artifact(sample_run.number, "test_output.log", "================ 12 passed ================")

        artifacts = store.get_artifacts(sample_run.number)
        assert len(artifacts) == 2
        names = [a.name for a in artifacts]
        assert "diff.patch" in names
        assert "test_output.log" in names

        diff_art = next(a for a in artifacts if a.name == "diff.patch")
        assert diff_art.content == "--- a\n+++ b\n+test\n"
        assert diff_art.size == len("--- a\n+++ b\n+test\n")

        # Verify hydrated on get_run
        hydrated = store.get_run(sample_run.number)
        assert hydrated is not None
        assert len(hydrated.artifacts) == 2

    def test_add_artifact_updates_existing_content(self, store: RunStore, sample_run: Run) -> None:
        """Adding an artifact with the same name updates its content."""
        store.save_run(sample_run)

        store.add_artifact(sample_run.number, "summary.txt", "v1 content")
        assert store.get_artifacts(sample_run.number)[0].content == "v1 content"

        store.add_artifact(sample_run.number, "summary.txt", "v2 updated content")
        artifacts = store.get_artifacts(sample_run.number)
        assert len(artifacts) == 1
        assert artifacts[0].content == "v2 updated content"

    def test_save_run_with_embedded_artifacts(self, store: RunStore) -> None:
        """save_run persists artifacts attached to the Run instance."""
        art1 = Artifact(name="patch.diff", path="patch.diff", content="diff data")
        art2 = "review_summary.md"  # supports string or Artifact
        run = Run(
            number=88,
            title="Artifacts Test",
            state=RunState.IMPLEMENTING,
            artifacts=[art1, art2],
        )
        store.save_run(run)

        retrieved = store.get_run(88)
        assert retrieved is not None
        assert len(retrieved.artifacts) == 2
        art_names = [a.name if isinstance(a, Artifact) else str(a) for a in retrieved.artifacts]
        assert "patch.diff" in art_names
        assert "review_summary.md" in art_names

    def test_artifacts_for_nonexistent_run_returns_empty_list(self, store: RunStore) -> None:
        """get_artifacts for unknown run returns empty list."""
        assert store.get_artifacts(404) == []


# ==============================================================================
# RunStore Tests: Saving and Retrieving a Review
# ==============================================================================

class TestRunStoreReviews:
    """Tests for saving and retrieving reviews."""

    def test_save_and_retrieve_review(self, store: RunStore, sample_run: Run) -> None:
        """save_review stores review details and get_review retrieves them."""
        store.save_run(sample_run)

        review = Review(
            passed=True,
            confidence=0.95,
            summary="Code change looks very clean and passes all checks.",
            findings=["Correct boundary check", "No regressions"],
            run_number=sample_run.number,
        )
        store.save_review(sample_run.number, review)

        retrieved_review = store.get_review(sample_run.number)
        assert retrieved_review is not None
        assert retrieved_review.passed is True
        assert retrieved_review.confidence == 0.95
        assert retrieved_review.summary == "Code change looks very clean and passes all checks."
        assert retrieved_review.findings == ["Correct boundary check", "No regressions"]
        assert retrieved_review.run_number == sample_run.number

        # Also verify hydrated on get_run
        hydrated_run = store.get_run(sample_run.number)
        assert hydrated_run is not None
        assert hydrated_run.review is not None
        assert hydrated_run.review.passed is True
        assert hydrated_run.review.confidence == 0.95
        assert hydrated_run.review.findings == ["Correct boundary check", "No regressions"]

    def test_save_review_update(self, store: RunStore, sample_run: Run) -> None:
        """Calling save_review again updates the review rather than inserting a duplicate."""
        store.save_run(sample_run)

        rev1 = Review(passed=False, confidence=0.4, summary="Failed initial check", findings=["Syntax error"])
        store.save_review(sample_run.number, rev1)

        rev2 = Review(passed=True, confidence=0.9, summary="Passed after repair", findings=["All fixed"])
        store.save_review(sample_run.number, rev2)

        final_rev = store.get_review(sample_run.number)
        assert final_rev is not None
        assert final_rev.passed is True
        assert final_rev.confidence == 0.9
        assert final_rev.summary == "Passed after repair"
        assert final_rev.findings == ["All fixed"]

    def test_get_nonexistent_review_returns_none(self, store: RunStore) -> None:
        """get_review returns None if no review exists for the run."""
        assert store.get_review(12345) is None


# ==============================================================================
# RunStore Tests: Listing Runs
# ==============================================================================

class TestRunStoreListingRuns:
    """Tests for listing runs in the database."""

    def test_list_runs_empty(self, store: RunStore) -> None:
        """list_runs returns an empty list on a fresh database."""
        assert store.list_runs() == []

    def test_list_runs_ordering_and_hydration(self, store: RunStore) -> None:
        """list_runs returns all runs ordered by number descending and fully hydrated."""
        r1 = Run(number=10, title="Run 10", state=RunState.QUEUED)
        r2 = Run(
            number=20,
            title="Run 20",
            state=RunState.IMPLEMENTING,
            events=[TimelineEvent("12:00:00", "actor", "step")],
        )
        r3 = Run(
            number=30,
            title="Run 30",
            state=RunState.READY_FOR_APPROVAL,
            review=Review(passed=True, confidence=0.88, summary="Ready"),
        )

        store.save_run(r1)
        store.save_run(r2)
        store.save_run(r3)

        runs = store.list_runs()
        assert len(runs) == 3
        # Should be ordered by number descending: 30, 20, 10
        assert [r.number for r in runs] == [30, 20, 10]

        # Verify hydration
        run30 = runs[0]
        assert run30.review is not None
        assert run30.review.passed is True
        assert run30.review.summary == "Ready"

        run20 = runs[1]
        assert len(run20.events) == 1
        assert run20.events[0].message == "step"

        run10 = runs[2]
        assert run10.state == RunState.QUEUED

    def test_delete_run_cascades(self, store: RunStore) -> None:
        """delete_run removes the run and all associated records."""
        run = Run(
            number=999,
            title="To be deleted",
            state=RunState.BLOCKED,
            events=[TimelineEvent("00:00:00", "test", "msg")],
            review=Review(passed=False, confidence=0.1, summary="Blocked"),
        )
        store.save_run(run)
        store.add_artifact(999, "dump.txt", "some dump")

        assert store.get_run(999) is not None
        assert store.count_runs() == 1

        deleted = store.delete_run(999)
        assert deleted is True
        assert store.get_run(999) is None
        assert store.get_events(999) == []
        assert store.get_artifacts(999) == []
        assert store.get_review(999) is None
        assert store.count_runs() == 0

        # Deleting nonexistent returns False
        assert store.delete_run(999) is False


# ==============================================================================
# EventBus Tests: Subscribe and Publish
# ==============================================================================

class TestEventBusSubscribeAndPublish:
    """Tests for EventBus subscribe and publish functionality."""

    def test_subscribe_and_publish_sync(self) -> None:
        """Subscribe a synchronous callback and publish events with payload."""
        bus = EventBus()
        received: list[Event] = []

        def on_event(event: Event) -> None:
            received.append(event)

        bus.subscribe(RUN_STATE_CHANGED, on_event)
        published = bus.publish(RUN_STATE_CHANGED, {"old": "QUEUED", "new": "INVESTIGATING"})

        assert isinstance(published, Event)
        assert published.topic == RUN_STATE_CHANGED
        assert published.data == {"old": "QUEUED", "new": "INVESTIGATING"}
        assert len(received) == 1
        assert received[0] is published
        assert isinstance(received[0].timestamp, float)

    def test_multiple_subscribers_same_topic(self) -> None:
        """Multiple callbacks registered on the same topic all receive the event."""
        bus = EventBus()
        results_a: list[Event] = []
        results_b: list[Event] = []

        bus.subscribe(AGENT_EVENT, lambda e: results_a.append(e))
        bus.subscribe(AGENT_EVENT, lambda e: results_b.append(e))

        bus.publish(AGENT_EVENT, {"actor": "investigator", "status": "done"})

        assert len(results_a) == 1
        assert len(results_b) == 1
        assert results_a[0].data["actor"] == "investigator"
        assert results_b[0].data["status"] == "done"

    def test_duplicate_subscribe_ignored(self) -> None:
        """Subscribing the exact same callback twice to the same topic does not duplicate calls."""
        bus = EventBus()
        calls = 0

        def counter(event: Event) -> None:
            nonlocal calls
            calls += 1

        bus.subscribe(TEST_OUTPUT, counter)
        bus.subscribe(TEST_OUTPUT, counter)

        bus.publish(TEST_OUTPUT, {"line": "test line"})
        assert calls == 1

    def test_publish_event_direct(self) -> None:
        """publish_event distributes an already-instantiated Event object."""
        bus = EventBus()
        received: list[Event] = []

        bus.subscribe(ARTIFACT_ADDED, lambda e: received.append(e))

        custom_event = Event(topic=ARTIFACT_ADDED, data={"file": "patch.diff"}, timestamp=123.456)
        bus.publish_event(custom_event)

        assert len(received) == 1
        assert received[0] is custom_event
        assert received[0].timestamp == 123.456

    def test_wildcard_subscription(self) -> None:
        """Wildcard '*' subscriber receives events from all topics."""
        bus = EventBus()
        all_events: list[Event] = []

        bus.subscribe("*", lambda e: all_events.append(e))

        bus.publish(RUN_STATE_CHANGED, {"state": "QUEUED"})
        bus.publish(AGENT_EVENT, {"msg": "hello"})
        bus.publish(RUN_COMPLETED, {"result": "success"})

        assert len(all_events) == 3
        topics = [e.topic for e in all_events]
        assert topics == [RUN_STATE_CHANGED, AGENT_EVENT, RUN_COMPLETED]

    def test_async_subscriber_execution(self) -> None:
        """Async subscriber coroutine is safely executed."""
        bus = EventBus()
        received: list[Event] = []

        async def async_callback(event: Event) -> None:
            await asyncio.sleep(0.001)
            received.append(event)

        bus.subscribe(AGENT_EVENT, async_callback)
        bus.publish(AGENT_EVENT, {"async": True})

        assert len(received) == 1
        assert received[0].data == {"async": True}


# ==============================================================================
# EventBus Tests: Standard Topics
# ==============================================================================

class TestEventBusStandardTopics:
    """Tests verifying standard topics constants and catalog."""

    def test_standard_topic_constants(self) -> None:
        """Verify the exact values of all standard topic strings."""
        assert RUN_STATE_CHANGED == "run.state_changed"
        assert AGENT_EVENT == "run.agent_event"
        assert ARTIFACT_ADDED == "run.artifact_added"
        assert TEST_OUTPUT == "run.test_output"
        assert RUN_COMPLETED == "run.completed"

    def test_standard_topics_tuple(self) -> None:
        """STANDARD_TOPICS contains all defined standard topics."""
        assert isinstance(STANDARD_TOPICS, tuple)
        assert len(STANDARD_TOPICS) == 5
        assert RUN_STATE_CHANGED in STANDARD_TOPICS
        assert AGENT_EVENT in STANDARD_TOPICS
        assert ARTIFACT_ADDED in STANDARD_TOPICS
        assert TEST_OUTPUT in STANDARD_TOPICS
        assert RUN_COMPLETED in STANDARD_TOPICS

    def test_publish_and_subscribe_each_standard_topic(self) -> None:
        """Publish and subscribe to each of the standard topics individually."""
        bus = EventBus()
        events_by_topic: dict[str, list[Event]] = {t: [] for t in STANDARD_TOPICS}

        for topic in STANDARD_TOPICS:
            bus.subscribe(topic, lambda e, t=topic: events_by_topic[t].append(e))

        for topic in STANDARD_TOPICS:
            bus.publish(topic, {"topic_name": topic})

        for topic in STANDARD_TOPICS:
            assert len(events_by_topic[topic]) == 1
            assert events_by_topic[topic][0].topic == topic
            assert events_by_topic[topic][0].data["topic_name"] == topic


# ==============================================================================
# EventBus Tests: Unsubscribe
# ==============================================================================

class TestEventBusUnsubscribe:
    """Tests for unsubscribing and clearing listeners."""

    def test_unsubscribe_removes_callback(self) -> None:
        """Unsubscribing a callback prevents future event deliveries."""
        bus = EventBus()
        received: list[Event] = []

        def callback(event: Event) -> None:
            received.append(event)

        bus.subscribe(RUN_STATE_CHANGED, callback)
        bus.publish(RUN_STATE_CHANGED, {"step": 1})
        assert len(received) == 1

        # Unsubscribe
        bus.unsubscribe(RUN_STATE_CHANGED, callback)
        bus.publish(RUN_STATE_CHANGED, {"step": 2})
        assert len(received) == 1  # Unchanged

    def test_unsubscribe_idempotent_and_safe(self) -> None:
        """Unsubscribing an unregistered callback or unknown topic does not raise."""
        bus = EventBus()

        def dummy(event: Event) -> None:
            pass

        # Should silently succeed without error
        bus.unsubscribe(RUN_STATE_CHANGED, dummy)
        bus.unsubscribe("nonexistent.topic", dummy)

    def test_clear_removes_all_subscribers(self) -> None:
        """clear() removes all subscribers across all topics."""
        bus = EventBus()
        received: list[Event] = []

        cb1 = lambda e: received.append(e)
        cb2 = lambda e: received.append(e)

        bus.subscribe(RUN_STATE_CHANGED, cb1)
        bus.subscribe(RUN_STATE_CHANGED, cb2)
        bus.subscribe(AGENT_EVENT, cb1)

        bus.publish(RUN_STATE_CHANGED, {"val": 1})
        # cb1 and cb2 both called for RUN_STATE_CHANGED
        assert len(received) == 2

        bus.clear()
        bus.publish(RUN_STATE_CHANGED, {"val": 2})
        bus.publish(AGENT_EVENT, {"val": 3})
        assert len(received) == 2  # No new events received

    def test_subscriber_exception_propagates(self) -> None:
        """Exceptions in synchronous subscribers propagate out of publish."""
        bus = EventBus()

        def faulty_subscriber(event: Event) -> None:
            raise RuntimeError("Subscriber explosion")

        bus.subscribe(TEST_OUTPUT, faulty_subscriber)
        with pytest.raises(RuntimeError, match="Subscriber explosion"):
            bus.publish(TEST_OUTPUT, {"text": "hello"})
