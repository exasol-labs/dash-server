from __future__ import annotations

import json
from pathlib import Path

import pytest

from _mcp_helpers import (
    _call_mcp,
    _bundle,
)

@pytest.mark.slow
def test_mcp_lists_files_and_deletes_app_with_exact_confirmation(app, client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "temporary",
                    "Temporary Dashboard",
                    summary="Safe deletion coverage.",
                    revenue="$1",
                )
            },
        },
    )
    assert create_response.status_code == 200

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "temporary",
                "files": [{"path": "notes.txt", "content": "delete me"}],
            },
        },
        request_id=2,
    )
    listed_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_list_files", "arguments": {"name": "temporary"}},
        request_id=3,
    )
    listed = listed_response.get_json()["result"]["structuredContent"]
    assert set(listed["draft"]["files"]) >= {
        "app.py",
        "dash-app.json",
        "notes.txt",
        "requirements.txt",
    }

    rejected_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_delete",
            "arguments": {"name": "temporary", "confirmation": "wrong-name"},
        },
        request_id=4,
    )
    rejected = rejected_response.get_json()["result"]
    assert rejected["isError"] is True
    assert rejected["structuredContent"]["error"]["category"] == "app_delete_confirmation_error"
    assert client.get("/apps/temporary").status_code == 200

    runtime_service = app.extensions["runtime_service"]
    repo_root = Path(app.extensions["git_repo_service"].repo_root)
    workspace_root = Path(runtime_service.workspace_service.workspace_location("temporary")["workspace_path"])
    artifact_root = Path(runtime_service.artifacts_root) / "temporary"

    deleted_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_delete",
            "arguments": {"name": "temporary", "confirmation": "temporary"},
        },
        request_id=5,
    )
    deleted = deleted_response.get_json()["result"]["structuredContent"]
    assert deleted["deleted"] is True
    assert deleted["app"]["name"] == "temporary"
    assert deleted["registry_deleted"] is True
    assert deleted["recovery"]["available_from_git_history"] is True
    assert deleted["git"]["removed_tags"] == ["dash-server/temporary/r000001"]
    assert client.get("/apps/temporary").status_code == 404
    assert app.extensions["registry"].get_app("temporary") is None
    assert not (repo_root / "apps" / "temporary").exists()
    assert not (repo_root / "desired-state" / "live" / "temporary.yaml").exists()
    assert not workspace_root.exists()
    assert not artifact_root.exists()
    history = (repo_root / "history" / "apps" / "temporary.jsonl").read_text()
    assert '"event_type": "app_deleted"' in history

    recreated_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "temporary",
                    "Recreated Dashboard",
                    summary="The deleted name can be reused.",
                    revenue="$2",
                )
            },
        },
        request_id=6,
    )
    assert recreated_response.get_json()["result"]["isError"] is False
    assert b"Recreated Dashboard" in client.get("/apps/temporary").data


@pytest.mark.slow
def test_runtime_can_remap_route_and_expose_route_resources(app, client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "sales",
                    "Sales Dashboard v1",
                    summary="Initial live revision.",
                    revenue="$1.2M",
                )
            },
        },
        request_id=33,
    )

    runtime_service = app.extensions["runtime_service"]
    remapped = runtime_service.update_route("sales", "/apps/sales-team")
    assert remapped["app"]["route"] == "/apps/sales-team"
    assert remapped["app"]["published"] is True

    old_route_response = client.get("/apps/sales")
    assert old_route_response.status_code == 404
    new_route_response = client.get("/apps/sales-team")
    assert new_route_response.status_code == 200
    assert b"Sales Dashboard v1" in new_route_response.data

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "sales"}},
        request_id=34,
    )
    assert validate_response.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    app_resource = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/sales"},
        request_id=35,
    )
    app_payload = json.loads(app_resource.get_json()["result"]["contents"][0]["text"])
    assert app_payload["app"]["route"] == "/apps/sales-team"
    assert app_payload["exposure"]["mount_path"] == "/apps/sales-team"

    routes_resource = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/sales/routes"},
        request_id=36,
    )
    routes_payload = json.loads(routes_resource.get_json()["result"]["contents"][0]["text"])
    assert routes_payload["routes"]["live"]["mount_path"] == "/apps/sales-team"
    assert routes_payload["routes"]["live"]["mounted"] is True

    permissions_resource = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/sales/permissions"},
        request_id=37,
    )
    permissions_payload = json.loads(permissions_resource.get_json()["result"]["contents"][0]["text"])
    assert permissions_payload["permissions"]["filesystem"]["mode"] == "workspace-write"


