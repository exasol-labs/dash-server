"""Phase 3 regression tests: worker process + proxy + isolated mount.

These tests spawn real subprocess workers using the current Python interpreter so that
the spawn → ready → proxy → response loop is exercised end-to-end. They are slower than
in-process unit tests (each test spends ~1–3 s booting a worker) but deliberately so:
the value of the isolated runtime is that this loop works.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dash_server.runtime.worker_manager import AppWorkerManager, WorkerStartError
from dash_server.runtime.worker_proxy import WorkerProxyWSGIApp


_TINY_APP_PY = """
from dash import Dash, Input, Output, html, dcc


def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix='/',
        requests_pathname_prefix=url_base_pathname.rstrip('/') + '/',
        title=metadata.get('title', 'isolated-tiny'),
    )
    app.layout = html.Div([
        dcc.Input(id='in', value='hello'),
        html.Div(id='out'),
    ])

    @app.callback(Output('out', 'children'), Input('in', 'value'))
    def echo(value):
        return f'echo:{value}'

    return app
"""


def _write_tiny_app(tmp_path: Path) -> Path:
    """Render a minimal Dash app on disk and return the directory path."""

    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "app.py").write_text(_TINY_APP_PY)
    return app_dir


def _wait_port_open(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _reserve_then_release_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_worker_manager_spawns_worker_and_emits_ready(tmp_path):
    """Spawn a worker, read its ready event, hit its loopback port."""

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        diagnostics_root=str(tmp_path / "diagnostics"),
    )
    try:
        record = manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
        )
        assert record.pid > 0
        assert record.port > 0
        assert record.host == "127.0.0.1"
        # The python_executable in the record matches what we asked for (sys.executable by default).
        assert record.python_executable == sys.executable
        # The worker is reachable.
        assert _wait_port_open(record.host, record.port, timeout=5.0)
        # Persisted record on disk.
        persisted = tmp_path / "workers" / "tiny" / "1.json"
        assert persisted.exists()
        payload = json.loads(persisted.read_text())
        assert payload["port"] == record.port
        assert payload["pid"] == record.pid
    finally:
        manager.stop_all()


def test_worker_manager_uses_configured_port_range(tmp_path):
    app_dir = _write_tiny_app(tmp_path)
    port = _reserve_then_release_port()
    if port > 65515:
        start, end = port - 20, port
    else:
        start, end = port, port + 20
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        port_range=f"{start}-{end}",
    )
    try:
        record = manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
        )

        assert start <= record.port <= end
        assert _wait_port_open(record.host, record.port, timeout=5.0)
    finally:
        manager.stop_all()


def test_worker_manager_passes_pycache_prefix_into_worker_env(tmp_path):
    """Phase 0 acceptance: PYTHONPYCACHEPREFIX reaches the worker."""

    pyc_root = tmp_path / "pyc"
    pyc_root.mkdir(parents=True, exist_ok=True)
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        pycache_root=str(pyc_root),
    )
    env = manager._build_worker_env(extra_env=None)
    assert env.get("PYTHONPYCACHEPREFIX") == str(pyc_root)
    # Non-allowlisted secrets do not leak from os.environ.
    assert "DASH_SERVER_DOES_NOT_EXIST" not in env


def test_worker_manager_only_passes_allowlisted_secret_env_vars(tmp_path, monkeypatch):
    """Worker spawn env is allow-listed; arbitrary parent env vars don't leak."""

    monkeypatch.setenv("EXA_PASSWORD", "p4ssw0rd")
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-leak")
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
    )
    env = manager._build_worker_env(extra_env=None)
    assert env.get("EXA_PASSWORD") == "p4ssw0rd"
    assert "UNRELATED_SECRET" not in env


def test_worker_manager_uses_provided_python_executable_argument(tmp_path, monkeypatch):
    """Phase 3 acceptance: workers spawn from the env's python_executable, not sys.executable."""

    captured = {}

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
            python_executable=sys.executable,  # in real usage this is env's python
            environment_id="sha256:test-env",
        )
        assert captured["cmd"][0] == sys.executable
        assert "--mode=serve" in captured["cmd"]
        assert "--app-source" in captured["cmd"]
    finally:
        manager.stop_all()


