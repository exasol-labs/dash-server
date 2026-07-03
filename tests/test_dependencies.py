from __future__ import annotations

import json
import sys
from pathlib import Path

from dash_server.dependencies import DependencyInstaller
from dash_server.workspace.service import WorkspaceService


def test_dependency_installer_caches_successful_installs(tmp_path: Path, monkeypatch):
    installer = DependencyInstaller(
        str(tmp_path / "dependency_state"),
        enabled=True,
        python_executable="/tmp/fake-python",
        timeout_seconds=30,
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str]):
        calls.append(command)
        return {
            "status": "succeeded",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(installer, "_run_install_command", fake_run)

    first = installer.ensure_requirements("sales", ["dash", "plotly"])
    second = installer.ensure_requirements("sales", ["dash", "plotly"])

    assert first["status"] == "succeeded"
    assert second["status"] == "cached"
    assert len(calls) == 1


def test_dependency_installer_force_clean_bypasses_cached_state(tmp_path: Path, monkeypatch):
    installer = DependencyInstaller(
        str(tmp_path / "dependency_state"),
        enabled=True,
        python_executable="/tmp/fake-python",
        timeout_seconds=30,
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str]):
        calls.append(command)
        return {
            "status": "succeeded",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(installer, "_run_install_command", fake_run)

    first = installer.ensure_requirements("sales", ["dash", "plotly"])
    second = installer.ensure_requirements("sales", ["dash", "plotly"])
    third = installer.ensure_requirements("sales", ["dash", "plotly"], force_clean=True)

    assert first["status"] == "succeeded"
    assert second["status"] == "cached"
    assert third["status"] == "succeeded"
    assert third["force_clean"] is True
    assert "bypassing cached dependency state" in third["notes"]
    assert len(calls) == 2


def test_workspace_validation_marks_missing_declared_dependency_when_install_disabled(
    tmp_path: Path,
):
    workspace = WorkspaceService(str(tmp_path / "workspaces"))
    app_dir = tmp_path / "workspaces" / "test"
    app_dir.mkdir(parents=True)
    (app_dir / "dash-app.json").write_text(
        json.dumps(
            {
                "name": "test",
                "title": "Test",
                "route": "/apps/test",
                "description": "Test app.",
                "template": "metric-cards",
            }
        )
    )
    (app_dir / "requirements.txt").write_text("missing-package-demo\n")
    (app_dir / "app.py").write_text(
        "import missing_package_demo\n\n"
        "from dash import Dash, html\n\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    app = Dash(__name__, server=server, routes_pathname_prefix='/', requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
        "    app.layout = html.Div(['ok'])\n"
        "    return app\n"
    )
    (app_dir / ".draft-state.json").write_text(json.dumps({"candidate_version": 1}))

    validation = workspace.validate_workspace("test", mount_path="/apps/test")

    assert validation["is_valid"] is False
    assert validation["dependency_install"]["status"] == "ready"
    assert validation["imports"]["category"] == "environment_missing_dependency"
    assert validation["imports"]["missing_dependency"] == "missing_package_demo"
    assert validation["imports"]["declared_in_requirements"] is True


# --- Phase 1 regression tests (DependencyEnvironmentService) ---

from dash_server.dependencies import DependencyEnvironmentService


def test_environment_id_is_stable_for_identical_requirements(tmp_path):
    svc_a = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs_a"),
        wheel_cache_root=str(tmp_path / "wheels_a"),
        pycache_root=str(tmp_path / "pyc_a"),
    )
    svc_b = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs_b"),
        wheel_cache_root=str(tmp_path / "wheels_b"),
        pycache_root=str(tmp_path / "pyc_b"),
    )
    reqs = ["dash>=4.0,<5.0", "plotly>=5.18", "pyexasol>=2.2.2,<3.0"]
    # Order doesn't matter — env ids are computed against the sorted+normalized list.
    assert svc_a.compute_environment_id(reqs) == svc_b.compute_environment_id(list(reversed(reqs)))


