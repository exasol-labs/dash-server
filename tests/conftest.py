from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from dash_server.app_factory import create_app

from _helpers import base_test_config, hosted_test_config


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