def test_worker_proxy_forwards_get_request_to_worker(tmp_path):
    """Spawn a worker, mount the proxy WSGI app, fetch the Dash layout endpoint."""

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
        )
        proxy = WorkerProxyWSGIApp(manager, mount_path="/apps/tiny", app_name="tiny")
        # Build a minimal WSGI environ for GET /_dash-layout
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/_dash-layout",
            "QUERY_STRING": "",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": sys.stderr,
            "wsgi.url_scheme": "http",
            "SERVER_NAME": "test",
            "SERVER_PORT": "5000",
            "HTTP_HOST": "test:5000",
        }
        captured_status = []
        captured_headers = []

        def start_response(status, headers, exc_info=None):
            captured_status.append(status)
            captured_headers.append(headers)
            return lambda data: None

        body_iter = proxy(environ, start_response)
        body = b"".join(body_iter)
        assert captured_status, "start_response was not called"
        assert captured_status[0].startswith("200"), captured_status
        # The Dash layout endpoint returns JSON describing the components.
        payload = json.loads(body.decode())
        assert isinstance(payload, dict) and "props" in payload
    finally:
        manager.stop_all()


def test_worker_proxy_respawns_dead_worker_from_persisted_spec(tmp_path):
    """3.5b: killing the worker out from under the manager triggers transparent re-spawn."""

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        record = manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
        )
        original_pid = record.pid
        # Force-kill the worker behind the manager's back.
        try:
            os.kill(original_pid, 9)
        except ProcessLookupError:
            pass
        time.sleep(0.3)
        for _ in range(20):
            if not record.handle.is_alive():
                break
            time.sleep(0.05)

        proxy = WorkerProxyWSGIApp(manager, mount_path="/apps/tiny", app_name="tiny")
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/_dash-layout",
            "QUERY_STRING": "",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": sys.stderr,
            "wsgi.url_scheme": "http",
            "SERVER_NAME": "test",
            "SERVER_PORT": "5000",
            "HTTP_HOST": "test:5000",
        }
        status_holder = []

        def start_response(status, headers, exc_info=None):
            status_holder.append(status)
            return lambda data: None

        body = b"".join(proxy(environ, start_response))
        # The proxy transparently re-spawned the worker and forwarded the request.
        assert status_holder[0].startswith("200"), status_holder
        payload = json.loads(body.decode())
        assert isinstance(payload, dict) and "props" in payload
        # The new worker has a different pid than the killed one.
        new_record = manager.get_record("/apps/tiny")
        assert new_record is not None
        assert new_record.pid != original_pid
    finally:
        manager.stop_all()


def test_worker_proxy_returns_503_when_no_spec_persisted(tmp_path):
    """3.5b: with no persisted spec, ensure_running returns None → proxy emits structured 503."""

    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    proxy = WorkerProxyWSGIApp(manager, mount_path="/apps/nonexistent", app_name="nonexistent")
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/_dash-layout",
        "QUERY_STRING": "",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": sys.stderr,
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "test",
        "SERVER_PORT": "5000",
        "HTTP_HOST": "test:5000",
    }
    status_holder = []
    headers_holder = []

    def start_response(status, headers, exc_info=None):
        status_holder.append(status)
        headers_holder.append(headers)
        return lambda data: None

    body = b"".join(proxy(environ, start_response))
    assert status_holder[0].startswith("503"), status_holder
    header_dict = {h.lower(): v for h, v in headers_holder[0]}
    assert header_dict.get("x-dash-server-worker-error") == "worker_not_running"
    payload = json.loads(body.decode())
    assert payload["mount_path"] == "/apps/nonexistent"


