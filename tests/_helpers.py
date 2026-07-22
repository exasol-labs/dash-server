"""Shared test helpers.

One copy of the MCP JSON-RPC plumbing, config-dict bases, and polling loop
that used to be duplicated per test file. New tests use these; old call
sites migrate opportunistically.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable


def base_test_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """The standard local-mode create_app config every test used to hand-build."""

    config: dict[str, Any] = {
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
    config.update(overrides)
    return config


def hosted_test_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """`base_test_config` plus the keys hosted mode requires (trusted-proxy auth)."""

    config = base_test_config(
        tmp_path,
        DASH_SERVER_MODE="hosted",
        SECRET_KEY="test-secret-key",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        DASH_SERVER_PUBLIC_BASE_URL="https://dash.example.test",
        DASH_SERVER_AUTH_PROVIDER="trusted_proxy",
        DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED=True,
        DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS=("127.0.0.1/32",),
        DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS=("trusted_proxy:admin-1",),
        DASH_SERVER_ALLOW_UNSAFE_INPROCESS=True,
    )
    config.update(overrides)
    return config


def call_mcp(
    client,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    request_id: int = 1,
    headers: dict[str, str] | None = None,
):
    """POST one tools/call JSON-RPC request to /mcp."""

    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )


def read_resource_json(client, uri: str, *, request_id: int = 2, headers: dict[str, str] | None = None):
    """Read one MCP resource and decode its JSON text contents."""

    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return json.loads(response.get_json()["result"]["contents"][0]["text"])


def wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float = 5.0,
    interval: float = 0.02,
    message: str = "condition",
):
    """Poll ``predicate`` until it returns a truthy value; return that value."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {message}")


__all__ = [
    "base_test_config",
    "call_mcp",
    "hosted_test_config",
    "read_resource_json",
    "wait_for",
]
