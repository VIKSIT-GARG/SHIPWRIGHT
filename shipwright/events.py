"""EventBus and event definitions for Shipwright."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import concurrent.futures
from dataclasses import dataclass, field
import inspect
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Standard topics constants
RUN_STATE_CHANGED: str = "run.state_changed"
AGENT_EVENT: str = "run.agent_event"
ARTIFACT_ADDED: str = "run.artifact_added"
TEST_OUTPUT: str = "run.test_output"
RUN_COMPLETED: str = "run.completed"

STANDARD_TOPICS: tuple[str, ...] = (
    RUN_STATE_CHANGED,
    AGENT_EVENT,
    ARTIFACT_ADDED,
    TEST_OUTPUT,
    RUN_COMPLETED,
)


@dataclass(slots=True)
class Event:
    """An event emitted across the Shipwright event bus."""

    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """In-memory publish/subscribe event bus.

    Supports synchronous and asynchronous subscribers with thread-safe subscription
    management and coroutine scheduling.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._subscribers: dict[str, list[Callable[[Event], Any]]] = {}
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_futures: set[concurrent.futures.Future[Any]] = set()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The associated asyncio event loop, if any."""
        return self._loop

    @loop.setter
    def loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def subscribe(self, topic: str, callback: Callable[[Event], Any]) -> None:
        """Subscribe a callback to a topic.

        Callbacks can be synchronous or asynchronous (coroutine function or returning awaitable).
        Duplicate subscriptions of the same callback for a topic are avoided.
        """
        with self._lock:
            callbacks = self._subscribers.setdefault(topic, [])
            if callback not in callbacks:
                callbacks.append(callback)

    def unsubscribe(self, topic: str, callback: Callable[[Event], Any]) -> None:
        """Unsubscribe a callback from a topic.

        If the callback or topic is not found, no error is raised.
        """
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic] = [
                    cb for cb in self._subscribers[topic] if cb != callback
                ]
                if not self._subscribers[topic]:
                    del self._subscribers[topic]

    def publish(self, topic: str, data: dict[str, Any] | None = None) -> Event:
        """Create and publish an event to all subscribers of the topic.

        Calls subscribers synchronously or safely schedules if async.
        Returns the published Event instance.
        """
        if data is None:
            data = {}
        event = Event(topic=topic, data=data, timestamp=time.time())
        self.publish_event(event)
        return event

    def publish_event(self, event: Event) -> None:
        """Publish an existing event instance to all registered subscribers."""
        with self._lock:
            targets = list(self._subscribers.get(event.topic, []))
            if "*" in self._subscribers and event.topic != "*":
                for wildcard_cb in self._subscribers["*"]:
                    if wildcard_cb not in targets:
                        targets.append(wildcard_cb)

        for callback in targets:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    self._schedule_async(result)
            except Exception:
                logger.exception(
                    "Error executing subscriber %r for topic %s",
                    callback,
                    event.topic,
                )
                raise

    def clear(self) -> None:
        """Remove all registered subscribers."""
        with self._lock:
            self._subscribers.clear()

    def _schedule_async(self, coro: Any) -> None:
        """Safely schedule an awaitable callback on an active event loop."""
        # 1. Check if the current thread has a running loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and current_loop.is_running():
            if self._loop is None:
                self._loop = current_loop
            task = current_loop.create_task(coro)
            self._background_tasks.add(task)
            task.add_done_callback(self._on_task_done)
            return

        # 2. Check if a configured loop is running in another thread
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            self._background_futures.add(fut)
            fut.add_done_callback(self._on_future_done)
            return

        # 3. No loop is running anywhere: execute synchronously
        asyncio.run(coro)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.exception("Background event handler task failed: %s", exc)

    def _on_future_done(self, fut: concurrent.futures.Future[Any]) -> None:
        self._background_futures.discard(fut)
        if not fut.cancelled():
            exc = fut.exception()
            if exc is not None:
                logger.exception("Threadsafe event handler future failed: %s", exc)


__all__ = [
    "AGENT_EVENT",
    "ARTIFACT_ADDED",
    "RUN_COMPLETED",
    "RUN_STATE_CHANGED",
    "STANDARD_TOPICS",
    "TEST_OUTPUT",
    "Event",
    "EventBus",
]