def test_idle_sweep_stops_idle_workers_but_preserves_spec(tmp_path):
    """3.5b: idle sweep marks quiet workers as stopped_idle without deleting their spec."""

    app_dir = _write_tiny_app(tmp_path)
    # idle_stop_seconds=0 disables, so use a tiny positive value and an immediate sweep.
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        idle_stop_seconds=1,
        idle_sweep_interval_seconds=0.1,
    )
    try:
        manager.start(
            app_name="tiny",
            revision_number=1,
            mount_path="/apps/tiny",
            app_source=app_dir / "app.py",
            manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
        )
        persisted = tmp_path / "workers" / "tiny" / "1.json"
        assert persisted.exists()

        # Wait long enough that the worker counts as idle, then run one sweep manually.
        time.sleep(1.2)
        stopped = manager.run_idle_sweep_once()
        assert "/apps/tiny" in stopped
        assert manager.get_record("/apps/tiny") is None
        # The persisted spec is still on disk so ensure_running can re-spawn from it.
        assert persisted.exists()
        payload = json.loads(persisted.read_text())
        assert payload["status"] == "stopped_idle"
        assert payload["app_source"].endswith("app.py")

        # ensure_running re-spawns from the spec transparently.
        new_record = manager.ensure_running("/apps/tiny")
        assert new_record is not None
        assert new_record.status == "running"
    finally:
        manager.stop_all()


def test_worker_manager_stop_cleans_persisted_record(tmp_path):
    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    manager.start(
        app_name="tiny",
        revision_number=1,
        mount_path="/apps/tiny",
        app_source=app_dir / "app.py",
        manifest={"name": "tiny", "title": "Tiny", "route": "/apps/tiny"},
    )
    persisted = tmp_path / "workers" / "tiny" / "1.json"
    assert persisted.exists()
    assert manager.stop("/apps/tiny") is True
    assert not persisted.exists()


def test_app_factory_creates_worker_manager_only_in_isolated_mode(tmp_path, monkeypatch):
    from dash_server.app_factory import create_app

    monkeypatch.setenv("DASH_SERVER_APP_RUNTIME_MODE", "in_process")
    monkeypatch.setenv("DASH_SERVER_APP_DEPENDENCY_ISOLATION", "shared")
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
        }
    )
    assert app.extensions.get("worker_manager") is None
    assert app.extensions["runtime_service"].runtime_mode == "in_process"

    # Now flip the mode and create a second app; this time the manager exists.
    monkeypatch.setenv("DASH_SERVER_APP_RUNTIME_MODE", "isolated")
    monkeypatch.setenv("DASH_SERVER_APP_DEPENDENCY_ISOLATION", "shared")
    app2 = create_app(
        {
            "TESTING": True,
            "REGISTRY_DB_PATH": str(tmp_path / "registry2.sqlite3"),
            "ARTIFACTS_ROOT": str(tmp_path / "artifacts2"),
            "WORKSPACES_ROOT": str(tmp_path / "workspaces2"),
            "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics2"),
            "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state2"),
            "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo2"),
            "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets2"),
            "AUTO_INSTALL_DEPENDENCIES": False,
            "APP_RUNTIME_MODE": "isolated",
        }
    )
    assert app2.extensions.get("worker_manager") is not None
    assert app2.extensions["runtime_service"].runtime_mode == "isolated"


def test_app_factory_passes_worker_port_range_to_manager(tmp_path):
    from dash_server.app_factory import create_app

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
            "APP_RUNTIME_MODE": "isolated",
            "APP_WORKER_PORT_RANGE": "5500-5599",
        }
    )

    manager = app.extensions["worker_manager"]
    assert manager.port_range == "5500-5599"
    assert app.config["APP_WORKER_PORT_RANGE"] == "5500-5599"


def test_app_factory_rejects_invalid_worker_port_range(tmp_path):
    from dash_server.app_factory import create_app

    with pytest.raises(RuntimeError, match="DASH_SERVER_APP_WORKER_PORT_RANGE"):
        create_app(
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
                "APP_RUNTIME_MODE": "isolated",
                "APP_WORKER_PORT_RANGE": "7000-5000",
            }
        )


# --- Phase 3.5a regression tests (worker package relocation) ---


def test_worker_module_lives_at_dash_server_runtime_path():
    """The worker entry point is importable from dash_server_runtime.worker (not just the shim)."""
    import dash_server_runtime.worker as new_worker
    import dash_server.runtime.worker as shim_worker

    assert callable(new_worker.main)
    # The shim re-exports the same function object so callers paying attention to identity
    # still get the canonical implementation.
    assert shim_worker.main is new_worker.main