def test_environment_id_changes_with_requirement_set(tmp_path):
    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
    )
    a = svc.compute_environment_id(["dash>=4.0,<5.0", "plotly>=5.18"])
    b = svc.compute_environment_id(["dash>=4.0,<5.0", "plotly>=5.18", "pandas>=2.0"])
    assert a != b


def test_environment_service_returns_skipped_for_no_requirements(tmp_path):
    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
    )
    result = svc.ensure_requirements("any-app", [])
    assert result["status"] == "skipped"


def test_environment_service_disabled_short_circuits(tmp_path):
    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        enabled=False,
    )
    result = svc.ensure_requirements("any-app", ["dash>=4.0,<5.0"])
    assert result["status"] == "disabled"
    assert "Per-app dependency environments are disabled" in result["notes"]


def test_environment_service_does_not_use_sys_executable_for_user_installs(tmp_path, monkeypatch):
    """Phase 1 acceptance: dashboard requirement installs never target sys.executable."""
    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        captured.append(list(command))
        # Pretend the install succeeded; the env directory already exists because EnvBuilder ran.
        return _FakeCompleted()

    # Skip the venv creation step too — we don't need a real interpreter for this assertion.
    class _FakeBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, env_dir):
            from pathlib import Path
            import os
            base = Path(env_dir)
            (base / "bin").mkdir(parents=True, exist_ok=True)
            python_path = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.touch()

    monkeypatch.setattr("dash_server.dependencies.environment_service.venv.EnvBuilder", _FakeBuilder)
    monkeypatch.setattr("dash_server.dependencies.environment_service.subprocess.run", fake_run)

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        helper_package_source=None,  # skip helper install in this test
    )
    result = svc.ensure_requirements("app-a", ["pandas>=2.0", "statsmodels>=0.14"])
    assert result["status"] == "succeeded", result
    assert result["python_executable"] != sys.executable
    # All pip-invoking commands must use the env's own python, never the server's.
    assert captured, "Expected at least one pip command"
    for cmd in captured:
        assert cmd[0] != sys.executable, f"Install used sys.executable: {cmd}"
        assert cmd[0] == result["python_executable"], cmd


def test_environment_service_caches_by_environment_id(tmp_path, monkeypatch):

    run_count = {"n": 0}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        run_count["n"] += 1
        return _FakeCompleted()

    class _FakeBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, env_dir):
            from pathlib import Path
            import os
            base = Path(env_dir)
            python_path = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.touch()

    monkeypatch.setattr("dash_server.dependencies.environment_service.venv.EnvBuilder", _FakeBuilder)
    monkeypatch.setattr("dash_server.dependencies.environment_service.subprocess.run", fake_run)

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        helper_package_source=None,
    )
    reqs = ["dash>=4.0,<5.0"]
    first = svc.ensure_requirements("app-a", reqs)
    pip_runs_after_first = run_count["n"]
    second = svc.ensure_requirements("app-b", reqs)  # different app, same requirements
    assert first["environment_id"] == second["environment_id"]
    assert second["status"] == "cached"
    # No new pip runs for the second app — env was reused.
    assert run_count["n"] == pip_runs_after_first


def test_environment_service_force_clean_rebuilds(tmp_path, monkeypatch):

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        return _FakeCompleted()

    class _FakeBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, env_dir):
            from pathlib import Path
            import os
            base = Path(env_dir)
            python_path = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.touch()

    monkeypatch.setattr("dash_server.dependencies.environment_service.venv.EnvBuilder", _FakeBuilder)
    monkeypatch.setattr("dash_server.dependencies.environment_service.subprocess.run", fake_run)

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        helper_package_source=None,
    )
    reqs = ["dash>=4.0,<5.0"]
    first = svc.ensure_requirements("app-a", reqs)
    import time
    time.sleep(0.01)
    second = svc.ensure_requirements("app-a", reqs, force_clean=True)
    assert first["environment_id"] == second["environment_id"]
    assert second["status"] == "succeeded"
    assert second.get("installed_at") != first.get("installed_at")


