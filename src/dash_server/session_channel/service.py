"""Facade the blueprint and the MCP handler both talk to.

Composes the session registry and the command queue, owns the enable gate, applies
the server-side result caps (the page is expected to bound its own payloads; the
control plane does not trust it to), and writes the audit record.

Failure-shape policy, which the tool description repeats for agents:

* a **transport** failure — no live tab, a stale tab, a busy session, or a deadline
  with no answer at all — raises :class:`DashServerError`, because the agent has to
  do something different rather than read a result;
* a **code** failure — the submitted JavaScript threw — comes back as an ordinary
  result with ``ok: false`` and a structured ``error`` carrying the message, stack,
  and the line *relative to the submitted code*, because that is what the agent
  needs in order to fix and retry in one hop.
"""

from __future__ import annotations

import json
import time
from typing import Any

from dash_server.exceptions import DashServerError
from dash_server.timestamps import now_iso

from .contract import (
    CATEGORY_BUSY,
    CATEGORY_PROTOCOL,
    CATEGORY_SESSION_GONE,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    DIAGNOSTICS_CHANNEL,
    EVAL_MODES,
    SENTINEL_OMITTED_CHARS,
    SENTINEL_TRUNCATED,
    SENTINEL_TYPE,
)
from .queue import CommandBusyError, CommandQueue
from .registry import Session, SessionRegistry

#: Console entries are capped hard: a runaway loop calling `console.log` must not
#: be able to fill an MCP response even if the page's own cap fails.
_MAX_CONSOLE_ENTRIES = 50
_MAX_CONSOLE_CHARS = 2000
_MAX_ERROR_CHARS = 4000