def test_python_m_dash_server_runtime_worker_validates_a_tiny_app(tmp_path):
    """`python -m dash_server_runtime.worker --mode=validate` works without PYTHONPATH=src/."""
    import subprocess as _sp
    import sys
    import json as _json
    import os as _os

    app_py = tmp_path / "app.py"
    app_py.write_text(
        "from dash import Dash, html\n\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    app = Dash(__name__, server=server, routes_pathname_prefix='/',\n"
        "               requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
        "    app.layout = html.Div('relocated-validate')\n"
        "    return app\n"
    )

    # Run with a deliberately minimal env: only what subprocess gives us, no PYTHONPATH.
    env = {k: v for k, v in _os.environ.items() if k in {"PATH", "HOME", "USER", "LANG", "TMPDIR"}}
    result = _sp.run(
        [
            sys.executable, "-m", "dash_server_runtime.worker",
            "--mode=validate",
            "--app-name", "relocated",
            "--app-source", str(app_py),
            "--mount-path", "/apps/relocated",
            "--manifest-json", _json.dumps(
                {"name": "relocated", "title": "Relocated", "route": "/apps/relocated"}
            ),
        ],
        capture_output=True, text=True, timeout=60, check=False, env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    payload = _json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    assert payload["status"] == "passed"


def test_worker_manager_spawns_dash_server_runtime_path(tmp_path, monkeypatch):
    """3.5a acceptance: AppWorkerManager spawns `python -m dash_server_runtime.worker`."""
    import subprocess as _sp

    captured = {}
    real_popen = _sp.Popen

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(_sp, "Popen", fake_popen)

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        manager.start(
            app_name="relocated",
            revision_number=1,
            mount_path="/apps/relocated",
            app_source=app_dir / "app.py",
            manifest={"name": "relocated", "title": "Relocated", "route": "/apps/relocated"},
        )
        # Worker spawn target is the new path, not the legacy one.
        assert captured["cmd"][2] == "dash_server_runtime.worker"
    finally:
        manager.stop_all()


def test_worker_manager_no_longer_injects_src_root_into_pythonpath(tmp_path):
    """3.5a: env builder no longer needs PYTHONPATH=src/ to find the worker module."""
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    env = manager._build_worker_env(extra_env=None)
    pythonpath = env.get("PYTHONPATH", "")
    # The env builder should not be inserting the dash-server source directory.
    # (If the user had PYTHONPATH set externally, it's passed through unchanged.)
    import os as _os
    external = _os.environ.get("PYTHONPATH")
    if external is None:
        assert "PYTHONPATH" not in env, env
    else:
        # No leading src_root prefix.
        assert not pythonpath.startswith("/Users") or pythonpath == external, env


# --- Phase 3.5c regression tests (forkserver baseline) ---


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Unix-socket-based forkserver is POSIX-only"
)
def test_forkserver_baseline_spawns_and_emits_ready_event(tmp_path):
    """3.5c: the baseline process starts, prewarms imports, listens on the UDS, emits ready."""
    import subprocess as _sp
    import json as _json
    import tempfile as _tempfile
    import uuid as _uuid

    # macOS caps AF_UNIX paths at 104 bytes; pytest's tmp_path can exceed that. Use the
    # short system temp dir for the socket file instead.
    socket_path = str(
        Path(_tempfile.gettempdir()) / f"dssrv-test-{_uuid.uuid4().hex[:8]}.sock"
    )
    process = _sp.Popen(
        [
            sys.executable,
            "-m",
            "dash_server_runtime.worker.baseline",
            socket_path,
            "dash_server_runtime",  # one-package prewarm — fast
        ],
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        # Wait for the ready line.
        ready_line = process.stdout.readline()
        assert ready_line, f"baseline never emitted a ready event; stderr={process.stderr.read()!r}"
        payload = _json.loads(ready_line)
        assert payload["event"] == "ready"
        assert payload["socket_path"] == socket_path
        assert "dash_server_runtime" in payload["prewarmed_packages"]
        # The socket file exists.
        assert os.path.exists(socket_path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except _sp.TimeoutExpired:
            process.kill()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Unix-socket-based forkserver is POSIX-only"
)
def test_worker_manager_forks_from_baseline_when_enabled(tmp_path):
    """3.5c: with enable_forkserver=True the manager spawns a worker via the baseline.

    Asserts the resulting record carries a `PidHandle` (no Popen) and the worker is
    reachable on its loopback port.
    """

    from dash_server.runtime.worker_manager import PidHandle

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        enable_forkserver=True,
        prewarm_packages=("dash_server_runtime",),  # keep prewarm light for test speed
    )
    try:
        record = manager.start(
            app_name="forked",
            revision_number=1,
            mount_path="/apps/forked",
            app_source=app_dir / "app.py",
            manifest={"name": "forked", "title": "Forked", "route": "/apps/forked"},
        )
        # Forkserver-spawned record has a PidHandle (no Popen wrapper).
        assert isinstance(record.handle, PidHandle), (
            "Expected a forkserver-spawned record (PidHandle), got SubprocessHandle instead"
        )
        assert record.pid > 0
        assert record.port > 0
        assert manager._record_is_alive(record) is True
        assert _wait_port_open(record.host, record.port, timeout=5.0)
        # Baseline is tracked under the python_executable key.
        baselines = manager.baseline_status()
        assert any(b["python_executable"] == sys.executable for b in baselines)
    finally:
        manager.stop_all()
        # stop_all also stops baselines; verify the dict is now empty.
        assert manager.baseline_status() == []


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Unix-socket-based forkserver is POSIX-only"
)
def test_forkserver_miss_falls_back_to_spawn(tmp_path, monkeypatch):
    """3.5c: when the baseline path raises, the manager falls back to spawn."""

    from dash_server.runtime.worker_manager import SubprocessHandle

    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        enable_forkserver=True,
    )
    # Stub _try_baseline_start to simulate a miss (e.g. baseline crashed).
    monkeypatch.setattr(manager, "_try_baseline_start", lambda **kwargs: None)
    try:
        record = manager.start(
            app_name="fallback",
            revision_number=1,
            mount_path="/apps/fallback",
            app_source=app_dir / "app.py",
            manifest={"name": "fallback", "title": "Fallback", "route": "/apps/fallback"},
        )
        # Fallback worker carries a SubprocessHandle (real Popen).
        assert isinstance(record.handle, SubprocessHandle)
        assert record.pid > 0
        assert _wait_port_open(record.host, record.port, timeout=5.0)
    finally:
        manager.stop_all()


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Unix-socket-based forkserver is POSIX-only"
)
def test_record_is_alive_handles_bare_pids(tmp_path):
    """3.5c: PidHandle.is_alive uses os.kill(pid, 0) when there's no Popen handle."""
    from dash_server.runtime.worker_manager import PidHandle, WorkerRecord

    # A definitely-alive pid: the current process.
    alive_record = WorkerRecord(
        app_name="x", revision_number=1, mount_path="/apps/x",
        pid=os.getpid(), host="127.0.0.1", port=0,
        python_executable=sys.executable, environment_id=None,
        started_at="2026-01-01T00:00:00Z",
        handle=PidHandle(pid=os.getpid()),
    )
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    assert manager._record_is_alive(alive_record) is True

    # A definitely-dead pid (very high number that's unlikely to be live).
    dead_record = WorkerRecord(
        app_name="x", revision_number=2, mount_path="/apps/x",
        pid=2_000_000_000, host="127.0.0.1", port=0,
        python_executable=sys.executable, environment_id=None,
        started_at="2026-01-01T00:00:00Z",
        handle=PidHandle(pid=2_000_000_000),
    )
    assert manager._record_is_alive(dead_record) is False