# --- Phase 2 regression tests (subprocess validator) ---


def test_subprocess_validator_smoke_against_in_tree_app(tmp_path):
    """Phase 2 acceptance: the worker module's --mode=validate path runs end-to-end.

    Uses the current Python (no per-app env) — this is just a smoke check that the
    subprocess validator imports a tiny Dash factory and reports passed.
    """
    import subprocess as _sp
    import sys
    import json as _json

    app_py = tmp_path / "app.py"
    app_py.write_text(
        "from dash import Dash, html\n\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    app = Dash(__name__, server=server, routes_pathname_prefix='/',\n"
        "               requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
        "    app.layout = html.Div(metadata.get('title', 'worker-validate'))\n"
        "    return app\n"
    )
    manifest_payload = _json.dumps({"name": "worker-validate", "title": "Worker Validate", "route": "/apps/worker-validate"})

    result = _sp.run(
        [
            sys.executable, "-m", "dash_server.runtime.worker",
            "--mode=validate",
            "--app-name", "worker-validate",
            "--app-source", str(app_py),
            "--mount-path", "/apps/worker-validate",
            "--manifest-json", manifest_payload,
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    payload = _json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    assert payload["status"] == "passed"
    assert payload["callbacks"]["count"] == 0


def test_subprocess_validator_reports_import_failure(tmp_path):
    """An app.py whose top-level code raises should produce a failed JSON, not crash the parent."""
    import subprocess as _sp
    import sys
    import json as _json

    app_py = tmp_path / "app.py"
    app_py.write_text(
        "raise RuntimeError('boom during import')\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    pass\n"
    )
    result = _sp.run(
        [
            sys.executable, "-m", "dash_server.runtime.worker",
            "--mode=validate",
            "--app-name", "broken",
            "--app-source", str(app_py),
            "--mount-path", "/apps/broken",
            "--manifest-json", _json.dumps({"name": "broken", "title": "B", "route": "/apps/broken"}),
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    # Exit non-zero, but stdout still contains a parseable JSON result.
    assert result.returncode != 0
    payload = _json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    assert payload["status"] == "failed"
    assert "boom during import" in (payload.get("error") or "")
    assert payload.get("phase") == "import"


def test_workspace_dispatches_to_subprocess_when_dependency_install_reports_non_server_python(tmp_path, monkeypatch):
    """_import_smoke_check delegates to the subprocess path when dependency_install reports an env python."""
    from dash_server.workspace.service import WorkspaceService

    svc = WorkspaceService(str(tmp_path / "ws"))

    captured: dict = {}

    def fake_subprocess_check(self, **kwargs):
        captured.update(kwargs)
        return {"status": "passed", "callbacks": {"count": 0}, "subprocess": {"python_executable": kwargs["subprocess_python"]}}

    monkeypatch.setattr(WorkspaceService, "_import_smoke_check_subprocess", fake_subprocess_check, raising=True)

    # Now exercise _import_smoke_check via a hand-rolled call. We need a manifest object and
    # an app.py on disk for the path validation.
    from dash_server.registry.models import AppManifest

    app_workspace = tmp_path / "ws" / "verify-sub"
    app_workspace.mkdir(parents=True, exist_ok=True)
    (app_workspace / "app.py").write_text("def create_dash_app(*a, **k): pass\n")
    manifest = AppManifest(
        name="verify-sub",
        title="Verify Sub",
        route="/apps/verify-sub",
        description=None,
        template=None,
        data_sources=None,
    )

    fake_python = str(tmp_path / "fake-env" / "bin" / "python")
    dependency_install = {
        "status": "succeeded",
        "python_executable": fake_python,
        "environment_id": "sha256:test",
    }
    result = svc._import_smoke_check(
        "verify-sub",
        manifest,
        declared_packages=[],
        dependency_install=dependency_install,
        mount_path=None,
    )
    assert result["status"] == "passed"
    assert captured["subprocess_python"] == fake_python
    assert captured["dependency_install"]["environment_id"] == "sha256:test"
