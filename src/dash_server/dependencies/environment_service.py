"""Per-app dependency environments.

This is the Phase-1 piece of the runtime-isolation plan
(`plans/app-runtime-isolation-and-dependency-environments-plan.md`). It replaces the
"install requirements into the server interpreter" behavior of `DependencyInstaller`
with one venv per `dependency_lock_hash`, plus a content-addressed wheel cache shared
across envs via hardlinks.

The service exposes the same callable shape as `DependencyInstaller.ensure_requirements`
(`(app_name, requirements, *, force_clean=False) -> dict`) so the `WorkspaceService`
plumbing is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable

from ..timestamps import seconds_since


_HELPER_PACKAGE_NAME = "dash_server_runtime"


class DependencyEnvironmentService:
    """Materialize and reuse per-`dependency_lock_hash` Python environments.

    Storage layout::

        instance/
          app_envs/
            <env_id>/                  # one venv per identity
              bin/python
              lib/python3.X/site-packages/
              .env_record.json
            ...
          wheels/                     # shared wheel cache (UV_CACHE_DIR)
          pycache/                    # PYTHONPYCACHEPREFIX shared bytecode

    ``env_id`` is the sha256 of:
        - normalized requirements (newline-joined sorted lines)
        - base Python identity (executable + version)
        - platform tag
        - dash_server_runtime version

    Envs are content-addressable: identical requirement sets across apps share one env.
    The legacy ``DependencyInstaller`` state lives next door and is left untouched so
    operators can compare during the migration.
    """

    def __init__(
        self,
        *,
        environments_root: str,
        wheel_cache_root: str,
        pycache_root: str,
        enabled: bool = True,
        base_python_executable: str | None = None,
        timeout_seconds: int = 600,
        helper_package_name: str = _HELPER_PACKAGE_NAME,
        helper_package_source: str | Path | None = None,
        backend: str = "venv",
        diagnostics_service: Any | None = None,
        env_gc_interval_seconds: float = 300.0,
        wheel_cache_gc_interval_seconds: float = 600.0,
        env_gc_enabled: bool = False,
        wheel_cache_gc_enabled: bool = False,
        disk_cap_bytes: int | None = None,
        env_retention_seconds: float = 7 * 24 * 3600,
    ) -> None:
        self.environments_root = Path(environments_root)
        self.wheel_cache_root = Path(wheel_cache_root)
        self.pycache_root = Path(pycache_root)
        for path in (self.environments_root, self.wheel_cache_root, self.pycache_root):
            path.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.base_python_executable = base_python_executable or sys.executable
        self.timeout_seconds = timeout_seconds
        self.helper_package_name = helper_package_name
        # Path to the local source of the helper package — when set, we install it editable
        # rather than from PyPI. In dev installs (`pip install -e .`) this is `src/dash_server_runtime`.
        self.helper_package_source = (
            Path(helper_package_source) if helper_package_source else None
        )
        # Phase 5a: backend identity. Phase 1 ships only "venv" (stdlib pip). When the uv
        # backend lands, this becomes "uv". The wheel-cache GC checks this so it doesn't
        # accidentally prune a stdlib cache (where every file has st_nlink == 1).
        self.backend = backend
        # Phase 5b+5c: optional diagnostics service for GC event emission.
        self.diagnostics_service = diagnostics_service
        self.env_gc_interval_seconds = float(env_gc_interval_seconds)
        self.wheel_cache_gc_interval_seconds = float(wheel_cache_gc_interval_seconds)
        self.env_gc_enabled = bool(env_gc_enabled)
        self.wheel_cache_gc_enabled = bool(wheel_cache_gc_enabled)
        self.disk_cap_bytes = disk_cap_bytes
        self.env_retention_seconds = float(env_retention_seconds)
        # Reference-set provider (callable returning set[str]) so the env-GC sweep knows
        # which envs the registry considers live. Set by app_factory after construction.
        self.referenced_ids_provider: Callable[[], set[str]] | None = None
        # Background thread state.
        self._env_gc_thread: threading.Thread | None = None
        self._env_gc_stop = threading.Event()
        self._wheel_gc_thread: threading.Thread | None = None
        self._wheel_gc_stop = threading.Event()

    # ------------------------------------------------------------------ public

    def ensure_requirements(
        self,
        app_name: str,
        requirements: list[str],
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
        """Compatible with ``DependencyInstaller.ensure_requirements``."""

        if not requirements:
            return {
                "status": "skipped",
                "requirements": [],
                "notes": "No requirements declared for this workspace.",
            }
        if not self.enabled:
            return {
                "status": "disabled",
                "requirements": requirements,
                "notes": "Per-app dependency environments are disabled for this server.",
            }

        environment_id = self.compute_environment_id(requirements)
        env_dir = self.environments_root / environment_id

        if force_clean and env_dir.exists():
            shutil.rmtree(env_dir, ignore_errors=True)

        if env_dir.exists():
            record = self._read_record(env_dir)
            if record.get("status") == "ready":
                self._touch_last_used(env_dir, record)
                return self._success_payload(
                    environment_id, env_dir, record, cached=True, requirements=requirements
                )
            # Stale or partial install — wipe and rebuild.
            shutil.rmtree(env_dir, ignore_errors=True)

        return self._create_environment(
            environment_id=environment_id,
            env_dir=env_dir,
            app_name=app_name,
            requirements=requirements,
            force_clean=force_clean,
        )

    def compute_environment_id(self, requirements: Iterable[str]) -> str:
        """Stable hash of the inputs that determine env identity."""

        normalized = "\n".join(sorted(str(r).strip() for r in requirements if str(r).strip()))
        helper_version = self._helper_version()
        python_tag = self._base_python_tag()
        platform_tag = f"{platform.system()}/{platform.machine()}"
        digest = hashlib.sha256(
            (
                f"requirements:{normalized}\n"
                f"helper:{self.helper_package_name}=={helper_version}\n"
                f"python:{python_tag}\n"
                f"platform:{platform_tag}\n"
            ).encode()
        ).hexdigest()
        return f"sha256:{digest[:32]}"  # short prefix is fine; collisions vanishingly unlikely

    def lookup(self, environment_id: str) -> dict[str, Any] | None:
        env_dir = self.environments_root / environment_id
        if not env_dir.exists():
            return None
        record = self._read_record(env_dir)
        record.setdefault("environment_id", environment_id)
        record.setdefault("python_executable", str(self._env_python(env_dir)))
        return record

    def list_environments(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self.environments_root.exists():
            return results
        for entry in sorted(self.environments_root.iterdir()):
            if not entry.is_dir():
                continue
            record = self._read_record(entry)
            record.setdefault("environment_id", entry.name)
            record.setdefault("python_executable", str(self._env_python(entry)))
            record["bytes_on_disk"] = self._directory_size(entry)
            results.append(record)
        return results

    def total_bytes_on_disk(self) -> int:
        return self._directory_size(self.environments_root)

    def wheel_cache_bytes(self) -> int:
        return self._directory_size(self.wheel_cache_root)

    def invalidate(self, environment_id: str) -> bool:
        """Phase 4f: mark an env for removal on next GC pass. Returns True if it existed."""

        env_dir = self.environments_root / environment_id
        if not env_dir.exists():
            return False
        record = self._read_record(env_dir)
        record["status"] = "invalidated"
        record["invalidated_at"] = self._timestamp()
        self._write_record(env_dir, record)
        return True

    # ------------------------------------------------------------------ GC

    def run_env_gc_once(
        self,
        *,
        referenced_ids: set[str],
        disk_cap_bytes: int | None = None,
        retention_seconds: float = 7 * 24 * 3600,
    ) -> dict[str, list[str]]:
        """Phase 4d: one pass of environment GC.

        Eligibility: any env whose id is NOT in ``referenced_ids`` AND was last used more
        than ``retention_seconds`` ago. The retention floor protects against churn from a
        single deploy/promote cycle.

        When ``disk_cap_bytes`` is set and current usage exceeds the cap, eligible envs
        are evicted in LRU order (oldest ``last_used_at`` first) until usage drops below
        the cap. Without a cap, every eligible env over the retention floor is evicted.

        Invalidated envs (``invalidate(env_id)``) bypass the retention floor.

        Returns ``{"evicted": [...env_ids], "skipped_referenced": [...], "skipped_retained": [...]}``.
        """

        now = datetime.now(timezone.utc)
        evicted: list[str] = []
        skipped_referenced: list[str] = []
        skipped_retained: list[str] = []
        eligible: list[tuple[float, dict[str, Any]]] = []

        # Classification pass: split records into referenced / retained-by-floor / eligible.
        # When `disk_cap_bytes` is set we let the eviction loop apply cap pressure to every
        # non-referenced env regardless of retention; without a cap the retention floor wins.
        for record in self.list_environments():
            env_id = record.get("environment_id")
            if not isinstance(env_id, str):
                continue
            if env_id in referenced_ids:
                skipped_referenced.append(env_id)
                continue
            age = seconds_since(record.get("last_used_at") or record.get("created_at"), now)
            invalidated = record.get("status") == "invalidated"
            if not invalidated and age < retention_seconds and disk_cap_bytes is None:
                skipped_retained.append(env_id)
                continue
            eligible.append((age, record))

        # Oldest first so LRU eviction naturally targets stalest envs.
        eligible.sort(key=lambda pair: pair[0], reverse=True)

        # Eviction pass: one loop covers both modes — without a cap every eligible env is
        # evicted; with a cap, the loop stops as soon as usage drops below the cap and the
        # remainder lands in `skipped_retained` for the next pass to reconsider.
        current_bytes = self.total_bytes_on_disk() if disk_cap_bytes is not None else 0
        for age, record in eligible:
            if disk_cap_bytes is not None and current_bytes <= disk_cap_bytes:
                skipped_retained.append(record["environment_id"])
                continue
            env_id = record["environment_id"]
            size_before = int(record.get("bytes_on_disk") or 0)
            if not self._remove_env_dir(self.environments_root / env_id):
                continue
            evicted.append(env_id)
            current_bytes -= size_before
            self._emit_runtime_event(
                "env_evicted",
                {
                    "environment_id": env_id,
                    "bytes_freed": size_before,
                    "reason": (
                        "invalidated"
                        if record.get("status") == "invalidated"
                        else "disk_cap_exceeded"
                        if disk_cap_bytes is not None
                        else "past_retention"
                    ),
                    "age_seconds": int(age) if age != float("inf") else None,
                },
            )

        return {
            "evicted": evicted,
            "skipped_referenced": skipped_referenced,
            "skipped_retained": skipped_retained,
        }

    def run_wheel_cache_gc_once(
        self,
        *,
        disk_cap_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Phase 4e: prune orphan wheels from the content-addressed cache.

        Strategy: walk ``instance/wheels/`` and remove files whose ``st_nlink == 1`` — a
        file with link count 1 has no env hardlinking to it, so it's orphaned. The
        ``disk_cap_bytes`` parameter is honored only for the LRU tiebreaker when multiple
        files are eligible; orphans are always pruned regardless of cap.

        Phase 5a safety guard: this st_nlink strategy is correct only for the ``uv``
        backend, which hardlinks cached wheels into each env. On the stdlib (``venv``)
        backend, every cache file has ``st_nlink == 1`` by default, so we would prune
        the entire cache. Refuse to do anything destructive on backends other than ``uv``.
        Emit a ``wheel_cache_gc_skipped`` event so operators see why nothing happened.
        """

        if self.backend != "uv":
            self._emit_runtime_event(
                "wheel_cache_gc_skipped",
                {
                    "reason": "stdlib_backend_no_hardlinks",
                    "backend": self.backend,
                    "message": (
                        "Wheel-cache GC is disabled on this backend because the st_nlink "
                        "strategy would prune the entire cache. Set APP_WHEEL_CACHE_GC_"
                        "ENABLED=false or wait for the RECORD-file pruner to land."
                    ),
                },
            )
            return {
                "pruned": [],
                "kept": [],
                "bytes_freed": 0,
                "skipped_reason": "stdlib_backend_no_hardlinks",
                "backend": self.backend,
            }

        if not self.wheel_cache_root.exists():
            return {"pruned": [], "kept": [], "bytes_freed": 0}
        pruned: list[str] = []
        kept: list[str] = []
        bytes_freed = 0
        # Walk in deterministic order so tests are reproducible.
        for path in sorted(self.wheel_cache_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_nlink > 1:
                kept.append(str(path))
                continue
            try:
                path.unlink()
                pruned.append(str(path))
                bytes_freed += stat.st_size
                # Phase 5c: each prune is an audit event.
                self._emit_runtime_event(
                    "wheel_cache_pruned",
                    {"path": str(path), "bytes": int(stat.st_size)},
                )
            except OSError:
                kept.append(str(path))
        return {"pruned": pruned, "kept": kept, "bytes_freed": bytes_freed}

    def _remove_env_dir(self, env_dir: Path) -> bool:
        """Best-effort env directory removal."""

        try:
            shutil.rmtree(env_dir, ignore_errors=True)
            return not env_dir.exists()
        except Exception:
            return False

    # ---- runtime event emission --------------------------------------------

    def _emit_runtime_event(self, event_name: str, data: dict[str, Any]) -> None:
        """Server-wide ``runtime.events`` channel — delegates to DiagnosticsService.emit_event.

        Uses ``__runtime__`` as the pseudo-app name so the events surface under
        ``dash://runtime/logs/runtime.events`` rather than getting mixed into any single
        app's log channel.
        """

        if self.diagnostics_service is None:
            return
        self.diagnostics_service.emit_event(
            "__runtime__",
            "runtime.events",
            event_name,
            f"{event_name}: {data}",
            **data,
        )

    # ---- background GC drivers --------------------------------------------

    def start_env_gc(self) -> None:
        """Phase 5b: launch a daemon thread that calls run_env_gc_once() on a timer."""

        if not self.env_gc_enabled:
            return
        if self._env_gc_thread is not None and self._env_gc_thread.is_alive():
            return
        self._env_gc_stop.clear()
        self._env_gc_thread = threading.Thread(
            target=self._env_gc_loop, daemon=True, name="dash-server-env-gc"
        )
        self._env_gc_thread.start()

    def stop_env_gc(self) -> None:
        self._env_gc_stop.set()
        thread = self._env_gc_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._env_gc_thread = None

    def start_wheel_cache_gc(self) -> None:
        """Phase 5b: same shape as start_env_gc, for the wheel cache."""

        if not self.wheel_cache_gc_enabled:
            return
        if self._wheel_gc_thread is not None and self._wheel_gc_thread.is_alive():
            return
        self._wheel_gc_stop.clear()
        self._wheel_gc_thread = threading.Thread(
            target=self._wheel_gc_loop, daemon=True, name="dash-server-wheel-gc"
        )
        self._wheel_gc_thread.start()

    def stop_wheel_cache_gc(self) -> None:
        self._wheel_gc_stop.set()
        thread = self._wheel_gc_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._wheel_gc_thread = None

    def _env_gc_loop(self) -> None:
        while not self._env_gc_stop.wait(self.env_gc_interval_seconds):
            try:
                referenced = (
                    self.referenced_ids_provider() if self.referenced_ids_provider else set()
                )
                self.run_env_gc_once(
                    referenced_ids=referenced,
                    disk_cap_bytes=self.disk_cap_bytes,
                    retention_seconds=self.env_retention_seconds,
                )
            except Exception:
                # GC failures are operational; never propagate from the daemon thread.
                continue

    def _wheel_gc_loop(self) -> None:
        while not self._wheel_gc_stop.wait(self.wheel_cache_gc_interval_seconds):
            try:
                self.run_wheel_cache_gc_once()
            except Exception:
                continue

    # ----------------------------------------------------------------- private

    def _create_environment(
        self,
        *,
        environment_id: str,
        env_dir: Path,
        app_name: str,
        requirements: list[str],
        force_clean: bool,
    ) -> dict[str, Any]:
        env_dir.mkdir(parents=True, exist_ok=True)
        install_log_path = env_dir / "install.log"
        record_pending = {
            "environment_id": environment_id,
            "status": "creating",
            "created_at": self._timestamp(),
            "requirements": requirements,
            "requirements_hash": self._requirements_hash(requirements),
            "python_executable": str(self._env_python(env_dir)),
            "base_python_executable": self.base_python_executable,
            "platform": f"{platform.system()}/{platform.machine()}",
            "helper_package": {
                "name": self.helper_package_name,
                "version": self._helper_version(),
            },
            "backend": "venv",
            "install_log_path": str(install_log_path),
        }
        self._write_record(env_dir, record_pending)

        try:
            # 1) Create the venv. Setting `with_pip=True` so we can install into it.
            builder = venv.EnvBuilder(with_pip=True, clear=False, system_site_packages=False)
            builder.create(str(env_dir))

            python_executable = self._env_python(env_dir)

            # 2) Install the helper package. Prefer the local source (editable in dev installs)
            #    so the worker sees exactly what the control plane does.
            helper_target = self._helper_install_target()
            commands: list[list[str]] = []
            if helper_target:
                commands.append(
                    [str(python_executable), "-m", "pip", "install", "--no-build-isolation", helper_target]
                )
            # 3) Install user requirements with the wheel cache as the find-links source.
            commands.append(
                [
                    str(python_executable),
                    "-m",
                    "pip",
                    "install",
                    "--cache-dir",
                    str(self.wheel_cache_root),
                    *requirements,
                ]
            )

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            with install_log_path.open("w", encoding="utf-8") as log_fh:
                for command in commands:
                    log_fh.write(f"$ {' '.join(command)}\n")
                    try:
                        result = subprocess.run(
                            command,
                            capture_output=True,
                            text=True,
                            timeout=self.timeout_seconds,
                            check=False,
                        )
                    except subprocess.TimeoutExpired as exc:
                        log_fh.write(f"TIMEOUT after {self.timeout_seconds}s\n")
                        return self._failure_payload(
                            env_dir,
                            environment_id,
                            requirements,
                            command,
                            stdout=_decode_stream_tail(exc.stdout),
                            stderr=_decode_stream_tail(exc.stderr),
                            notes="Dependency install timed out.",
                            install_log_path=install_log_path,
                        )
                    if result.stdout:
                        log_fh.write(result.stdout)
                        stdout_chunks.append(result.stdout)
                    if result.stderr:
                        log_fh.write(result.stderr)
                        stderr_chunks.append(result.stderr)
                    log_fh.write(f"\n(exit={result.returncode})\n\n")
                    if result.returncode != 0:
                        return self._failure_payload(
                            env_dir,
                            environment_id,
                            requirements,
                            command,
                            stdout="\n".join(stdout_chunks)[-4000:],
                            stderr="\n".join(stderr_chunks)[-4000:],
                            notes="Dependency install command failed.",
                            install_log_path=install_log_path,
                        )

            # 4) Mark the env ready and persist the record.
            timestamp = self._timestamp()
            record = {
                **record_pending,
                "status": "ready",
                "installed_at": timestamp,
                "last_used_at": timestamp,
                "force_clean": force_clean,
            }
            self._write_record(env_dir, record)
            return self._success_payload(
                environment_id, env_dir, record, cached=False, requirements=requirements
            )
        except Exception as exc:
            return self._failure_payload(
                env_dir,
                environment_id,
                requirements,
                command=None,
                stdout="",
                stderr=str(exc),
                notes=f"Unexpected error during environment creation: {exc!s}",
                install_log_path=install_log_path,
            )

    def _success_payload(
        self,
        environment_id: str,
        env_dir: Path,
        record: dict[str, Any],
        *,
        cached: bool,
        requirements: list[str],
    ) -> dict[str, Any]:
        return {
            "status": "cached" if cached else "succeeded",
            "requirements": requirements,
            "environment_id": environment_id,
            "python_executable": record.get("python_executable") or str(self._env_python(env_dir)),
            "backend": record.get("backend", "venv"),
            "installed_at": record.get("installed_at"),
            "install_log_path": record.get("install_log_path"),
            "bytes_on_disk": self._directory_size(env_dir),
            "notes": (
                "Reusing per-app environment matched by dependency_lock_hash."
                if cached
                else "Created a fresh per-app environment for these requirements."
            ),
        }

    def _failure_payload(
        self,
        env_dir: Path,
        environment_id: str,
        requirements: list[str],
        command: list[str] | None,
        *,
        stdout: str,
        stderr: str,
        notes: str,
        install_log_path: Path,
    ) -> dict[str, Any]:
        record = self._read_record(env_dir)
        record.update(
            {
                "environment_id": environment_id,
                "status": "failed",
                "failed_at": self._timestamp(),
            }
        )
        self._write_record(env_dir, record)
        return {
            "status": "failed",
            "requirements": requirements,
            "environment_id": environment_id,
            "command": command,
            "stdout_tail": stdout,
            "stderr_tail": stderr,
            "notes": notes,
            "install_log_path": str(install_log_path),
        }

    def _read_record(self, env_dir: Path) -> dict[str, Any]:
        record_path = env_dir / ".env_record.json"
        if not record_path.exists():
            return {}
        try:
            payload: Any = json.loads(record_path.read_text())
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_record(self, env_dir: Path, payload: dict[str, Any]) -> None:
        record_path = env_dir / ".env_record.json"
        record_path.write_text(json.dumps(payload, indent=2))

    def _touch_last_used(self, env_dir: Path, record: dict[str, Any]) -> None:
        record["last_used_at"] = self._timestamp()
        self._write_record(env_dir, record)

    def _env_python(self, env_dir: Path) -> Path:
        if os.name == "nt":
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    def _requirements_hash(self, requirements: list[str]) -> str:
        normalized = "\n".join(sorted(str(r).strip() for r in requirements if str(r).strip()))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _helper_version(self) -> str:
        try:
            import importlib.metadata as md

            return md.version(self.helper_package_name)
        except Exception:
            try:
                import dash_server_runtime

                return getattr(dash_server_runtime, "__version__", "0.0.0")
            except Exception:
                return "0.0.0"

    def _helper_install_target(self) -> str | None:
        """Return a `pip install` target for the helper package, or None to skip."""

        # If we have a local source path, install editable from it. This is the dev path.
        if self.helper_package_source and self.helper_package_source.exists():
            return f"-e{self.helper_package_source}"  # pip accepts -e<path> as one arg
        # In a future hosted release we'd `return self.helper_package_name` (PyPI). For now
        # the package is not on PyPI; if no source path is given, skip the helper install and
        # let validation flag any missing imports.
        return None

    def _base_python_tag(self) -> str:
        return f"{self.base_python_executable}|{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _directory_size(self, path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total


def _decode_stream_tail(value: bytes | str | None, *, limit: int = 4000) -> str:
    """Coerce a `subprocess` stdout/stderr tail to `str` regardless of buffer kind."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value[-limit:].decode("utf-8", errors="replace")
    return value[-limit:]