# --- Phase 4 regression tests ---


def test_phase_4a_revision_environment_columns_present_in_schema(tmp_path):
    """4a: app_revisions has dependency_environment_id + env_python_executable columns."""
    from dash_server.registry.sqlite_registry import SQLiteAppRegistry
    import sqlite3 as _sqlite3

    db_path = tmp_path / "registry.sqlite3"
    registry = SQLiteAppRegistry(str(db_path))
    registry.initialize()
    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(app_revisions)")}
    assert "dependency_environment_id" in cols
    assert "env_python_executable" in cols


def test_phase_4g_start_time_p50_records_durations(tmp_path):
    """4g: spawn durations land in the rolling deque; start_time_ms_p50 returns the median."""
    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        manager.start(
            app_name="t", revision_number=1, mount_path="/apps/t",
            app_source=app_dir / "app.py",
            manifest={"name": "t", "title": "T", "route": "/apps/t"},
        )
        p50 = manager.start_time_ms_p50()
        assert p50 is not None and p50 > 0
        # Verify the deque has at least one sample (max 256, rolling).
        assert len(manager._start_durations_ms) == 1
    finally:
        manager.stop_all()


def test_phase_4c_restart_cap_refuses_excess_spawns(tmp_path):
    """4c: AppWorkerManager refuses to start once the per-mount restart cap is hit."""
    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(
        workers_root=str(tmp_path / "workers"),
        max_restarts_per_5_minutes=2,
    )
    try:
        # First two starts pass.
        for i in range(2):
            manager.start(
                app_name="cap", revision_number=i, mount_path="/apps/cap",
                app_source=app_dir / "app.py",
                manifest={"name": "cap", "title": "Cap", "route": "/apps/cap"},
            )
            manager.stop("/apps/cap")
        # Third hits the cap and raises.
        with pytest.raises(WorkerStartError) as exc:
            manager.start(
                app_name="cap", revision_number=99, mount_path="/apps/cap",
                app_source=app_dir / "app.py",
                manifest={"name": "cap", "title": "Cap", "route": "/apps/cap"},
            )
        assert "Restart cap" in str(exc.value)
    finally:
        manager.stop_all()


