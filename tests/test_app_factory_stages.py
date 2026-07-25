"""Characterization tests for the ``create_app`` stage decomposition.

These pin the behavior-preserving contract of the refactored factory: the
instance-path precedence (config override beats ambient env var), the
``Roots`` write-backs, and the full-build gate the Wave 3 plan names.

The pure-function tests here are intentionally *not* marked ``slow`` so they
join the fast ``pytest -m "not slow"`` loop; only the full ``create_app``
construction is marked slow.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask
import pytest

from dash_server.app_factory import (
    Roots,
    _resolve_instance_path,
    _resolve_roots,
    create_app,
)
from dash_server.config import Config


def test_resolve_instance_path_config_override_beats_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``test_config["INSTANCE_PATH"]`` override wins over the ambient env var."""

    override = tmp_path / "override"
    monkeypatch.setenv("DASH_SERVER_INSTANCE_PATH", str(tmp_path / "from-env"))

    resolved = _resolve_instance_path({"INSTANCE_PATH": str(override)}, tmp_path / "proj")

    assert resolved == str(override)


def test_resolve_instance_path_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no config override, a runtime-set env var is honored (read live)."""

    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("DASH_SERVER_INSTANCE_PATH", str(env_dir))

    assert _resolve_instance_path(None, tmp_path / "proj") == str(env_dir)
    assert _resolve_instance_path({}, tmp_path / "proj") == str(env_dir)


def test_resolve_instance_path_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASH_SERVER_INSTANCE_PATH", raising=False)
    monkeypatch.delenv("FLASK_INSTANCE_PATH", raising=False)

    project_root = tmp_path / "proj"
    assert _resolve_instance_path(None, project_root) == str(project_root / "instance")


def test_resolve_roots_writes_back_app_roots(tmp_path: Path) -> None:
    """``_resolve_roots`` derives every root and writes the three APP_*_ROOT
    values back into ``app.config`` (load-bearing for downstream services)."""

    instance_path = tmp_path / "instance"
    app = Flask(__name__, instance_path=str(instance_path))
    app.config.from_object(Config)

    roots = _resolve_roots(app)

    assert isinstance(roots, Roots)
    # Defaults derive under the instance path when no per-subroot override is set.
    assert roots.db_path == Config.default_db_path(str(instance_path))
    assert roots.artifacts_root == Config.default_artifacts_root(str(instance_path))
    # The three write-backs must land in app.config.
    assert app.config["APP_ENVIRONMENTS_ROOT"] == roots.app_environments_root
    assert app.config["APP_WHEEL_CACHE_ROOT"] == roots.app_wheel_cache_root
    assert app.config["APP_PYCACHE_ROOT"] == roots.app_pycache_root


def test_resolve_roots_honors_per_subroot_override(tmp_path: Path) -> None:
    """An explicit per-subroot config value overrides the instance-path derivation."""

    app = Flask(__name__, instance_path=str(tmp_path / "instance"))
    app.config.from_object(Config)
    custom_db = str(tmp_path / "custom" / "registry.sqlite3")
    app.config["REGISTRY_DB_PATH"] = custom_db

    roots = _resolve_roots(app)

    assert roots.db_path == custom_db


@pytest.mark.slow
def test_instance_path_override_wins_over_env_full_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave 3 gate: build an app with test_config overriding INSTANCE_PATH while
    the env var is set to something else; the override wins end-to-end."""

    override_dir = tmp_path / "override-instance"
    env_dir = tmp_path / "env-instance"
    monkeypatch.setenv("DASH_SERVER_INSTANCE_PATH", str(env_dir))

    app = create_app({"INSTANCE_PATH": str(override_dir)})

    assert app.instance_path == str(override_dir)
    assert app.config["INSTANCE_PATH"] == str(override_dir)
    # Derived state lands under the override, not the env path.
    assert (override_dir / "dash_server.sqlite3").exists()
    assert not env_dir.exists()
