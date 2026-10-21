import asyncio
import threading
import time
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


def test_standard_topic_constants() -> None:
    assert RUN_STATE_CHANGED == "run.state_changed"
    assert AGENT_EVENT == "run.agent_event"
    assert ARTIFACT_ADDED == "run.artifact_added"
    assert TEST_OUTPUT == "run.test_output"
    assert RUN_COMPLETED == "run.completed"
    assert STANDARD_TOPICS == (
        "run.state_changed",
        "run.agent_event",
        "run.artifact_added",
        "run.test_output",
        "run.completed",
    )


def test_event_dataclass_defaults_and_slots() -> None:
    before = time.time()
    event = Event(topic=RUN_STATE_CHANGED)
    after = time.time()

    assert event.topic == "run.state_changed"
    assert event.data == {}
    assert before <= event.timestamp <= after
    assert hasattr(event, "__slots__")

    custom_event = Event(
        topic=AGENT_EVENT,
        data={"agent": "implementer", "action": "code"},
        timestamp=12345.67,
    )
    assert custom_event.topic == "run.agent_event"
    assert custom_event.data == {"agent": "implementer", "action": "code"}
    assert custom_event.timestamp == 12345.67


def test_event_bus_subscribe_and_publish_sync() -> None:
    bus = EventBus()
    received: list[Event] = []

    def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(RUN_STATE_CHANGED, handler)
    # duplicate subscribe should not register twice
    bus.subscribe(RUN_STATE_CHANGED, handler)

    payload = {"from_state": "QUEUED", "to_state": "INVESTIGATING"}
    published = bus.publish(RUN_STATE_CHANGED, payload)

    assert isinstance(published, Event)
    assert published.topic == RUN_STATE_CHANGED
    assert published.data == payload
    assert len(received) == 1
    assert received[0] is published


def test_event_bus_unsubscribe_and_clear() -> None:
    bus = EventBus()
    received: list[Event] = []

    def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(TEST_OUTPUT, handler)
    bus.publish(TEST_OUTPUT, {"line": "test line 1"})
    assert len(received) == 1

    # Unsubscribe
    bus.unsubscribe(TEST_OUTPUT, handler)
    # Idempotent unsubscribe on missing callback or topic
    bus.unsubscribe(TEST_OUTPUT, handler)
    bus.unsubscribe("nonexistent_topic", handler)

    bus.publish(TEST_OUTPUT, {"line": "test line 2"})
    assert len(received) == 1

    # Subscribe again and clear
    bus.subscribe(TEST_OUTPUT, handler)
    bus.subscribe(RUN_COMPLETED, handler)
    bus.clear()
    bus.publish(TEST_OUTPUT, {"line": "test line 3"})
    bus.publish(RUN_COMPLETED, {"status": "success"})
    assert len(received) == 1


def test_event_bus_publish_event() -> None:
    bus = EventBus()
    received: list[Event] = []

    bus.subscribe(ARTIFACT_ADDED, lambda e: received.append(e))
    event = Event(topic=ARTIFACT_ADDED, data={"artifact": "diff.patch"}, timestamp=100.0)
    bus.publish_event(event)

    assert len(received) == 1
    assert received[0] is event
    assert received[0].timestamp == 100.0


def test_event_bus_wildcard_subscription() -> None:
    bus = EventBus()
    all_events: list[Event] = []
    specific_events: list[Event] = []

    bus.subscribe("*", lambda e: all_events.append(e))
    bus.subscribe(RUN_COMPLETED, lambda e: specific_events.append(e))

    bus.publish(RUN_STATE_CHANGED, {"state": "PLANNED"})
    bus.publish(RUN_COMPLETED, {"success": True})

    assert len(all_events) == 2
    assert [e.topic for e in all_events] == [RUN_STATE_CHANGED, RUN_COMPLETED]
    assert len(specific_events) == 1
    assert specific_events[0].topic == RUN_COMPLETED


def test_event_bus_async_callback_sync_fallback() -> None:
    """When no event loop is running, an async callback is safely executed synchronously."""
    bus = EventBus()
    received: list[Event] = []

    async def async_handler(event: Event) -> None:
        await asyncio.sleep(0.001)
        received.append(event)

    bus.subscribe(AGENT_EVENT, async_handler)
    bus.publish(AGENT_EVENT, {"msg": "hello from async"})

    assert len(received) == 1
    assert received[0].data == {"msg": "hello from async"}


def test_event_bus_async_callback_in_event_loop() -> None:
    """When called inside an active event loop, async callbacks are scheduled as tasks."""
    async def run_async_test() -> None:
        bus = EventBus()
        received: list[Event] = []
        done_event = asyncio.Event()

        async def async_handler(event: Event) -> None:
            await asyncio.sleep(0.01)
            received.append(event)
            done_event.set()

        bus.subscribe(AGENT_EVENT, async_handler)
        bus.publish(AGENT_EVENT, {"msg": "task scheduled"})

        await asyncio.wait_for(done_event.wait(), timeout=2.0)
        assert len(received) == 1
        assert received[0].data == {"msg": "task scheduled"}

    asyncio.run(run_async_test())


def test_event_bus_async_callback_cross_thread() -> None:
    """When called from a worker thread, async callback safely schedules onto the target loop."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        bus = EventBus(loop=loop)
        received: list[Event] = []
        done_event = threading.Event()

        async def async_handler(event: Event) -> None:
            received.append(event)
            done_event.set()

        bus.subscribe(RUN_COMPLETED, async_handler)
        bus.publish(RUN_COMPLETED, {"result": "ok"})

        assert done_event.wait(timeout=2.0)
        assert len(received) == 1
        assert received[0].data == {"result": "ok"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)


def test_event_bus_sync_error_propagation() -> None:
    bus = EventBus()

    def bad_callback(event: Event) -> None:
        raise ValueError("Something went wrong in subscriber")

    bus.subscribe("error.topic", bad_callback)
    with pytest.raises(ValueError, match="Something went wrong in subscriber"):
        bus.publish("error.topic", {})