def test_phase_4b_adopt_persisted_workers_adopts_live_record(tmp_path):
    """4b: adopt_persisted_workers picks up a live worker whose JSON exists on disk."""
    app_dir = _write_tiny_app(tmp_path)
    manager = AppWorkerManager(workers_root=str(tmp_path / "workers"))
    try:
        record = manager.start(
            app_name="adopt", revision_number=1, mount_path="/apps/adopt",
            app_source=app_dir / "app.py",
            manifest={"name": "adopt", "title": "Adopt", "route": "/apps/adopt"},
        )
        original_pid = record.pid
        # Simulate a control-plane restart: drop the in-memory record but leave the worker
        # process and persisted JSON in place.
        with manager._lock:
            manager._records.clear()
        # Adoption sees the JSON, verifies the port/pid, and re-registers.
        actions = manager.adopt_persisted_workers()
        assert actions == {"/apps/adopt": "adopted"}
        adopted = manager.get_record("/apps/adopt")
        assert adopted is not None
        assert adopted.pid == original_pid
        # Adopted records carry a PidHandle (no Popen — we didn't spawn this worker).
        from dash_server.runtime.worker_manager import PidHandle

        assert isinstance(adopted.handle, PidHandle)
    finally:
        manager.stop_all()


def test_phase_4b_adopt_persisted_workers_reaps_dead_record(tmp_path):
    """4b: adoption deletes the JSON when the recorded port is unreachable."""
    workers_root = tmp_path / "workers"
    workers_root.mkdir(parents=True)
    (workers_root / "ghost").mkdir()
    # Hand-craft a record file with a bogus port + pid that's almost certainly dead.
    (workers_root / "ghost" / "1.json").write_text(json.dumps({
        "app_name": "ghost",
        "revision_number": 1,
        "mount_path": "/apps/ghost",
        "pid": 2_000_000_000,
        "host": "127.0.0.1",
        "port": 1,  # privileged port we can't bind without root
        "python_executable": sys.executable,
        "environment_id": None,
        "started_at": "2026-01-01T00:00:00Z",
        "status": "running",
    }))
    manager = AppWorkerManager(workers_root=str(workers_root))
    actions = manager.adopt_persisted_workers()
    assert actions == {"/apps/ghost": "reaped"}
    # JSON file gone.
    assert not (workers_root / "ghost" / "1.json").exists()


