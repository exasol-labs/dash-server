"""Single-flight command queue between the MCP handler and a browser tab.

One in-flight command per session, deliberately: the alternative (queueing) lets
an agent stack expensive work on a page the user is actively reading, and makes
the deadline meaningless because a command's clock would start at an unpredictable
time. A second concurrent dispatch is refused immediately instead.

The MCP handler blocks on :meth:`CommandQueue.wait`; the page picks the command up
on its next poll and posts a result back, which sets the event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any
import uuid


@dataclass
class Command:
    """One dispatched evaluation, and the slot its result lands in."""

    command_id: str
    session_id: str
    code: str
    timeout_seconds: float
    command_seq: int
    created_monotonic: float
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    delivered_monotonic: float | None = None
    result: dict[str, Any] | None = None
    #: Set when the deadline expired before a result arrived. The page may still be
    #: running the code — JavaScript cannot be cancelled from the server — so the
    #: slot is released and a late result is discarded rather than mismatched.
    abandoned: bool = False

    def to_wire(self) -> dict[str, Any]:
        """The payload handed to the page on its poll."""

        return {
            "command_id": self.command_id,
            "code": self.code,
            "timeout_seconds": self.timeout_seconds,
            "command_seq": self.command_seq,
        }


class CommandBusyError(RuntimeError):
    """Raised when a session already has an in-flight command."""

    def __init__(self, pending: Command) -> None:
        super().__init__(f"session {pending.session_id} already has command {pending.command_id} in flight")
        self.pending = pending


class CommandQueue:
    """Per-session single-flight command slots."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, Command] = {}
        self._last_completion_monotonic: dict[str, float] = {}

    def enqueue(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        command_seq: int,
    ) -> Command:
        with self._lock:
            existing = self._pending.get(session_id)
            if existing is not None and not existing.done.is_set() and not existing.abandoned:
                raise CommandBusyError(existing)
            command = Command(
                command_id=uuid.uuid4().hex,
                session_id=session_id,
                code=code,
                timeout_seconds=timeout_seconds,
                command_seq=command_seq,
                created_monotonic=time.monotonic(),
            )
            self._pending[session_id] = command
            return command

    def take(self, session_id: str) -> Command | None:
        """Hand the pending command to a polling page, once.

        A command already delivered is *not* re-delivered on a later poll: a page
        that reloaded mid-command keeps its ``sessionStorage`` id, and re-delivering
        would run the agent's code twice. The command times out instead.
        """

        with self._lock:
            command = self._pending.get(session_id)
            if command is None or command.abandoned or command.done.is_set():
                return None
            if command.delivered_monotonic is not None:
                return None
            command.delivered_monotonic = time.monotonic()
            return command

    def complete(self, *, session_id: str, command_id: str, result: dict[str, Any]) -> bool:
        """Record a result. Returns ``False`` for an unknown or abandoned command."""

        with self._lock:
            command = self._pending.get(session_id)
            if command is None or command.command_id != command_id:
                return False
            if command.abandoned:
                # Late result for a command the handler already gave up on. Drop it
                # rather than resolving a slot the caller is no longer waiting on.
                self._pending.pop(session_id, None)
                return False
            command.result = result
            self._last_completion_monotonic[session_id] = time.monotonic()
            command.done.set()
            self._pending.pop(session_id, None)
            return True

    def wait(self, command: Command) -> dict[str, Any] | None:
        """Block until the page answers, or the deadline expires.

        Returns the result payload, or ``None`` on timeout (the slot is released so
        the session is not wedged for the rest of its life).
        """

        completed = command.done.wait(timeout=command.timeout_seconds)
        if completed:
            return command.result
        with self._lock:
            command.abandoned = True
            current = self._pending.get(command.session_id)
            if current is not None and current.command_id == command.command_id:
                self._pending.pop(command.session_id, None)
        return None

    def pending(self, session_id: str) -> Command | None:
        with self._lock:
            command = self._pending.get(session_id)
            if command is None or command.done.is_set() or command.abandoned:
                return None
            return command

    def seconds_since_completion(self, session_id: str) -> float | None:
        with self._lock:
            stamp = self._last_completion_monotonic.get(session_id)
        if stamp is None:
            return None
        return time.monotonic() - stamp

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._pending.pop(session_id, None)
            self._last_completion_monotonic.pop(session_id, None)


__all__ = ["Command", "CommandBusyError", "CommandQueue"]
