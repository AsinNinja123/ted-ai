"""Universal contracts for the star-shaped agent runtime.

The Ted Code Book — Chapter 36. §36.2 is the contract, §36.3 is how synchronous
code runs these coroutines, §36.5 is who decides an action is consequential.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import threading
import time
import uuid

from core import events


@dataclass
class AgentResult:
    ok: bool
    did: str
    evidence: dict
    failed: str | None = None
    duration_ms: int = 0

    def __post_init__(self):
        if not isinstance(self.evidence, dict):
            raise TypeError("AgentResult.evidence must be a dict")
        self.did = str(self.did or "").strip()
        self.failed = str(self.failed).strip() if self.failed else None
        self.duration_ms = max(0, int(self.duration_ms or 0))

    def as_dict(self):
        return {
            "ok": bool(self.ok),
            "did": self.did,
            "evidence": dict(self.evidence),
            "failed": self.failed,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class Delegation:
    agent: str
    method: str
    args: dict = field(default_factory=dict)

    def as_dict(self):
        return {"agent": self.agent, "method": self.method, "args": dict(self.args)}


@dataclass
class Plan:
    heard: str
    steps: list[Delegation]
    parallel: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_dict(self):
        return {
            "plan_id": self.id,
            "heard": self.heard,
            "steps": [step.as_dict() for step in self.steps],
            "parallel": bool(self.parallel),
        }

    def announce(self):
        """Put the exact parsed plan on the shared log/HUD event stream."""
        events.emit("plan", self.as_dict())
        return self


class ConfirmationGate:
    """One Future per pending consequential action, keyed by request id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    async def request(self, agent, method, args, plan_id=None, timeout=60.0):
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            self._pending[request_id] = (loop, future)
        events.emit("confirmation_required", {
            "request_id": request_id,
            "plan_id": plan_id,
            "agent": agent,
            "method": method,
            "args": dict(args),
        })
        try:
            approved = bool(await asyncio.wait_for(future, timeout=timeout))
        except asyncio.TimeoutError:
            approved = False
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        events.emit("confirmation_resolved", {
            "request_id": request_id,
            "plan_id": plan_id,
            "approved": approved,
        })
        return approved

    def resolve(self, request_id, approved):
        with self._lock:
            item = self._pending.pop(request_id, None)
        if item is None:
            return False
        loop, future = item
        if future.done():
            return False

        def finish():
            if not future.done():
                future.set_result(bool(approved))

        loop.call_soon_threadsafe(finish)
        return True

    def pending_ids(self):
        with self._lock:
            return tuple(self._pending)


DEFAULT_CONFIRMATION_GATE = ConfirmationGate()


class BaseAgent(ABC):
    """Timing, dry-run, confirmation, failure containment, and event emission."""

    name = "Agent"

    def __init__(self, confirmation_gate=None):
        self.confirmation_gate = confirmation_gate or DEFAULT_CONFIRMATION_GATE

    async def execute(self, method, args=None, *, plan_id=None, dry_run=False,
                      confirmation_timeout=60.0):
        args = dict(args or {})
        started = time.perf_counter()
        events.emit("agent_started", {
            "plan_id": plan_id,
            "agent": self.name,
            "method": method,
            "args": args,
            "dry_run": bool(dry_run),
        })
        try:
            if dry_run:
                result = self._dry_run(method, args)
            elif self.needs_confirmation(method, args):
                approved = await self.confirmation_gate.request(
                    self.name, method, args, plan_id=plan_id,
                    timeout=confirmation_timeout)
                if not approved:
                    result = AgentResult(
                        ok=False,
                        did="Nothing was changed.",
                        evidence={"confirmed": False},
                        failed="The action was not approved.",
                    )
                else:
                    result = await asyncio.to_thread(self._run, method, args)
            else:
                result = await asyncio.to_thread(self._run, method, args)
            if not isinstance(result, AgentResult):
                raise TypeError(f"{self.name} returned {type(result).__name__}, not AgentResult")
        except Exception as exc:
            result = AgentResult(
                ok=False,
                did="Nothing was changed.",
                evidence={},
                failed=f"{type(exc).__name__}: {exc}",
            )

        result.duration_ms = max(
            result.duration_ms, int((time.perf_counter() - started) * 1000))
        payload = result.as_dict()
        payload.update(plan_id=plan_id, agent=self.name, method=method)
        events.emit("agent_result", payload)
        return result

    def needs_confirmation(self, method, args):
        return False

    @abstractmethod
    def describe(self):
        raise NotImplementedError

    @abstractmethod
    def _run(self, method, args):
        raise NotImplementedError

    @abstractmethod
    def _dry_run(self, method, args):
        raise NotImplementedError
