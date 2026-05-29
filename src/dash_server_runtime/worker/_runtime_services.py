"""Worker-side bootstrap for the runtime services hosted apps need.

The worker is given paths (``--gitops-repo-path``, ``--exasol-secrets-root``,
``--diagnostics-root``) plus a scoped set of env vars at spawn time. It uses those
paths to construct local instances of the same services the control plane runs.

The imports here are **conditional**: in a per-app environment that doesn't have
``dash_server`` installed, the bootstrap functions silently degrade and the worker
runs without those services. This is intentional — Phase 3.5a's promise is that
``dash_server_runtime`` alone is enough to serve a Dash app; Exasol bootstrap is a
nice-to-have layered on top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_exasol_service_for_worker(
    gitops_repo_path: str | None,
    exasol_secrets_root: str | None,
) -> tuple[Any | None, str | None]:
    """Return ``(service, error_message)``. ``service`` is None if bootstrap failed.

    Failures (e.g. ``dash_server`` not installed in this env) are surfaced as a
    structured warning so the manager can capture them, but they never raise.
    """

    if not gitops_repo_path:
        return None, None
    try:
        from dash_server.exasol import ExasolDashboardService
        from dash_server.gitops import GitRepoService
    except Exception as exc:
        return None, f"dash_server not available in worker env: {exc!s}"
    try:
        secrets_root = exasol_secrets_root or str(Path(gitops_repo_path).parent / "exasol-secrets")
        return (
            ExasolDashboardService(GitRepoService(gitops_repo_path), secrets_root),
            None,
        )
    except Exception as exc:
        return None, f"Could not initialize Exasol service in worker: {exc!s}"


def build_diagnostics_service_for_worker(diagnostics_root: str | None) -> tuple[Any | None, str | None]:
    if not diagnostics_root:
        return None, None
    try:
        from dash_server.diagnostics import DiagnosticsService
    except Exception as exc:
        return None, f"dash_server.diagnostics not available in worker env: {exc!s}"
    try:
        return DiagnosticsService(diagnostics_root), None
    except Exception as exc:
        return None, f"Could not initialize diagnostics service in worker: {exc!s}"
