from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from dash_server.app_factory import create_app
from dash_server.runtime.worker_manager import AppWorkerManager

from _helpers import base_test_config, hosted_test_config


@pytest.fixture(autouse=True)
def _stop_worker_processes(monkeypatch: pytest.MonkeyPatch):
    """Stop every worker and forkserver baseline a test started.

    Workers deliberately outlive their manager (a restarted server adopts them), so a test
    that just drops its app or manager would leave them running after pytest exits.
    """

    managers: list[AppWorkerManager] = []
    original_init = AppWorkerManager.__init__

    def tracking_init(self: AppWorkerManager, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        managers.append(self)

    monkeypatch.setattr(AppWorkerManager, "__init__", tracking_init)
    yield
    for manager in managers:
        try:
            manager.stop_all()
        except Exception:
            pass


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    return create_app(base_test_config(tmp_path))


@pytest.fixture()
def client(app: Flask):
    return app.test_client()


@pytest.fixture()
def make_app(tmp_path: Path):
    """Factory fixture: build a local-mode app with per-test overrides."""

    def _make(**overrides: Any) -> Flask:
        return create_app(base_test_config(tmp_path, **overrides))

    return _make


@pytest.fixture()
def make_hosted_app(tmp_path: Path):
    """Factory fixture: build a hosted-mode (trusted-proxy) app with overrides."""

    def _make(**overrides: Any) -> Flask:
        return create_app(hosted_test_config(tmp_path, **overrides))

    return _make