@pytest.mark.slow
def test_runtime_can_disable_exposure_and_persist_policy_state(tmp_path):
    from dash_server.app_factory import create_app

    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    first_client = first_app.test_client()
    first_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 38,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": _bundle(
                        "ops",
                        "Ops Dashboard v1",
                        summary="Operations view.",
                        revenue="$2.4M",
                    )
                },
            },
        },
    )
    runtime_service = first_app.extensions["runtime_service"]
    runtime_service.update_visibility("ops", "internal")
    runtime_service.update_auth_policy("ops", "required")
    runtime_service.update_permissions(
        "ops",
        {
            "filesystem": {"mode": "workspace-read"},
            "network": {"mode": "deny"},
            "env": {"mode": "allowlist", "keys": ["REGION"]},
        },
    )
    runtime_service.update_route("ops", "/apps/ops-internal")
    runtime_service.set_enabled("ops", False)

    assert first_client.get("/apps/ops-internal").status_code == 404
    disabled_health = runtime_service.run_healthcheck("ops", record=False)
    assert disabled_health["health"]["status"] == "not_published"

    second_app = create_app(config)
    second_client = second_app.test_client()
    app_resource = second_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 39,
            "method": "resources/read",
            "params": {"uri": "dash://apps/ops"},
        },
    )
    app_payload = json.loads(app_resource.get_json()["result"]["contents"][0]["text"])
    assert app_payload["app"]["route"] == "/apps/ops-internal"
    assert app_payload["app"]["enabled"] is False
    assert app_payload["app"]["visibility"] == "internal"
    assert app_payload["app"]["auth_policy"] == "required"
    assert app_payload["app"]["permissions"]["network"]["mode"] == "deny"
    assert second_client.get("/apps/ops-internal").status_code == 404

    second_runtime = second_app.extensions["runtime_service"]
    enabled = second_runtime.set_enabled("ops", True)
    assert enabled["app"]["enabled"] is True
    assert second_client.get("/apps/ops-internal").status_code == 200


@pytest.mark.slow
def test_route_conflicts_are_rejected_before_stage_5(app, client):
    create_sales = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "sales",
                    "Sales Dashboard v1",
                    summary="Initial live revision.",
                    revenue="$1.2M",
                )
            },
        },
        request_id=40,
    )
    assert create_sales.status_code == 200

    create_conflict = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    **_bundle(
                        "deals",
                        "Deals Dashboard v1",
                        summary="Conflicting route.",
                        revenue="$880K",
                    ),
                    "manifest": {
                        **_bundle(
                            "deals",
                            "Deals Dashboard v1",
                            summary="Conflicting route.",
                            revenue="$880K",
                        )["manifest"],
                        "route": "/apps/sales",
                    },
                }
            },
        },
        request_id=41,
    )
    assert create_conflict.status_code == 200
    conflict_result = create_conflict.get_json()["result"]
    assert conflict_result["isError"] is True
    assert conflict_result["structuredContent"]["error"]["category"] == "route_conflict"

    runtime_service = app.extensions["runtime_service"]
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "ops",
                    "Ops Dashboard v1",
                    summary="Operations view.",
                    revenue="$2.4M",
                )
            },
        },
        request_id=42,
    )
    try:
        runtime_service.update_route("ops", "/apps/sales")
    except Exception as exc:
        assert getattr(exc, "category", None) == "route_conflict"
    else:
        raise AssertionError("Expected route conflict when remapping ops to /apps/sales")

