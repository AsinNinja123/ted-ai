"""Small in-process event channel shared by Ted's runtime and local dashboard.

The Ted Code Book — §36.7.


Events are facts about work that actually happened: a plan was accepted, an
agent started, a confirmation was requested, or an agent returned evidence.
The terminal log and the HUD consume this same stream so they cannot disagree
about the underlying event.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import queue
import threading
import time
import uuid


@dataclass(frozen=True)
class Event:
    id: int
    kind: str
    payload: dict
    ts: float

    def as_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "ts": self.ts,
        }


def sse_encode(event: Event) -> str:
    """Encode one event as a standards-compliant SSE frame."""
    data = json.dumps(event.as_dict(), separators=(",", ":"), default=str)
    return f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n"


class Subscription:
    """One bounded consumer queue owned by an :class:`EventBus`."""

    def __init__(self, bus, token, pending):
        self._bus = bus
        self._token = token
        self._pending = pending
        self._closed = False

    def get(self, timeout=None):
        return self._pending.get(timeout=timeout)

    def close(self):
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self._token)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class EventBus:
    """Thread-safe fan-out with a short replay buffer for SSE reconnects."""

    def __init__(self, history_size=256, logger=None):
        self._lock = threading.Lock()
        self._history = deque(maxlen=max(1, int(history_size)))
        self._subscribers = {}
        self._listeners = []
        self._next_id = 1
        self._logger = logger

    def emit(self, kind, payload=None):
        kind = str(kind or "").strip()
        if not kind:
            raise ValueError("event kind cannot be empty")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise TypeError("event payload must be a dict")

        with self._lock:
            event = Event(self._next_id, kind, dict(payload), time.time())
            self._next_id += 1
            self._history.append(event)
            pending = tuple(self._subscribers.values())

        # A slow or abandoned browser must never block Ted. Keep the freshest
        # facts and let Last-Event-ID replay anything still in the ring buffer.
        for target in pending:
            try:
                target.put_nowait(event)
            except queue.Full:
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target.put_nowait(event)
                except queue.Full:
                    pass

        if self._logger is not None:
            try:
                self._logger(event)
            except Exception:
                pass
        # In-process consumers are NOT SSE subscribers on purpose: registering
        # one must not make subscriber_count non-zero, or the HUD fallback in
        # core/app.py would think a browser is already listening.
        for listen in tuple(self._listeners):
            try:
                listen(event)
            except Exception:
                pass
        return event

    def add_listener(self, fn):
        """Register an in-process consumer of every event.

        Used by the HUD bridge: when a standalone `python -m dashboard` owns
        port 5175, Ted's own bus has no SSE subscriber and the thought bubble
        would otherwise go dark with no error anywhere.
        """
        with self._lock:
            self._listeners.append(fn)
        return fn

    def subscribe(self, after_id=None, max_pending=128):
        """Subscribe, optionally replaying events newer than ``after_id``.

        A brand-new HUD passes no id and sees only future work. A reconnecting
        EventSource sends Last-Event-ID and receives any still-buffered gap.
        """
        token = uuid.uuid4().hex
        try:
            after = None if after_id in (None, "") else int(after_id)
        except (TypeError, ValueError):
            after = None
        with self._lock:
            backlog = ([] if after is None else
                       [event for event in self._history if event.id > after])
            pending = queue.Queue(maxsize=max(int(max_pending), len(backlog) + 1, 1))
            for event in backlog:
                pending.put_nowait(event)
            self._subscribers[token] = pending
        return Subscription(self, token, pending)

    def _unsubscribe(self, token):
        with self._lock:
            self._subscribers.pop(token, None)

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


def _log_event(event):
    payload = json.dumps(event.payload, separators=(",", ":"), default=str)
    print(f"[event] {event.id} {event.kind} {payload}")


BUS = EventBus(logger=_log_event)


def emit(kind, payload=None):
    """Publish through Ted's single process-wide event stream."""
    return BUS.emit(kind, payload)


def subscribe(after_id=None, max_pending=128):
    return BUS.subscribe(after_id=after_id, max_pending=max_pending)
