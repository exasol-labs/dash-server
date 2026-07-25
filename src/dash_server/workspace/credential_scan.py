"""Credential-safety scanning for draft workspaces.

Builds the ``credential_safety`` report used by
``WorkspaceService.validate_workspace``. Hosted apps must never embed database
credentials, read credential environment variables, or open direct database
connections; all of those must go through a server-side Exasol profile.
"""

from __future__ import annotations

import re
from typing import Any

from dash_server.artifacts_io import APP_MANIFEST_FILENAME
from dash_server.registry.models import AppManifest

EXASOL_ENV_PATTERN = re.compile(
    r"\b(?:EXA|EXASOL)_(?:DSN|USER|PASS|PASSWORD|PAT|ACCESS_TOKEN|REFRESH_TOKEN)\b"
)

_FORBIDDEN_DATA_SOURCE_KEYS = {
    "dsn",
    "user",
    "password",
    "access_token",
    "refresh_token",
    "saas_pat",
    "secret",
    "secret_ref",
}


def credential_safety_report(
    files: dict[str, str],
    manifest: AppManifest,
    *,
    python_files: dict[str, str],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    raw_data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else None
    if isinstance(raw_data_sources, dict):
        primary = raw_data_sources.get("primary")
        if isinstance(primary, dict) and primary.get("kind") == "exasol":
            forbidden_keys = sorted(
                key for key in primary if key in _FORBIDDEN_DATA_SOURCE_KEYS
            )
            if forbidden_keys:
                findings.append(
                    {
                        "path": APP_MANIFEST_FILENAME,
                        "message": (
                            "Exasol data_sources must reference a server-side profile and must not embed "
                            f"credential keys: {', '.join(forbidden_keys)}."
                        ),
                    }
                )

    for relative_path, content in python_files.items():
        if "pyexasol.connect" in content:
            findings.append(
                {
                    "path": relative_path,
                    "message": (
                        "Hosted apps must not call pyexasol.connect(...) directly. "
                        "Use a server-side Exasol profile and runtime helper instead."
                    ),
                }
            )
        if EXASOL_ENV_PATTERN.search(content):
            findings.append(
                {
                    "path": relative_path,
                    "message": (
                        "Hosted apps must not read EXA_/EXASOL_ credential environment variables directly. "
                        "Bind an Exasol profile and let the server resolve credentials."
                    ),
                }
            )
        if re.search(r"\b(?:password|access_token|refresh_token|saas_pat)\s*=", content):
            findings.append(
                {
                    "path": relative_path,
                    "message": (
                        "Hosted app source appears to define database credential parameters directly. "
                        "Move Exasol credentials into the server-side profile configuration."
                    ),
                }
            )

    status = "failed" if findings else "passed"
    return {"status": status, "findings": findings}
