"""Lightweight dependency installation for validation and build workflows."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.markers import UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement

from dash_server.timestamps import now_iso


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

        conflicts = self._server_conflicts(requirements)
        if conflicts:
            # These packages are the server's own: installing would change them for the
            # server and every other app sharing this environment.
            return {
                "status": "failed",
                "requirements": requirements,
                "conflicts": conflicts,
                "notes": (
                    "These requirements would change packages dash-server itself runs on, in the environment "
                    f"every app on this server shares: {'; '.join(conflicts)}. Widen them to accept the installed "
                    "version (or drop them: the server provides these packages), or give apps their own "
                    "environments with DASH_SERVER_APP_DEPENDENCY_ISOLATION=per_app."
                ),
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

        if not force_clean and self._requirements_already_satisfied(requirements):
            return {
                "status": "already_satisfied",
                "requirements": requirements,
                "notes": "Declared requirements are already present in the server environment; nothing to install.",
            }

        command = self._install_command(requirements)
        if command is None:
            return {
                "status": "failed",
                "requirements": requirements,
                "notes": (
                    f"Cannot install dependencies: {self.python_executable} has no pip module and "
                    "no `uv` executable is on PATH. Install pip into the server environment "
                    "(e.g. `uv pip install pip`) or put `uv` on PATH, then restart dash-server."
                ),
            }
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

    def _targets_server_environment(self) -> bool:
        return Path(self.python_executable).absolute() == Path(sys.executable).absolute()

    def _server_conflicts(self, requirements: list[str]) -> list[str]:
        """Requirements that exclude the installed version of a package in the server's environment."""

        if not self._targets_server_environment():
            return []
        conflicts = []
        for entry in requirements:
            try:
                requirement = Requirement(entry)
            except InvalidRequirement:
                continue
            try:
                if requirement.marker is not None and not requirement.marker.evaluate():
                    continue
            except (UndefinedComparison, UndefinedEnvironmentName):
                pass
            try:
                installed_version = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                continue
            if not requirement.specifier.contains(installed_version, prereleases=True):
                conflicts.append(f"{entry} (the server has {requirement.name} {installed_version})")
        return conflicts

    def _server_constraints_file(self) -> Path:
        """Pin every package installed in the server's environment, so an install only adds packages.

        Covers what a requirement pulls in transitively, which ``_server_conflicts`` can't see.
        """

        pins = sorted(
            {
                f"{distribution.metadata['Name']}=={distribution.version}"
                for distribution in metadata.distributions()
                if distribution.metadata["Name"]
            },
            key=str.lower,
        )
        path = self.state_root / "server-constraints.txt"
        path.write_text("\n".join(pins) + "\n", encoding="utf-8")
        return path

    def _requirements_already_satisfied(self, requirements: list[str]) -> bool:
        """Return whether every requirement is met by the running server interpreter.

        Only answerable in-process when the install target *is* this interpreter; for any
        other target we cannot inspect its packages, so report ``False`` and install.
        Requirements with extras or direct URLs are treated as unsatisfied because their
        installed state cannot be verified from distribution metadata alone.
        """

        if not self._targets_server_environment():
            return False
        for entry in requirements:
            try:
                requirement = Requirement(entry)
            except InvalidRequirement:
                return False
            try:
                if requirement.marker is not None and not requirement.marker.evaluate():
                    continue
            except (UndefinedComparison, UndefinedEnvironmentName):
                # Unevaluable marker: let the real installer decide (and report) instead.
                return False
            if requirement.extras or requirement.url:
                return False
            try:
                installed_version = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                return False
            if not requirement.specifier.contains(installed_version, prereleases=True):
                return False
        return True

    def _install_command(self, requirements: list[str]) -> list[str] | None:
        """Pick an installer for the target interpreter: its own pip, else ``uv pip``.

        Environments created by ``uv venv`` (the README's default install path) ship
        without pip, so ``python -m pip`` alone is not a safe assumption.
        """

        constraints: list[str] = []
        if self._targets_server_environment():
            constraints = ["--constraint", str(self._server_constraints_file())]
        if self._has_pip():
            return [self.python_executable, "-m", "pip", "install", *constraints, *requirements]
        uv_executable = shutil.which("uv")
        if uv_executable is not None:
            return [uv_executable, "pip", "install", "--python", self.python_executable, *constraints, *requirements]
        return None

    def _has_pip(self) -> bool:
        try:
            completed = subprocess.run(
                [self.python_executable, "-m", "pip", "--version"],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

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
        return now_iso()
