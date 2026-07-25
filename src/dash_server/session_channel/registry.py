"""In-memory registry of live browser tabs attached to hosted dashboards.

One record per *tab*, not per browser: the page generates its id into
``sessionStorage``, which is tab-scoped, and "which tab is the user looking at" is
exactly the addressing question an agent needs answered. A cookie would identify
the browser instead.

``last_poll_at`` is authoritative for liveness. A tab that stopped polling is
treated as gone rather than as "last known state", because an agent reporting a
dead tab's stale selections as current is the failure mode this whole feature
exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any

from dash_server.timestamps import now_iso

from .contract import app_name_from_mount_path, mount_kind_from_mount_path


@dataclass
class Session:
    """One registered browser tab."""

    session_id: str
    mount_path: str
    app_name: str | None
    mount_kind: str
    revision_number: int | None
    pathname: str | None
    capabilities: dict[str, Any]
    registered_at: str
    #: ``time.monotonic()`` of the last poll — the liveness clock. Monotonic so a
    #: system clock change cannot make a live tab look stale (or vice versa).
    last_poll_monotonic: float
    last_poll_at: str
    #: Wall-clock of the last dispatched command, used for adaptive poll pacing.
    last_command_monotonic: float | None = None
    command_seq: int = 0
    poll_count: int = 0
    user_agent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, now_monotonic: float, stale_after_seconds: float) -> dict[str, Any]:
        age = max(0.0, now_monotonic - self.last_poll_monotonic)
        return {
            "session_id": self.session_id,
            "app": self.app_name,
            "mount_path": self.mount_path,
            "mount_kind": self.mount_kind,
            "revision_number": self.revision_number,
            "pathname": self.pathname,
            "capabilities": dict(self.capabilities),
            "registered_at": self.registered_at,
            "last_poll_at": self.last_poll_at,
            "seconds_since_poll": round(age, 3),
            "live": age <= stale_after_seconds,
            "command_seq": self.command_seq,
            "poll_count": self.poll_count,
        }


class SessionRegistry:
    """Bounded, thread-safe registry of browser tabs keyed by session id."""

    def __init__(self, *, max_sessions: int, stale_after_seconds: float) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.stale_after_seconds = max(0.5, float(stale_after_seconds))
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    # ---- page-facing ------------------------------------------------------

    def register(
        self,
        *,
        session_id: str,
        mount_path: str,
        revision_number: int | None,
        pathname: str | None,
        capabilities: dict[str, Any] | None,
        user_agent: str | None = None,
    ) -> Session:
        """Create or refresh the record for ``session_id``.

        Re-registration is normal: a page reload keeps its ``sessionStorage`` id but
        starts a fresh renderer, so its capability probe may report a different
        prop-access tier. The stored capabilities are replaced, not merged.
        """

        now = time.monotonic()
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.mount_path = mount_path
                existing.app_name = app_name_from_mount_path(mount_path)
                existing.mount_kind = mount_kind_from_mount_path(mount_path)
                existing.revision_number = revision_number
                existing.pathname = pathname
                existing.capabilities = dict(capabilities or {})
                existing.last_poll_monotonic = now
                existing.last_poll_at = now_iso()
                if user_agent is not None:
                    existing.user_agent = user_agent
                return existing

            session = Session(
                session_id=session_id,
                mount_path=mount_path,
                app_name=app_name_from_mount_path(mount_path),
                mount_kind=mount_kind_from_mount_path(mount_path),
                revision_number=revision_number,
                pathname=pathname,
                capabilities=dict(capabilities or {}),
                registered_at=now_iso(),
                last_poll_monotonic=now,
                last_poll_at=now_iso(),
                user_agent=user_agent,
            )
            self._sessions[session_id] = session
            self._evict_if_needed()
            return session

    def touch(self, session_id: str, *, pathname: str | None = None) -> Session | None:
        """Record a poll from ``session_id``; returns ``None`` if it is unknown."""

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.last_poll_monotonic = time.monotonic()
            session.last_poll_at = now_iso()
            session.poll_count += 1
            if pathname:
                session.pathname = pathname
            return session

    # ---- agent-facing -----------------------------------------------------

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def is_live(self, session: Session) -> bool:
        return (time.monotonic() - session.last_poll_monotonic) <= self.stale_after_seconds

    def resolve(self, *, app_name: str, session_id: str | None) -> Session | None:
        """Resolve an explicit session id, or the most-recently-polled live tab.

        ``session_id`` of ``None`` or ``"auto"`` picks the freshest live session for
        the app, which with a single local user is almost always the right tab.
        An explicit id is returned even when stale so the caller can report *why*
        it is unusable rather than silently falling back to a different tab —
        answering about the wrong tab is worse than failing.
        """

        with self._lock:
            if session_id and session_id != "auto":
                return self._sessions.get(session_id)
            candidates = [
                session
                for session in self._sessions.values()
                if session.app_name == app_name and self.is_live(session)
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda session: session.last_poll_monotonic)

    def list_sessions(self, *, app_name: str | None = None, include_stale: bool = True) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if app_name is None or session.app_name == app_name
            ]
            payloads = [
                session.to_dict(now_monotonic=now, stale_after_seconds=self.stale_after_seconds)
                for session in sessions
            ]
        if not include_stale:
            payloads = [payload for payload in payloads if payload["live"]]
        payloads.sort(key=lambda payload: payload["seconds_since_poll"])
        return payloads

    def next_command_seq(self, session_id: str) -> int:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return 0
            session.command_seq += 1
            session.last_command_monotonic = time.monotonic()
            return session.command_seq

    def seconds_since_command(self, session_id: str) -> float | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.last_command_monotonic is None:
                return None
            return time.monotonic() - session.last_command_monotonic

    def prune_stale(self, *, older_than_seconds: float) -> list[str]:
        """Drop sessions that have not polled for a long time; return their ids."""

        cutoff = time.monotonic() - max(0.0, older_than_seconds)
        with self._lock:
            dropped = [
                session_id
                for session_id, session in self._sessions.items()
                if session.last_poll_monotonic < cutoff
            ]
            for session_id in dropped:
                self._sessions.pop(session_id, None)
        return dropped

    def _evict_if_needed(self) -> None:
        """Drop the least-recently-polled sessions past ``max_sessions``.

        Caller holds the lock. Stale tabs are evicted before live ones purely by
        poll recency, which is the same ordering `resolve` uses.
        """

        overflow = len(self._sessions) - self.max_sessions
        if overflow <= 0:
            return
        by_recency = sorted(self._sessions.values(), key=lambda session: session.last_poll_monotonic)
        for session in by_recency[:overflow]:
            self._sessions.pop(session.session_id, None)


__all__ = ["Session", "SessionRegistry"]
