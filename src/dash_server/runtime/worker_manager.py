"""Lifecycle manager for out-of-process app workers.

This is the Phase-3 piece of the runtime-isolation plan. The manager spawns
``python -m dash_server_runtime.worker --mode=serve`` subprocesses, reads each
worker's ``{"event":"ready","port":N}`` line on stdout, tracks the resulting
record in memory, and exposes a small surface for the proxy and the runtime
service to use.

Design notes (mirroring the plan):

- Workers come from spawn for now. A forkserver baseline is a future
  optimization; until then ``start(...)`` invokes ``subprocess.Popen`` with the
  env's ``python_executable``.
- The proxy calls ``ensure_running(mount_path)`` before forwarding a request.
  That entry point exists to support transparent restart after idle-stop. The
  initial implementation just returns the existing record; the idle-stop loop
  is a follow-up.
- Records are persisted to ``instance/workers/{app}/{revision}.json`` so a
  control-plane restart can reap orphaned workers and remount survivors.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Protocol

from dash_server_runtime.worker.protocol import (
    EVENT_FORKED,
    EVENT_READY,
    EVENT_RESPONSE,
    KEY_EVENT,
    KEY_STATUS,
    READY_READ_EVENTS,
)

from ..timestamps import now_iso as _now_iso
from ..timestamps import seconds_since


class WorkerHandle(Protocol):
    """Abstract worker lifecycle handle.

    Workers come from two paths — `subprocess.Popen` for cold spawn and a forkserver
    baseline for warm fork (plus startup-time adoption of pre-existing pids). Each
    has its own way to read liveness, send a stop signal, and surface stdout. This
    protocol lets the manager treat them uniformly.

    `pid` / `stdout` are declared as `@property` so a `SubprocessHandle` (which
    derives them from its `Popen`) satisfies the Protocol alongside `PidHandle`
    (which stores them directly). Mutable-attribute declarations would require both
    to be settable, which `SubprocessHandle` isn't.
    """

    @property
    def pid(self) -> int: ...
    @property
    def stdout(self) -> IO[str] | None: ...
    def is_alive(self) -> bool: ...
    def stop(self, *, timeout_seconds: float) -> None: ...


@dataclass
class SubprocessHandle:
    """Spawn-path handle wrapping a `subprocess.Popen`."""

    process: subprocess.Popen[str]

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def stdout(self) -> IO[str] | None:
        return self.process.stdout

    def is_alive(self) -> bool:
        try:
            return self.process.poll() is None
        except Exception:
            return False

    def stop(self, *, timeout_seconds: float) -> None:
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception:
            pass


@dataclass
class PidHandle:
    """Bare-pid handle for forked or adopted workers (no `Popen` available).

    `stdout` is the read-end of the worker's stdout pipe when the manager spawned
    the worker via the forkserver baseline; `stderr` is the matching stderr pipe.
    Adopted workers have neither (we don't own the child's fds). Holding the file
    objects on the handle keeps the kernel-side pipe buffer drainable for the
    worker's lifetime — matching what `subprocess.Popen` does for us in the spawn
    path.
    """

    pid: int
    stdout: IO[str] | None = None
    stderr: IO[str] | None = None

    def is_alive(self) -> bool:
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def stop(self, *, timeout_seconds: float) -> None:
        _send_sigterm_then_kill(self.pid, timeout_seconds=timeout_seconds)


@dataclass
class WorkerRecord:
    app_name: str
    revision_number: int
    mount_path: str
    pid: int
    host: str
    port: int
    python_executable: str
    environment_id: str | None
    started_at: str
    # Spec preserved so that idle-stopped workers can be re-spawned by ensure_running.
    app_source: str | None = None
    manifest: dict[str, Any] | None = None
    extra_env: dict[str, str] | None = None
    handle: WorkerHandle | None = field(default=None, repr=False)
    last_request_at: str | None = None
    last_healthcheck_at: str | None = None
    last_response_status: int | None = None
    rss_bytes: int | None = None
    status: str = "running"  # "running" | "stopped_idle" | "exited"


class WorkerStartError(RuntimeError):
    """Raised when a worker subprocess fails to emit a ready event."""


@dataclass
class _BaselineHandle:
    """Reference to a long-lived forkserver baseline process.

    The baseline is itself a `subprocess.Popen` so it carries a `SubprocessHandle`
    for liveness/stop just like any other worker. The additional fields are
    baseline-specific metadata (which python it serves, where its accept socket
    lives, which packages it prewarmed).
    """

    python_executable: str
    socket_path: str
    subprocess: SubprocessHandle
    prewarmed_packages: tuple[str, ...]

    @property
    def pid(self) -> int:
        return self.subprocess.pid


class AppWorkerManager:
    """Owns the lifecycle of out-of-process app workers."""

    def __init__(
        self,
        *,
        workers_root: str,
        diagnostics_root: str | None = None,
        gitops_repo_path: str | None = None,
        exasol_secrets_root: str | None = None,
        pycache_root: str | None = None,
        start_timeout_seconds: int = 30,
        idle_stop_seconds: int = 600,
        idle_sweep_interval_seconds: float = 30.0,
        host: str = "127.0.0.1",
        port_range: str | None = None,
        diagnostics_service: Any | None = None,
        enable_forkserver: bool = False,
        prewarm_packages: tuple[str, ...] = ("dash", "flask", "dash_server_runtime"),
        max_restarts_per_5_minutes: int = 5,
        allowed_env_passthrough: tuple[str, ...] = (
            "EXA_PASSWORD",
            "EXA_TOKEN",
            "EXA_ACCESS_TOKEN",
            "EXA_REFRESH_TOKEN",
        ),
    ) -> None:
        self.workers_root = Path(workers_root)
        self.workers_root.mkdir(parents=True, exist_ok=True)
        self.diagnostics_root = diagnostics_root
        self.gitops_repo_path = gitops_repo_path
        self.exasol_secrets_root = exasol_secrets_root
        self.pycache_root = pycache_root
        self.start_timeout_seconds = start_timeout_seconds
        # idle_stop_seconds <= 0 disables idle-stop entirely (`APP_WORKER_IDLE_STOP_SECONDS=0`).
        self.idle_stop_seconds = idle_stop_seconds
        self.idle_sweep_interval_seconds = idle_sweep_interval_seconds
        self.host = host
        self.port_range = port_range
        self.diagnostics_service = diagnostics_service
        # Phase 3.5c: when enabled, the manager keeps one long-lived "baseline" process
        # per python_executable that has prewarmed Dash/Flask/dash_server_runtime, and
        # forks workers from it. Falls back to spawn on any miss.
        self.enable_forkserver = enable_forkserver
        self.prewarm_packages = tuple(prewarm_packages)
        self.allowed_env_passthrough = tuple(allowed_env_passthrough)
        self._records: dict[str, WorkerRecord] = {}  # keyed by mount_path
        self._baselines: dict[str, _BaselineHandle] = {}  # python_executable → handle
        self._baselines_lock = threading.Lock()
        self._lock = threading.RLock()
        self._idle_sweep_thread: threading.Thread | None = None
        self._idle_sweep_stop: threading.Event = threading.Event()
        # Phase 4g: rolling deque of recent spawn durations (ms). statistics.median over
        # the deque gives p50 cold-start time, surfaced on dash://runtime/workers.
        from collections import deque

        self._start_durations_ms: deque[float] = deque(maxlen=256)
        # Phase 4c: per-mount restart timestamps for cap enforcement.
        self._restart_timestamps: dict[str, deque[float]] = {}
        self.max_restarts_per_5_minutes = max(0, int(max_restarts_per_5_minutes))

    # ---- public surface -----------------------------------------------------

    def start(
        self,
        *,
        app_name: str,
        revision_number: int,
        mount_path: str,
        app_source: Path,
        manifest: dict[str, Any],
        python_executable: str | None = None,
        environment_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> WorkerRecord:
        """Spawn a worker, wait for its ready event, return the record."""

        with self._lock:
            existing = self._records.get(mount_path)
            if existing is not None and self._record_is_alive(existing):
                return existing
            # Stale record — drop it before we start the replacement.
            self._records.pop(mount_path, None)

            # Phase 4c: enforce restart cap before any spawn work.
            self._check_restart_cap(mount_path, app_name=app_name)

            # Phase 4g: time the spawn so dash://runtime/workers can report p50 cold-start.
            start_perf = time.monotonic()

            python = python_executable or sys.executable

            # Phase 3.5c: try forkserver baseline first when enabled. On any miss
            # (no baseline, mismatched python, fork error) fall back to spawn below.
            if self.enable_forkserver:
                forked = self._try_baseline_start(
                    app_name=app_name,
                    revision_number=revision_number,
                    mount_path=mount_path,
                    app_source=app_source,
                    manifest=manifest,
                    python_executable=python,
                    environment_id=environment_id,
                    extra_env=extra_env,
                )
                if forked is not None:
                    self._record_start_duration_ms((time.monotonic() - start_perf) * 1000)
                    return forked
                # Forkserver miss — log structured event and fall through to spawn.
                self._emit_worker_event(
                    app_name,
                    "forkserver_miss",
                    "forkserver_miss: falling back to spawn",
                    level="warning",
                    python_executable=python,
                    mount_path=mount_path,
                )

            cmd = [
                python,
                "-m",
                "dash_server_runtime.worker",
                "--mode=serve",
                "--app-name",
                app_name,
                "--app-source",
                str(app_source),
                "--mount-path",
                mount_path,
                "--manifest-json",
                json.dumps(manifest),
                "--revision-number",
                str(revision_number),
                "--listen-host",
                self.host,
                "--listen-port",
                "0",
            ]
            if self.port_range:
                cmd += ["--listen-port-range", self.port_range]
            if self.gitops_repo_path:
                cmd += ["--gitops-repo-path", self.gitops_repo_path]
            if self.exasol_secrets_root:
                cmd += ["--exasol-secrets-root", self.exasol_secrets_root]
            if self.diagnostics_root:
                cmd += ["--diagnostics-root", self.diagnostics_root]

            env = self._build_worker_env(extra_env)

            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise WorkerStartError(f"Failed to launch worker subprocess: {exc!s}") from exc

            ready_payload = self._read_ready_event(process)
            if ready_payload is None or ready_payload.get(KEY_EVENT) != EVENT_READY:
                # Worker died or emitted an error event; gather its tail for diagnostics.
                stderr_tail = ""
                stdout_tail = ""
                try:
                    process.terminate()
                    out, err = process.communicate(timeout=5)
                    stdout_tail = (out or "")[-4000:]
                    stderr_tail = (err or "")[-4000:]
                except subprocess.TimeoutExpired:
                    process.kill()
                raise WorkerStartError(
                    "Worker did not emit a ready event. "
                    f"Last stdout chunk: {stdout_tail!r}; stderr tail: {stderr_tail!r}; "
                    f"failure payload: {ready_payload!r}"
                )

            record = WorkerRecord(
                app_name=app_name,
                revision_number=revision_number,
                mount_path=mount_path,
                pid=int(ready_payload.get("pid") or process.pid),
                host=str(ready_payload.get("host") or self.host),
                port=int(ready_payload["port"]),
                python_executable=python,
                environment_id=environment_id,
                started_at=_now_iso(),
                app_source=str(app_source),
                manifest=manifest,
                extra_env=dict(extra_env) if extra_env else None,
                handle=SubprocessHandle(process=process),
                status="running",
            )
            self._records[mount_path] = record
            self._persist_record(record)
            # Continue draining the worker's stdout into the diagnostics channel. The drain
            # thread also tracks the last-seen status code so the `worker_http` probe can
            # read it without a separate HTTP roundtrip.
            self._start_drain(record)
            # Phase 4g: record spawn duration for p50 reporting.
            self._record_start_duration_ms((time.monotonic() - start_perf) * 1000)
            return record

    def stop(
        self,
        mount_path: str,
        *,
        timeout_seconds: float = 10.0,
        idle: bool = False,
    ) -> bool:
        """Stop the worker behind ``mount_path``.

        With ``idle=False`` (default) the persisted record is removed too, so the worker is
        gone for good. With ``idle=True`` (used by the idle-stop sweep) the persisted record
        is kept so ``ensure_running`` can re-spawn from it on the next request.
        """

        with self._lock:
            record = self._records.pop(mount_path, None)
        if record is None:
            return False
        if record.handle is not None:
            record.handle.stop(timeout_seconds=timeout_seconds)
        if idle:
            # Preserve the spec on disk so the next request can re-spawn the worker.
            record.status = "stopped_idle"
            self._persist_record(record)
        else:
            self._remove_persisted(record)
        return True

    def restart(
        self,
        *,
        mount_path: str,
        app_name: str,
        revision_number: int,
        app_source: Path,
        manifest: dict[str, Any],
        python_executable: str | None = None,
        environment_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> WorkerRecord:
        self.stop(mount_path)
        return self.start(
            app_name=app_name,
            revision_number=revision_number,
            mount_path=mount_path,
            app_source=app_source,
            manifest=manifest,
            python_executable=python_executable,
            environment_id=environment_id,
            extra_env=extra_env,
        )

    def ensure_running(self, mount_path: str) -> WorkerRecord | None:
        """Return the record for ``mount_path``, re-spawning from disk if necessary.

        Three cases:

        1. In-memory record exists and its process is alive → return it (fast path).
        2. In-memory record exists but its process is dead → drop the in-memory entry,
           re-spawn from the persisted spec.
        3. No in-memory record but a persisted spec exists (idle-stop or post-restart
           crash recovery) → re-spawn from the spec.

        Returns ``None`` when no spec is available or the re-spawn fails.
        """

        with self._lock:
            record = self._records.get(mount_path)
            if record is not None and self._record_is_alive(record):
                return record
            if record is not None:
                self._records.pop(mount_path, None)
            spec = self._read_persisted_spec(mount_path)
        if spec is None:
            return None
        # Re-spawn outside the lock; start() reacquires it.
        try:
            return self.start(**spec)
        except WorkerStartError:
            return None

    def get_record(self, mount_path: str) -> WorkerRecord | None:
        with self._lock:
            return self._records.get(mount_path)

    def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            results = []
            for record in self._records.values():
                alive = self._record_is_alive(record)
                results.append(
                    {
                        "app_name": record.app_name,
                        "revision_number": record.revision_number,
                        "mount_path": record.mount_path,
                        "pid": record.pid,
                        "endpoint": f"{record.host}:{record.port}",
                        "python_executable": record.python_executable,
                        "environment_id": record.environment_id,
                        "started_at": record.started_at,
                        "last_request_at": record.last_request_at,
                        "rss_bytes": record.rss_bytes,
                        "status": "ready" if alive else "exited",
                    }
                )
            return results

    def touch_last_request(self, mount_path: str) -> None:
        with self._lock:
            record = self._records.get(mount_path)
            if record is not None:
                record.last_request_at = _now_iso()

    def set_last_response_status(self, mount_path: str, status_code: int) -> None:
        """Used by the proxy to record the most recent worker HTTP status code.

        Read by the ``worker_http`` health probe to decide pass/fail without making
        an extra HTTP request to the worker.
        """

        with self._lock:
            record = self._records.get(mount_path)
            if record is not None:
                record.last_response_status = int(status_code)

    def _record_start_duration_ms(self, ms: float) -> None:
        """Append a spawn duration to the rolling deque used by the p50 metric."""

        with self._lock:
            self._start_durations_ms.append(float(ms))

    def start_time_ms_p50(self) -> float | None:
        """Return the 50th-percentile spawn duration, or None when no samples exist."""

        import statistics

        with self._lock:
            samples = list(self._start_durations_ms)
        if not samples:
            return None
        return float(statistics.median(samples))

    def total_rss_bytes(self) -> int:
        """Aggregate RSS sample across all live workers. Returns 0 when no samples exist."""

        total = 0
        with self._lock:
            for record in self._records.values():
                rss = record.rss_bytes
                if rss is None:
                    continue
                total += int(rss)
        return total

    def _check_restart_cap(self, mount_path: str, *, app_name: str) -> None:
        """Phase 4c: refuse a spawn when the per-mount restart cap was hit recently."""

        if self.max_restarts_per_5_minutes <= 0:
            return
        from collections import deque

        now = time.monotonic()
        window_start = now - 300.0  # 5 minutes
        timestamps = self._restart_timestamps.setdefault(mount_path, deque(maxlen=64))
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()
        if len(timestamps) >= self.max_restarts_per_5_minutes:
            self._emit_worker_event(
                app_name,
                "restart_capped",
                f"restart_capped: {len(timestamps)} restarts in last 5 minutes",
                level="error",
                mount_path=mount_path,
                restarts_in_window=len(timestamps),
                cap=self.max_restarts_per_5_minutes,
            )
            raise WorkerStartError(
                f"Restart cap hit for {mount_path}: "
                f"{len(timestamps)} restarts within the last 5 minutes "
                f"(cap is {self.max_restarts_per_5_minutes})."
            )
        timestamps.append(now)

    def sample_rss(self, mount_path: str) -> int | None:
        """Best-effort RSS sample for the given worker."""

        with self._lock:
            record = self._records.get(mount_path)
            if record is None or record.pid is None:
                return None
        rss = _read_process_rss(record.pid)
        if rss is not None:
            with self._lock:
                record.rss_bytes = rss
        return rss

    def stop_all(self) -> None:
        with self._lock:
            mounts = list(self._records.keys())
        for mount_path in mounts:
            self.stop(mount_path)
        # Also tear down any prewarmed baselines.
        self.stop_all_baselines()

    def adopt_persisted_workers(self) -> dict[str, str]:
        """Phase 4b: scan instance/workers/*/*.json on startup; adopt or reap each entry.

        Returns a ``{mount_path: action}`` map where action is one of ``"adopted"``,
        ``"reaped"``, or ``"already_running"``. Adoption produces ``PidHandle``-backed
        records — the same shape the forkserver path produces — so the rest of the
        manager handles them uniformly through the ``WorkerHandle`` protocol.

        For each persisted spec:

        1. If the recorded TCP port is reachable AND the pid is alive → adopt.
        2. Otherwise → kill via pid (in case the port flapped but the process survived)
           and delete the record file.

        Both decisions emit a structured event on the per-app ``worker.events`` channel.
        """

        if not self.workers_root.exists():
            return {}
        actions: dict[str, str] = {}
        for app_dir in self.workers_root.iterdir():
            if not app_dir.is_dir():
                continue
            for entry in app_dir.iterdir():
                if not entry.is_file() or entry.suffix != ".json":
                    continue
                try:
                    spec = json.loads(entry.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                mount_path = spec.get("mount_path")
                pid = spec.get("pid")
                if not isinstance(mount_path, str) or not isinstance(pid, int):
                    continue

                with self._lock:
                    already_running = mount_path in self._records
                if already_running:
                    actions[mount_path] = "already_running"
                    continue

                if self._adopt_worker(spec, entry):
                    actions[mount_path] = "adopted"
                else:
                    self._reap_worker(spec, entry)
                    actions[mount_path] = "reaped"
        return actions

    def _adopt_worker(self, spec: dict[str, Any], record_path: Path) -> bool:
        host = spec.get("host") or "127.0.0.1"
        port = spec.get("port")
        pid = spec.get("pid")
        if not isinstance(port, int) or not isinstance(pid, int) or pid <= 0:
            return False
        # 1. Is the recorded port reachable?
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                pass
        except OSError:
            return False
        # 2. Is the recorded pid still alive?
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        # Both checks passed — adopt by constructing a bare-pid record. Adopted workers
        # have no stdout pipe (we didn't spawn them), so the handle's stdout is None and
        # _start_drain skips them automatically.
        record = WorkerRecord(
            app_name=spec["app_name"],
            revision_number=int(spec["revision_number"]),
            mount_path=spec["mount_path"],
            pid=pid,
            host=str(host),
            port=int(port),
            python_executable=str(spec.get("python_executable") or sys.executable),
            environment_id=spec.get("environment_id"),
            started_at=str(spec.get("started_at") or _now_iso()),
            app_source=spec.get("app_source"),
            manifest=spec.get("manifest"),
            extra_env=spec.get("extra_env"),
            handle=PidHandle(pid=pid),
            status="running",
        )
        with self._lock:
            self._records[record.mount_path] = record
        # Refresh the persisted record so status reflects the live state.
        self._persist_record(record)
        self._emit_worker_event(
            record.app_name,
            "worker_adopted",
            f"worker_adopted at pid {pid}",
            mount_path=record.mount_path,
            pid=pid,
            host=host,
            port=port,
            revision_number=record.revision_number,
        )
        return True

    def _reap_worker(self, spec: dict[str, Any], record_path: Path) -> None:
        pid = spec.get("pid")
        if isinstance(pid, int) and pid > 0:
            _send_sigterm_then_kill(pid, timeout_seconds=2)
        try:
            record_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._emit_worker_event(
            spec.get("app_name") or "unknown",
            "worker_reaped",
            f"worker_reaped (pid={pid})",
            mount_path=spec.get("mount_path"),
            pid=pid,
        )

    def stop_all_baselines(self) -> None:
        with self._baselines_lock:
            handles = list(self._baselines.values())
            self._baselines.clear()
        for handle in handles:
            handle.subprocess.stop(timeout_seconds=5)
            try:
                Path(handle.socket_path).unlink(missing_ok=True)
            except OSError:
                pass

    def baseline_status(self) -> list[dict[str, Any]]:
        """Lightweight snapshot of the active baselines (for diagnostics/MCP)."""

        with self._baselines_lock:
            return [
                {
                    "python_executable": handle.python_executable,
                    "pid": handle.pid,
                    "socket_path": handle.socket_path,
                    "prewarmed_packages": list(handle.prewarmed_packages),
                    "alive": handle.subprocess.is_alive(),
                }
                for handle in self._baselines.values()
            ]

    # ---- internals ----------------------------------------------------------

    def _build_worker_env(self, extra_env: dict[str, str] | None) -> dict[str, str]:
        """Allow-list only the env vars the worker needs. Don't pass all of os.environ."""

        env: dict[str, str] = {}
        # Minimum required for Python to find packages and write bytecode.
        for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        for key in self.allowed_env_passthrough:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        if self.pycache_root:
            env["PYTHONPYCACHEPREFIX"] = self.pycache_root
        # Phase 3.5a: the worker module lives in `dash_server_runtime`, which Phase 1 installs
        # into every per-app env. The env's site-packages is enough; no PYTHONPATH dance.
        # In editable dev installs the parent of the source tree is on sys.path automatically
        # via the `.pth` file created by `pip install -e .`, so PYTHONPATH still isn't needed.
        # We pass any existing PYTHONPATH through unchanged for operators who set it explicitly.
        existing_pp = os.environ.get("PYTHONPATH")
        if existing_pp:
            env["PYTHONPATH"] = existing_pp
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        return env

    def _read_ready_event(self, process: subprocess.Popen[str]) -> dict[str, Any] | None:
        """Read worker stdout until we see a ready event or the timeout fires."""

        deadline = time.monotonic() + self.start_timeout_seconds
        assert process.stdout is not None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                # Process exited before emitting ready. Read the rest of stdout for context.
                try:
                    leftover = process.stdout.read() or ""
                except Exception:
                    leftover = ""
                for line in leftover.splitlines():
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict) and KEY_EVENT in payload:
                        return payload
                return None
            line = process.stdout.readline()
            if not line:
                # No data ready yet — sleep briefly to avoid spinning.
                time.sleep(0.01)
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get(KEY_EVENT) in READY_READ_EVENTS:
                return payload
        return None  # timed out

    def _start_drain(self, record: WorkerRecord) -> None:
        """Spawn a daemon thread that drains the worker's stdout pipe (no-op if absent)."""

        stdout = record.handle.stdout if record.handle is not None else None
        if stdout is None:
            return
        threading.Thread(target=self._drain, args=(stdout, record), daemon=True).start()

    def _drain(self, stdout: IO[str], record: WorkerRecord) -> None:
        """Drain worker stdout into the diagnostics service and track HTTP status codes."""

        try:
            for raw_line in iter(stdout.readline, ""):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                # Worker emits structured event lines (`{"event": ...}`) plus opaque user output
                # via print()/logging. Split them so user output lands in the "worker" log
                # channel and structured events live in "worker.events" for operator clarity.
                parsed: dict[str, Any] | None = None
                if line.startswith("{"):
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError:
                        candidate = None
                    if isinstance(candidate, dict) and KEY_EVENT in candidate:
                        parsed = candidate
                if parsed is not None:
                    self._handle_worker_event(record, parsed)
                    self._record_log(record, channel="worker.events", message=line, data=parsed)
                else:
                    self._record_log(record, channel="worker", message=line)
        except Exception:
            pass

    def _handle_worker_event(self, record: WorkerRecord, payload: dict[str, Any]) -> None:
        """Update the in-memory record from a structured worker event."""

        event_name = payload.get(KEY_EVENT)
        if event_name == EVENT_RESPONSE:
            status = payload.get(KEY_STATUS)
            if isinstance(status, int):
                with self._lock:
                    record.last_response_status = status

    def _record_log(
        self,
        record: WorkerRecord,
        *,
        channel: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Forward a worker stdout line into the diagnostics service when one is configured."""

        if self.diagnostics_service is None:
            return
        try:
            self.diagnostics_service.append_log(
                record.app_name,
                channel,
                message,
                revision_number=record.revision_number,
                data=data or {},
            )
        except Exception:
            # Logging is best-effort; never let a diagnostics failure kill the worker drain.
            pass

    def _emit_worker_event(
        self,
        app_name: str,
        event: str,
        message: str,
        *,
        level: str = "info",
        **data: Any,
    ) -> None:
        """Per-app ``worker.events`` channel — delegates to DiagnosticsService.emit_event."""

        if self.diagnostics_service is None:
            return
        self.diagnostics_service.emit_event(
            app_name, "worker.events", event, message, level=level, **data
        )

    def _record_is_alive(self, record: WorkerRecord) -> bool:
        return record is not None and record.handle is not None and record.handle.is_alive()

    def _persist_record(self, record: WorkerRecord) -> None:
        app_dir = self.workers_root / record.app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        path = app_dir / f"{record.revision_number}.json"
        path.write_text(
            json.dumps(
                {
                    "app_name": record.app_name,
                    "revision_number": record.revision_number,
                    "mount_path": record.mount_path,
                    "pid": record.pid,
                    "host": record.host,
                    "port": record.port,
                    "python_executable": record.python_executable,
                    "environment_id": record.environment_id,
                    "started_at": record.started_at,
                    "status": record.status,
                    "app_source": record.app_source,
                    "manifest": record.manifest,
                    "extra_env": record.extra_env,
                },
                indent=2,
            )
        )

    def _remove_persisted(self, record: WorkerRecord) -> None:
        path = self.workers_root / record.app_name / f"{record.revision_number}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # ---- forkserver baseline path -------------------------------------------

    def _try_baseline_start(
        self,
        *,
        app_name: str,
        revision_number: int,
        mount_path: str,
        app_source: Path,
        manifest: dict[str, Any],
        python_executable: str,
        environment_id: str | None,
        extra_env: dict[str, str] | None,
    ) -> WorkerRecord | None:
        """Try to fork a worker from a pre-warmed baseline. Returns None on any miss."""

        baseline = self._get_or_create_baseline(python_executable)
        if baseline is None:
            return None

        # Build the same args namespace serve() expects. The forked child runs
        # `dash_server_runtime.worker._serve.serve(Namespace(**args))` directly.
        request_args = {
            "app_name": app_name,
            "app_source": str(app_source),
            "mount_path": mount_path,
            "manifest_json": json.dumps(manifest),
            "revision_number": revision_number,
            "listen_host": self.host,
            "listen_port": 0,
            "listen_port_range": self.port_range,
            "gitops_repo_path": self.gitops_repo_path,
            "exasol_secrets_root": self.exasol_secrets_root,
            "diagnostics_root": self.diagnostics_root,
        }

        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        client_sock: socket.socket | None = None
        try:
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_sock.settimeout(self.start_timeout_seconds)
            client_sock.connect(baseline.socket_path)
            msg = json.dumps({"args": request_args}).encode()
            socket.send_fds(client_sock, [msg], [out_w, err_w])
        except OSError:
            # Connect or send failed — baseline is unreachable; mark it dead and miss.
            self._drop_baseline(baseline.python_executable)
            os.close(out_r)
            os.close(out_w)
            os.close(err_r)
            os.close(err_w)
            return None
        finally:
            # Our copies of the write-ends are owned by the baseline now (it dup2'd
            # them onto the child's stdio). Close ours so EOF propagates on exit.
            os.close(out_w)
            os.close(err_w)

        # Read the parent's ack: a single newline-terminated JSON line.
        try:
            ack_bytes = b""
            while not ack_bytes.endswith(b"\n"):
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                ack_bytes += chunk
        except OSError:
            ack_bytes = b""
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

        try:
            ack = json.loads(ack_bytes.decode().strip() or "{}")
        except json.JSONDecodeError:
            ack = {}
        if ack.get(KEY_EVENT) != EVENT_FORKED:
            os.close(out_r)
            os.close(err_r)
            return None
        forked_pid = int(ack["pid"])

        # Now read the child's ready event from its stdout pipe.
        stdout_file = os.fdopen(out_r, "r", buffering=1)
        stderr_file = os.fdopen(err_r, "r", buffering=1)
        ready_payload = self._read_ready_event_from_file(stdout_file)
        if ready_payload is None or ready_payload.get(KEY_EVENT) != EVENT_READY:
            # Forked child failed to bind / import; kill it and let caller fall back.
            _send_sigterm_then_kill(forked_pid, timeout_seconds=5)
            for handle in (stdout_file, stderr_file):
                try:
                    handle.close()
                except Exception:
                    pass
            return None

        record = WorkerRecord(
            app_name=app_name,
            revision_number=revision_number,
            mount_path=mount_path,
            pid=int(ready_payload.get("pid") or forked_pid),
            host=str(ready_payload.get("host") or self.host),
            port=int(ready_payload["port"]),
            python_executable=python_executable,
            environment_id=environment_id,
            started_at=_now_iso(),
            app_source=str(app_source),
            manifest=manifest,
            extra_env=dict(extra_env) if extra_env else None,
            handle=PidHandle(pid=forked_pid, stdout=stdout_file, stderr=stderr_file),
            status="running",
        )
        self._records[mount_path] = record
        self._persist_record(record)
        self._start_drain(record)
        return record

    def _read_ready_event_from_file(self, stdout_file: Any) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.start_timeout_seconds
        while time.monotonic() < deadline:
            line = stdout_file.readline()
            if not line:
                # Pipe closed before ready event — give up.
                return None
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get(KEY_EVENT) in READY_READ_EVENTS:
                return payload
        return None

    def _get_or_create_baseline(self, python_executable: str) -> _BaselineHandle | None:
        if not self.enable_forkserver:
            return None
        with self._baselines_lock:
            existing = self._baselines.get(python_executable)
            if existing is not None and existing.subprocess.is_alive():
                return existing
            if existing is not None:
                self._baselines.pop(python_executable, None)
            try:
                handle = self._start_baseline(python_executable)
            except Exception:
                return None
            if handle is not None:
                self._baselines[python_executable] = handle
            return handle

    def _drop_baseline(self, python_executable: str) -> None:
        with self._baselines_lock:
            handle = self._baselines.pop(python_executable, None)
        if handle is None:
            return
        handle.subprocess.stop(timeout_seconds=0)

    def _start_baseline(self, python_executable: str) -> _BaselineHandle | None:
        # Unix-socket path is capped at 104 bytes on macOS / 108 on Linux. Pytest tmp paths
        # and deep instance dirs can blow past that, so we put baseline sockets under the
        # short system temp dir instead of `workers_root`.
        import tempfile

        socket_path = str(
            Path(tempfile.gettempdir()) / f"dssrv-baseline-{uuid.uuid4().hex[:12]}.sock"
        )
        prewarm_csv = ",".join(self.prewarm_packages)
        cmd = [
            python_executable,
            "-m",
            "dash_server_runtime.worker.baseline",
            socket_path,
            prewarm_csv,
        ]
        env = self._build_worker_env(extra_env=None)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        ready = self._read_ready_event(process)
        if ready is None or ready.get(KEY_EVENT) != EVENT_READY:
            # No drain thread is running yet, so use `communicate()` to flush stdout/stderr
            # while we wait — `SubprocessHandle.stop()` only does terminate+wait and would
            # deadlock if the baseline filled its pipe buffer before exiting.
            try:
                process.terminate()
                process.communicate(timeout=5)
            except Exception:
                pass
            return None
        threading.Thread(target=self._drain_baseline, args=(process.stdout,), daemon=True).start()
        return _BaselineHandle(
            python_executable=python_executable,
            socket_path=str(ready.get("socket_path") or socket_path),
            subprocess=SubprocessHandle(process),
            prewarmed_packages=tuple(ready.get("prewarmed_packages") or self.prewarm_packages),
        )

    def _drain_baseline(self, stdout: IO[str] | None) -> None:
        """Drain the baseline's stdout so its pipe buffer doesn't fill."""

        if stdout is None:
            return
        try:
            for _ in iter(stdout.readline, ""):
                pass
        except Exception:
            pass

    def _read_persisted_spec(self, mount_path: str) -> dict[str, Any] | None:
        """Return a ``start(**spec)``-compatible kwargs dict, or None if no spec exists."""

        for app_dir in self.workers_root.iterdir() if self.workers_root.exists() else []:
            if not app_dir.is_dir():
                continue
            for entry in app_dir.iterdir():
                if not entry.is_file() or entry.suffix != ".json":
                    continue
                try:
                    payload = json.loads(entry.read_text())
                except json.JSONDecodeError:
                    continue
                if payload.get("mount_path") != mount_path:
                    continue
                if not payload.get("app_source") or not payload.get("manifest"):
                    return None  # Older record predates the spec-preservation work.
                return {
                    "app_name": payload["app_name"],
                    "revision_number": int(payload["revision_number"]),
                    "mount_path": payload["mount_path"],
                    "app_source": Path(payload["app_source"]),
                    "manifest": payload["manifest"],
                    "python_executable": payload.get("python_executable"),
                    "environment_id": payload.get("environment_id"),
                    "extra_env": payload.get("extra_env"),
                }
        return None

    # ---- idle sweep ---------------------------------------------------------

    def start_idle_sweep(self) -> None:
        """Start the background thread that idle-stops quiet workers."""

        if self.idle_stop_seconds <= 0:
            return
        if self._idle_sweep_thread is not None and self._idle_sweep_thread.is_alive():
            return
        self._idle_sweep_stop.clear()
        self._idle_sweep_thread = threading.Thread(
            target=self._idle_sweep_loop, daemon=True
        )
        self._idle_sweep_thread.start()

    def stop_idle_sweep(self) -> None:
        self._idle_sweep_stop.set()
        thread = self._idle_sweep_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._idle_sweep_thread = None

    def _idle_sweep_loop(self) -> None:
        while not self._idle_sweep_stop.wait(self.idle_sweep_interval_seconds):
            try:
                self.run_idle_sweep_once()
            except Exception:
                # Sweep failures are operational; never propagate.
                continue

    def run_idle_sweep_once(self) -> list[str]:
        """One pass of the idle sweep. Returns mount paths that were idle-stopped."""

        if self.idle_stop_seconds <= 0:
            return []
        now = datetime.now(timezone.utc)
        stopped: list[str] = []
        with self._lock:
            snapshot = list(self._records.values())
        for record in snapshot:
            age = seconds_since(record.last_request_at or record.started_at, now)
            if age < self.idle_stop_seconds:
                continue
            # Worker is past idle threshold — stop it but preserve the spec for re-spawn.
            if self.stop(record.mount_path, idle=True):
                stopped.append(record.mount_path)
        return stopped




def _send_sigterm_then_kill(pid: int, *, timeout_seconds: float = 10.0) -> None:
    """Polite stop: SIGTERM, wait, then SIGKILL if the process is still up."""

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except ChildProcessError:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass


def _read_process_rss(pid: int) -> int | None:
    """Best-effort RSS read. Tries psutil, then /proc, then `ps`."""

    try:
        import psutil  # type: ignore

        return int(psutil.Process(pid).memory_info().rss)
    except Exception:
        pass

    proc_status = Path(f"/proc/{pid}/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except Exception:
            pass

    try:
        import shutil as _shutil

        if _shutil.which("ps"):
            result = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            value = result.stdout.strip()
            if value.isdigit():
                return int(value) * 1024
    except Exception:
        pass
    return None