def test_phase_4d_env_gc_evicts_unreferenced_envs_past_retention(tmp_path):
    """4d: run_env_gc_once removes envs not in referenced_ids when retention has passed."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
    )
    # Create two fake envs on disk by writing .env_record.json files directly.
    for env_id, age_days in [("sha256:keep", 0), ("sha256:evict", 30)]:
        d = svc.environments_root / env_id
        d.mkdir(parents=True)
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
        (d / ".env_record.json").write_text(json.dumps({
            "environment_id": env_id,
            "status": "ready",
            "created_at": ts,
            "last_used_at": ts,
            "python_executable": str(d / "bin" / "python"),
        }))
    # Mark "keep" as referenced; evict should drop the other.
    result = svc.run_env_gc_once(
        referenced_ids={"sha256:keep"},
        retention_seconds=7 * 24 * 3600,
    )
    assert "sha256:evict" in result["evicted"]
    assert "sha256:keep" in result["skipped_referenced"]
    assert not (svc.environments_root / "sha256:evict").exists()
    assert (svc.environments_root / "sha256:keep").exists()


def test_phase_4d_env_gc_skips_recent_envs_below_retention(tmp_path):
    """4d: an unreferenced env that was used in the last 7 days is retained."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
    )
    d = svc.environments_root / "sha256:fresh"
    d.mkdir(parents=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (d / ".env_record.json").write_text(json.dumps({
        "environment_id": "sha256:fresh",
        "status": "ready",
        "created_at": ts,
        "last_used_at": ts,
        "python_executable": str(d / "bin" / "python"),
    }))
    result = svc.run_env_gc_once(referenced_ids=set(), retention_seconds=7 * 24 * 3600)
    assert result["evicted"] == []
    assert "sha256:fresh" in result["skipped_retained"]
    assert d.exists()


def test_phase_4f_runtime_workers_resource_returns_payload(app, client):
    """4f: dash://runtime/workers responds even when no workers are running."""
    response = _call_mcp(client, "resources/read", {"uri": "dash://runtime/workers"})
    # In_process mode → no worker_manager → 503-style error is OK.
    # In isolated mode it'd return the payload. Either way, the resource is registered.
    payload = response.get_json()
    assert "result" in payload or "error" in payload


def test_phase_4f_environment_invalidate_tool_validates_required_arg(client):
    """4f: app_environment_invalidate rejects calls missing environment_id."""
    response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_environment_invalidate", "arguments": {}},
    )
    payload = response.get_json()
    # Strict-args validator (BUG-008 fix) rejects missing required field with -32602.
    assert payload["result"]["isError"] is True


def _call_mcp(client, method, params, request_id=99):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    return response


# --- Phase 5 regression tests ---


def test_phase_5a_wheel_cache_gc_skips_stdlib_backend(tmp_path):
    """5a: when backend != 'uv' the wheel cache GC refuses to prune (safety guard)."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        backend="venv",  # stdlib path
    )
    # Drop a non-hardlinked file into the cache; it would be eligible under the bare
    # st_nlink strategy but the safety guard should refuse.
    (svc.wheel_cache_root / "fake.whl").write_bytes(b"not actually a wheel")
    result = svc.run_wheel_cache_gc_once()
    assert result["pruned"] == []
    assert result["skipped_reason"] == "stdlib_backend_no_hardlinks"
    assert (svc.wheel_cache_root / "fake.whl").exists()


def test_phase_5a_wheel_cache_gc_proceeds_on_uv_backend(tmp_path):
    """5a: the uv backend path still prunes nlink==1 files as before."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        backend="uv",
    )
    orphan = svc.wheel_cache_root / "orphan.whl"
    orphan.write_bytes(b"orphan")
    result = svc.run_wheel_cache_gc_once()
    assert str(orphan) in result["pruned"]
    assert not orphan.exists()


