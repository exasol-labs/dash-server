from __future__ import annotations

import json

import pytest

from _mcp_helpers import (
    _call_mcp,
    _dash_layout,
    _resource_json,
    _layout_ids,
    _bundle,
)

def test_mcp_get_exposes_streamable_http_endpoint(client):
    response = client.get("/mcp")

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert b"dash-server MCP endpoint ready" in response.data


def test_mcp_file_resources_expose_seeded_demo_workspace(client):
    files_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/demo/files"},
        request_id=5,
    )
    assert files_response.status_code == 200
    files_payload = json.loads(files_response.get_json()["result"]["contents"][0]["text"])
    assert files_payload["draft"]["candidate_version"] == 1
    assert set(files_payload["draft"]["files"]) == {
        "app.py",
        "dash-app.json",
        "requirements.txt",
    }

    file_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/demo/files/app.py"},
        request_id=6,
    )
    file_payload = json.loads(file_response.get_json()["result"]["contents"][0]["text"])
    assert "create_dash_app" in file_payload["content"]


@pytest.mark.slow
def test_app_read_file_returns_current_draft_contents(client):
    read_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "demo", "path": "app.py"}},
        request_id=7,
    )
    assert read_response.status_code == 200
    payload = read_response.get_json()["result"]["structuredContent"]
    assert payload["path"] == "app.py"
    assert "create_dash_app" in payload["content"]
    assert payload["draft"]["candidate_version"] == 1


@pytest.mark.slow
def test_mcp_apps_inventory_includes_created_app(client):
    initial_inventory = _call_mcp(
        client,
        "tools/call",
        {"name": "apps_list", "arguments": {}},
        request_id=3,
    )
    initial_apps = initial_inventory.get_json()["result"]["structuredContent"]["apps"]
    assert any(app["name"] == "demo" for app in initial_apps)

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "deals",
                    "Deals Dashboard v1",
                    summary="Deals dashboard created from MCP inventory test.",
                    revenue="$980K",
                )
            },
        },
        request_id=4,
    )
    assert create_response.status_code == 200

    apps_list_response = _call_mcp(
        client,
        "tools/call",
        {"name": "apps_list", "arguments": {}},
        request_id=5,
    )
    visible_text = apps_list_response.get_json()["result"]["content"][0]["text"]
    assert "Result:" in visible_text
    assert '"name": "deals"' in visible_text
    listed_apps = apps_list_response.get_json()["result"]["structuredContent"]["apps"]
    deals = next(app for app in listed_apps if app["name"] == "deals")
    assert deals["route"] == "/apps/deals"
    assert deals["mounted"] is True
    assert deals["current_revision_number"] == 1
    assert deals["draft_candidate_version"] == 1

    resource_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps"},
        request_id=6,
    )
    resource_apps = json.loads(resource_response.get_json()["result"]["contents"][0]["text"])["apps"]
    assert any(app["name"] == "demo" for app in resource_apps)
    assert any(app["name"] == "deals" and app["mounted"] is True for app in resource_apps)


def test_mcp_resources_list_includes_repo_status(client):
    resources_response = _call_mcp(
        client,
        "resources/list",
        {},
        request_id=13,
    )
    assert resources_response.status_code == 200
    resources = resources_response.get_json()["result"]["resources"]
    repo_status = next(resource for resource in resources if resource["uri"] == "dash://repo/status")
    assert repo_status["title"] == "GitOps repository status"
    artifact_files = next(
        resource
        for resource in resources
        if resource["uri"] == "dash://apps/demo/artifacts/latest/files"
    )
    latest_build_diff = next(
        resource
        for resource in resources
        if resource["uri"] == "dash://apps/demo/diff/latest-build...draft"
    )
    assert artifact_files["title"] == "demo latest artifact files"
    assert latest_build_diff["title"] == "demo latest-build-to-draft diff"


def test_runtime_status_reports_configured_port_settings(client):
    runtime_status = _resource_json(
        client,
        "dash://runtime/status",
        request_id=131,
    )

    assert runtime_status["control_plane_host"] == "127.0.0.1"
    assert runtime_status["control_plane_port"] == 5100
    assert runtime_status["worker_host"] == "127.0.0.1"
    assert runtime_status["worker_port_range"] is None

    # PS27 round-2 recommendation #1: an agent must be able to tell whether the running
    # process predates the source tree it's reading docs/schemas from.
    build = runtime_status["build"]
    assert "process_started_at" in build
    assert build["source"] in {"git", "unknown"}
    if build["source"] == "git":
        assert build["commit_sha"]
        assert build["commit_timestamp"]


@pytest.mark.slow
def test_repo_status_reports_dirty_worktrees_after_draft_edit(client):
    patch_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_patch_file",
            "arguments": {
                "name": "demo",
                "path": "app.py",
                "search": 'SUMMARY = "Stage 4 proves MCP-driven editing, diagnostics, and revisioned Dash hosting."',
                "replace": 'SUMMARY = "Stage 4 proves MCP-driven editing, diagnostics, revisioned Dash hosting, and Git-backed draft worktrees."',
            },
        },
        request_id=14,
    )
    assert patch_response.status_code == 200

    repo_status = _resource_json(
        client,
        "dash://repo/status",
        request_id=15,
    )
    assert "draft/demo" in repo_status["repo"]["dirty_worktrees"]


@pytest.mark.slow
def test_live_apps_expose_revision_status_for_auto_refresh(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "refresh-app",
                    "Refresh Dashboard v1",
                    summary="Initial refresh revision.",
                    revenue="$820K",
                )
            },
        },
        request_id=21,
    )

    initial_status = client.get("/apps/refresh-app/__dash-server/status")
    assert initial_status.status_code == 200
    assert initial_status.get_json()["revision_number"] == 1

    layout_ids = _layout_ids(_dash_layout(client, "/apps/refresh-app"))
    assert "__dash-server-refresh-interval" in layout_ids
    assert "__dash-server-refresh-meta" in layout_ids
    assert "__dash-server-refresh-noop" in layout_ids

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_patch_file",
            "arguments": {
                "name": "refresh-app",
                "path": "app.py",
                "search": "Initial refresh revision.",
                "replace": "Updated refresh revision.",
            },
        },
        request_id=22,
    )
    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "refresh-app"}},
        request_id=23,
    )
    revision_number = build_response.get_json()["result"]["structuredContent"]["revision"]["revision_number"]
    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "refresh-app", "revision_number": revision_number}},
        request_id=24,
    )
    assert promote_response.status_code == 200

    updated_status = client.get("/apps/refresh-app/__dash-server/status")
    assert updated_status.status_code == 200
    assert updated_status.get_json()["revision_number"] == 2

