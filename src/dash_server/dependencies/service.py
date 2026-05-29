"""Lightweight dependency installation for validation and build workflows."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DependencyInstaller:
    """Install declared requirements into the current Python environment with per-app caching."""

    def __init__(
        self,
        state_root: str,
        *,
        enabled: bool,
        python_executable: str,
        timeout_seconds: int,
    ) -> None:
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds

    def ensure_requirements(
        self,
        app_name: str,
        requirements: list[str],
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
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
                "notes": "Automatic dependency install is disabled for this server.",
            }

        state_path = self.state_root / f"{app_name}.json"
        requirements_hash = self._requirements_hash(requirements)
        state = self._read_state(state_path)
        if (
            not force_clean
            and (
            state.get("requirements_hash") == requirements_hash
            and state.get("python_executable") == self.python_executable
            )
        ):
            return {
                "status": "cached",
                "requirements": requirements,
                "installed_at": state.get("installed_at"),
                "notes": "Declared requirements are already installed for this server environment.",
            }

        command = [self.python_executable, "-m", "pip", "install", *requirements]
        result = self._run_install_command(command)
        if result["status"] != "succeeded":
            return {
                "status": result["status"],
                "requirements": requirements,
                "command": command,
                "stdout_tail": result.get("stdout_tail"),
                "stderr_tail": result.get("stderr_tail"),
                "notes": result.get("notes"),
            }

        installed_at = self._timestamp()
        self._write_state(
            state_path,
            {
                "requirements_hash": requirements_hash,
                "requirements": requirements,
                "python_executable": self.python_executable,
                "installed_at": installed_at,
            },
        )
        return {
            "status": "succeeded",
            "requirements": requirements,
            "command": command,
            "installed_at": installed_at,
            "notes": (
                "Reinstalled declared requirements into the current server environment, bypassing cached dependency state."
                if force_clean
                else "Installed declared requirements into the current server environment."
            ),
            "force_clean": force_clean,
        }

    def _run_install_command(self, command: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "stdout_tail": (exc.stdout or "")[-4000:],
                "stderr_tail": (exc.stderr or "")[-4000:],
                "notes": "Dependency install timed out.",
            }

        if completed.returncode != 0:
            return {
                "status": "failed",
                "stdout_tail": completed.stdout[-4000:],
                "stderr_tail": completed.stderr[-4000:],
                "notes": "Dependency install command failed.",
            }
        return {
            "status": "succeeded",
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }

    def _read_state(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _write_state(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    def _requirements_hash(self, requirements: list[str]) -> str:
        encoded = "\n".join(requirements).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