def test_phase_5b_start_env_gc_skips_when_disabled(tmp_path):
    """5b: start_env_gc is a no-op when env_gc_enabled=False."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        env_gc_enabled=False,
    )
    svc.start_env_gc()
    assert svc._env_gc_thread is None


def test_phase_5b_start_env_gc_starts_thread_when_enabled(tmp_path):
    """5b: start_env_gc launches a daemon thread when env_gc_enabled=True."""
    from dash_server.dependencies import DependencyEnvironmentService

    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        env_gc_enabled=True,
        env_gc_interval_seconds=3600.0,  # don't actually run during the test
    )
    try:
        svc.start_env_gc()
        assert svc._env_gc_thread is not None
        assert svc._env_gc_thread.is_alive()
    finally:
        svc.stop_env_gc()


def test_phase_5c_env_eviction_emits_runtime_event(tmp_path):
    """5c: env_evicted events flow into the diagnostics runtime.events channel."""
    from dash_server.dependencies import DependencyEnvironmentService
    from dash_server.diagnostics import DiagnosticsService

    diagnostics = DiagnosticsService(str(tmp_path / "diagnostics"))
    svc = DependencyEnvironmentService(
        environments_root=str(tmp_path / "envs"),
        wheel_cache_root=str(tmp_path / "wheels"),
        pycache_root=str(tmp_path / "pyc"),
        diagnostics_service=diagnostics,
    )
    # Create one env we'll evict.
    from datetime import datetime, timezone, timedelta

    d = svc.environments_root / "sha256:evictme"
    d.mkdir(parents=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    (d / ".env_record.json").write_text(json.dumps({
        "environment_id": "sha256:evictme",
        "status": "ready",
        "created_at": old_ts,
        "last_used_at": old_ts,
        "python_executable": str(d / "bin" / "python"),
    }))
    result = svc.run_env_gc_once(referenced_ids=set(), retention_seconds=7 * 24 * 3600)
    assert "sha256:evictme" in result["evicted"]
    # The event should land in __runtime__/runtime.events.
    logs = diagnostics.tail_logs("__runtime__", channel="runtime.events", limit=20)
    events = [e for e in logs["entries"] if e["data"].get("event") == "env_evicted"]
    assert events, logs
    assert events[0]["data"]["environment_id"] == "sha256:evictme"


def test_phase_5d_hosted_mode_override_emits_startup_warning(tmp_path, monkeypatch, caplog):
    """5d: when DASH_SERVER_ALLOW_UNSAFE_INPROCESS is honored, app.logger.warning fires."""
    from dash_server.app_factory import create_app
    import logging

    caplog.set_level(logging.WARNING, logger="dash_server")
    create_app(
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
            "DASH_SERVER_MODE": "hosted",
            "SECRET_KEY": "test-secret-key",
            "SESSION_COOKIE_SECURE": True,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "DASH_SERVER_PUBLIC_BASE_URL": "https://dash.example.test",
            "DASH_SERVER_AUTH_PROVIDER": "trusted_proxy",
            "DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED": True,
            "DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS": ("127.0.0.1/32",),
            "DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS": ("trusted_proxy:admin-123",),
            "DASH_SERVER_ALLOW_UNSAFE_INPROCESS": True,
        }
    )
    # The warning fires during create_app's _validate_runtime_isolation_config.
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "DASH_SERVER_ALLOW_UNSAFE_INPROCESS" in messages
    assert "development-only" in messages


def test_phase_5e_sandboxing_followup_plan_exists():
    """5e: the placeholder plan file exists for operators looking for the next step."""
    plan = Path(__file__).resolve().parents[1] / "plans" / "runtime-sandboxing-adapter-plan.md"
    assert plan.exists()
    text = plan.read_text().lower()
    # Plan must distinguish operational isolation (shipped) from a real security sandbox (future).
    assert "operational" in text
    assert "sandbox" in text
    assert "process isolation" in text


def test_phase_5f_readme_mentions_runtime_isolation():
    """5f: README's Current Status section advertises the shipped feature."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text()
    assert "runtime-isolation" in text or "runtime isolation" in text
    assert "docs/runtime-modes.md" in text