class SessionChannelService:
    """Control-plane half of the browser session channel."""

    def __init__(
        self,
        *,
        enabled: bool,
        disabled_reason: str | None = None,
        diagnostics_service: Any | None = None,
        max_sessions: int = 20,
        stale_after_ms: int = 6000,
        poll_interval_ms: int = 2000,
        active_poll_interval_ms: int = 250,
        command_timeout_seconds: int = 10,
        max_code_bytes: int = 16384,
        max_result_bytes: int = 262144,
        active_window_seconds: float = 5.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.disabled_reason = disabled_reason
        self.diagnostics_service = diagnostics_service
        self.poll_interval_ms = max(100, int(poll_interval_ms))
        self.active_poll_interval_ms = max(50, int(active_poll_interval_ms))
        self.command_timeout_seconds = max(1, int(command_timeout_seconds))
        self.max_code_bytes = max(1, int(max_code_bytes))
        self.max_result_bytes = max(1024, int(max_result_bytes))
        self.active_window_seconds = max(0.0, float(active_window_seconds))
        self.stale_after_seconds = max(0.5, int(stale_after_ms) / 1000.0)
        self.registry = SessionRegistry(
            max_sessions=max_sessions,
            stale_after_seconds=self.stale_after_seconds,
        )
        self.queue = CommandQueue()

    # ---- gate -------------------------------------------------------------

    def require_enabled(self, *, tool_name: str) -> None:
        if self.enabled:
            return
        raise DashServerError(
            category=CATEGORY_UNAVAILABLE,
            summary="The browser session channel is not available on this server.",
            details={
                "tool": tool_name,
                "reason": self.disabled_reason or "disabled",
                "hint": (
                    "The session channel is a local-mode feature. It requires "
                    "DASH_SERVER_MODE=local, SESSION_CHANNEL_ENABLED, and a loopback "
                    "control-plane bind."
                ),
            },
        )

    def status(self) -> dict[str, Any]:
        """Operator/agent-visible channel configuration."""

        return {
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "poll_interval_ms": self.poll_interval_ms,
            "active_poll_interval_ms": self.active_poll_interval_ms,
            "stale_after_ms": int(self.stale_after_seconds * 1000),
            "command_timeout_seconds": self.command_timeout_seconds,
            "max_code_bytes": self.max_code_bytes,
            "max_result_bytes": self.max_result_bytes,
            "max_sessions": self.registry.max_sessions,
            "session_count": len(self.registry.list_sessions()),
        }

    # ---- page-facing ------------------------------------------------------

    def register(self, payload: Any, *, user_agent: str | None = None) -> dict[str, Any]:
        body = self._require_object(payload)
        session_id = self._require_session_id(body.get("session_id"))
        mount_path = body.get("mount_path")
        if not isinstance(mount_path, str) or not mount_path.startswith("/"):
            raise DashServerError(
                category=CATEGORY_PROTOCOL,
                summary="register requires a mount_path beginning with '/'.",
                details={"field": "mount_path"},
            )
        revision_number = body.get("revision_number")
        if revision_number is not None and (
            not isinstance(revision_number, int) or isinstance(revision_number, bool)
        ):
            revision_number = None
        capabilities = body.get("capabilities")
        session = self.registry.register(
            session_id=session_id,
            mount_path=mount_path,
            revision_number=revision_number,
            pathname=body.get("pathname") if isinstance(body.get("pathname"), str) else None,
            capabilities=capabilities if isinstance(capabilities, dict) else {},
            user_agent=user_agent,
        )
        return {
            "session_id": session.session_id,
            "app": session.app_name,
            "poll_interval_ms": self._poll_interval_for(session.session_id),
        }

    def poll(self, session_id: Any, *, pathname: str | None = None) -> dict[str, Any]:
        resolved_id = self._require_session_id(session_id)
        session = self.registry.touch(resolved_id, pathname=pathname)
        if session is None:
            # Unknown id: tell the page to re-register rather than silently dropping
            # it into a poll loop that can never receive a command.
            return {"command": None, "register_required": True, "poll_interval_ms": self.poll_interval_ms}
        command = self.queue.take(resolved_id)
        return {
            "command": command.to_wire() if command is not None else None,
            "register_required": False,
            "poll_interval_ms": self._poll_interval_for(resolved_id),
        }

    def submit_result(self, payload: Any) -> dict[str, Any]:
        body = self._require_object(payload)
        session_id = self._require_session_id(body.get("session_id"))
        command_id = body.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            raise DashServerError(
                category=CATEGORY_PROTOCOL,
                summary="result requires a command_id.",
                details={"field": "command_id"},
            )
        accepted = self.queue.complete(session_id=session_id, command_id=command_id, result=body)
        return {"accepted": accepted}

    # ---- agent-facing -----------------------------------------------------

    def list_sessions(self, *, app_name: str | None = None) -> dict[str, Any]:
        sessions = self.registry.list_sessions(app_name=app_name)
        return {
            "sessions": sessions,
            "live_count": sum(1 for session in sessions if session["live"]),
            "channel": self.status(),
        }

    def dispatch(
        self,
        *,
        app_name: str,
        code: str,
        session_id: str | None = None,
        timeout_seconds: int | None = None,
        tool_name: str = "app_session_eval_js",
    ) -> dict[str, Any]:
        """Run ``code`` in a tab and return the bounded result envelope."""

        self.require_enabled(tool_name=tool_name)
        self._validate_code(code, tool_name=tool_name)
        timeout = self._resolve_timeout(timeout_seconds, tool_name=tool_name)
        session = self._resolve_session_or_fail(app_name=app_name, session_id=session_id, tool_name=tool_name)

        command_seq = self.registry.next_command_seq(session.session_id)
        try:
            command = self.queue.enqueue(
                session_id=session.session_id,
                code=code,
                timeout_seconds=float(timeout),
                command_seq=command_seq,
            )
        except CommandBusyError as exc:
            raise DashServerError(
                category=CATEGORY_BUSY,
                summary="That session already has a command in flight.",
                details={
                    "tool": tool_name,
                    "session_id": session.session_id,
                    "pending_command_id": exc.pending.command_id,
                    "hint": "Wait for the in-flight command to finish, or target another session.",
                },
            ) from exc

        raw = self.queue.wait(command)
        if raw is None:
            self._audit(
                session,
                code=code,
                outcome="timeout",
                command_id=command.command_id,
                duration_ms=int(timeout * 1000),
            )
            raise DashServerError(
                category=CATEGORY_TIMEOUT,
                summary=f"The session did not answer within {timeout}s.",
                details={
                    "tool": tool_name,
                    "session_id": session.session_id,
                    "command_id": command.command_id,
                    "timeout_seconds": timeout,
                    "seconds_since_poll": self._seconds_since_poll(session),
                    "hint": (
                        "The tab may have been closed, backgrounded, or the code may still "
                        "be running — JavaScript cannot be cancelled from the server. Check "
                        "app_sessions_list before retrying."
                    ),
                },
            )

        envelope = self._build_envelope(session, command_id=command.command_id, command_seq=command_seq, raw=raw)
        self._audit(
            session,
            code=code,
            outcome="ok" if envelope["ok"] else "error",
            command_id=command.command_id,
            duration_ms=envelope.get("duration_ms"),
            truncated=envelope.get("truncated"),
        )
        return envelope

    # ---- internals --------------------------------------------------------

    def _resolve_session_or_fail(
        self,
        *,
        app_name: str,
        session_id: str | None,
        tool_name: str,
    ) -> Session:
        session = self.registry.resolve(app_name=app_name, session_id=session_id)
        live_sessions = self.registry.list_sessions(app_name=app_name)
        if session is None:
            raise DashServerError(
                category=CATEGORY_SESSION_GONE,
                summary=(
                    f"No live browser session for app {app_name}."
                    if not session_id or session_id == "auto"
                    else f"Session {session_id} is not registered."
                ),
                details={
                    "tool": tool_name,
                    "app": app_name,
                    "requested_session_id": session_id or "auto",
                    "reason": "unknown" if session_id and session_id != "auto" else "no_live_session",
                    "live_sessions": live_sessions,
                    "hint": "Ask the user to open the dashboard in a browser tab, then retry.",
                },
            )
        if session.app_name != app_name:
            # An explicit id belonging to a different app. Refuse rather than
            # answering about the wrong dashboard.
            raise DashServerError(
                category=CATEGORY_SESSION_GONE,
                summary=f"Session {session.session_id} belongs to app {session.app_name}, not {app_name}.",
                details={
                    "tool": tool_name,
                    "app": app_name,
                    "session_app": session.app_name,
                    "requested_session_id": session_id,
                    "reason": "app_mismatch",
                    "live_sessions": live_sessions,
                },
            )
        if not self.registry.is_live(session):
            raise DashServerError(
                category=CATEGORY_SESSION_GONE,
                summary=f"Session {session.session_id} has stopped polling and is treated as gone.",
                details={
                    "tool": tool_name,
                    "app": app_name,
                    "session_id": session.session_id,
                    "reason": "stale",
                    "last_poll_at": session.last_poll_at,
                    "seconds_since_poll": self._seconds_since_poll(session),
                    "stale_after_ms": int(self.stale_after_seconds * 1000),
                    "live_sessions": live_sessions,
                    "hint": (
                        "That tab's state is no longer observable. Do not report its last "
                        "known values as current."
                    ),
                },
            )
        return session

    def _build_envelope(
        self,
        session: Session,
        *,
        command_id: str,
        command_seq: int,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        ok = bool(raw.get("ok"))
        value, value_truncated = self._cap_value(raw.get("value"))
        out, out_truncated = self._cap_value(raw.get("out"))
        console = self._cap_console(raw.get("console"))
        eval_mode = raw.get("eval_mode")
        if eval_mode not in EVAL_MODES:
            eval_mode = None
        envelope: dict[str, Any] = {
            "ok": ok,
            "value": value,
            "out": out if isinstance(out, dict) else {},
            "console": console,
            "truncated": bool(raw.get("truncated")) or value_truncated or out_truncated,
            "duration_ms": self._coerce_int(raw.get("duration_ms")),
            "eval_mode": eval_mode,
            "tier_used": raw.get("tier_used") if isinstance(raw.get("tier_used"), str) else None,
            "captured_at": now_iso(),
            "command_id": command_id,
            "command_seq": command_seq,
            "session": {
                "session_id": session.session_id,
                "app": session.app_name,
                "mount_path": session.mount_path,
                "mount_kind": session.mount_kind,
                "revision_number": session.revision_number,
                "pathname": session.pathname,
                "last_poll_at": session.last_poll_at,
            },
        }
        error = raw.get("error")
        if not ok:
            envelope["error"] = self._cap_error(error)
        return envelope

    def _cap_value(self, value: Any) -> tuple[Any, bool]:
        """Backstop the page's own byte cap; report the drop explicitly."""

        if value is None:
            return None, False
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=repr)
        except (TypeError, ValueError):
            return {SENTINEL_TYPE: "unserializable", SENTINEL_TRUNCATED: True}, True
        if len(encoded) <= self.max_result_bytes:
            return value, False
        return (
            {
                SENTINEL_TYPE: "truncated",
                SENTINEL_TRUNCATED: True,
                SENTINEL_OMITTED_CHARS: len(encoded) - self.max_result_bytes,
            },
            True,
        )

    def _cap_console(self, console: Any) -> list[dict[str, Any]]:
        if not isinstance(console, list):
            return []
        capped: list[dict[str, Any]] = []
        for entry in console[:_MAX_CONSOLE_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            level = entry.get("level")
            text = entry.get("text")
            capped.append(
                {
                    "level": level if isinstance(level, str) else "log",
                    "text": self._clip(text if isinstance(text, str) else str(text), _MAX_CONSOLE_CHARS),
                }
            )
        if len(console) > _MAX_CONSOLE_ENTRIES:
            capped.append(
                {
                    "level": "meta",
                    "text": f"[{len(console) - _MAX_CONSOLE_ENTRIES} further console entries omitted]",
                }
            )
        return capped

    def _cap_error(self, error: Any) -> dict[str, Any]:
        if not isinstance(error, dict):
            return {"name": "Error", "message": "The page reported a failure without details."}
        capped: dict[str, Any] = {
            "name": str(error.get("name") or "Error")[:200],
            "message": self._clip(str(error.get("message") or ""), _MAX_ERROR_CHARS),
        }
        stack = error.get("stack")
        if isinstance(stack, str) and stack:
            capped["stack"] = self._clip(stack, _MAX_ERROR_CHARS)
        line = error.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            capped["line"] = line
        return capped

    def _audit(
        self,
        session: Session,
        *,
        code: str,
        outcome: str,
        command_id: str,
        duration_ms: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        """Append the command to the per-app audit channel.

        Not a governance control in local mode — the user owns everything here. It
        exists so the user can see what an agent ran in their page, and so an agent
        can review its own history. Best-effort by construction.
        """

        if self.diagnostics_service is None or not session.app_name:
            return
        self.diagnostics_service.emit_event(
            session.app_name,
            DIAGNOSTICS_CHANNEL,
            "session_command",
            f"session_channel {outcome} for {session.mount_path}",
            level="info" if outcome == "ok" else "warning",
            revision_number=session.revision_number,
            session_id=session.session_id,
            command_id=command_id,
            mount_path=session.mount_path,
            outcome=outcome,
            duration_ms=duration_ms,
            truncated=truncated,
            code=self._clip(code, 4000),
        )

    def _poll_interval_for(self, session_id: str) -> int:
        """Fast interval while a command is outstanding or just finished.

        Long-polling would be simpler for latency but would hold a request open on
        the app's single-threaded worker server; short adaptive polling keeps each
        request trivial. The interval decays back on its own once the agent stops.
        """

        if self.queue.pending(session_id) is not None:
            return self.active_poll_interval_ms
        since = self.queue.seconds_since_completion(session_id)
        if since is not None and since <= self.active_window_seconds:
            return self.active_poll_interval_ms
        return self.poll_interval_ms

    def _validate_code(self, code: Any, *, tool_name: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise DashServerError(
                category="tool_validation_error",
                summary="code must be a non-empty JavaScript string.",
                details={"tool": tool_name, "field": "code"},
            )
        size = len(code.encode("utf-8"))
        if size > self.max_code_bytes:
            raise DashServerError(
                category="tool_validation_error",
                summary=f"code is {size} bytes; the limit is {self.max_code_bytes}.",
                details={"tool": tool_name, "field": "code", "max_code_bytes": self.max_code_bytes},
            )

    def _resolve_timeout(self, timeout_seconds: int | None, *, tool_name: str) -> int:
        if timeout_seconds is None:
            return self.command_timeout_seconds
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise DashServerError(
                category="tool_validation_error",
                summary="timeout_seconds must be a positive integer.",
                details={"tool": tool_name, "field": "timeout_seconds"},
            )
        return min(timeout_seconds, 30)

    @staticmethod
    def _seconds_since_poll(session: Session) -> float:
        return round(max(0.0, time.monotonic() - session.last_poll_monotonic), 3)

    @staticmethod
    def _require_object(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DashServerError(
                category=CATEGORY_PROTOCOL,
                summary="The session-channel request body must be a JSON object.",
                details={"received_type": type(payload).__name__},
            )
        return payload

    @staticmethod
    def _require_session_id(value: Any) -> str:
        if isinstance(value, str) and value.strip() and len(value) <= 128:
            return value.strip()
        raise DashServerError(
            category=CATEGORY_PROTOCOL,
            summary="session_id must be a non-empty string of at most 128 characters.",
            details={"field": "session_id"},
        )

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"… [{len(text) - limit} chars omitted]"


__all__ = ["SessionChannelService"]
