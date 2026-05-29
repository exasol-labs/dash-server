from __future__ import annotations

from pathlib import Path
import sys

import pytest

from dash_server.app_factory import create_app


@pytest.fixture()
def app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
            "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
            "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
            "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state"),
            "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
            "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets"),
            "AUTO_INSTALL_DEPENDENCIES": False,
            "PYTHON_EXECUTABLE": sys.executable,
        }
    )
    return app


@pytest.fixture()
def client(app):
    return app.test_client()
