from __future__ import annotations

import json
from pathlib import Path

import pytest

from dash_server.app_factory import create_app

from _helpers import base_test_config
from _mcp_helpers import (
    _call_mcp,
    _dash_layout,
    _resource_json,
    _dash_callback,
    _layout_texts,
    _bundle,
    _multipage_assets_app_py,
    _app_callback_app_py,
    _artifact_sensitive_app_py,
)

@pytest.mark.slow
def test_repo_reconcile_applies_direct_git_desired_state_change(client, app):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "deals",
                    "Deals Dashboard v1",
                    summary="Initial live revision.",
                    revenue="$1.2M",
                )
            },
        },
        request_id=20,
    )
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["current_revision"]["revision_number"] == 1

    build_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_build",
            "arguments": {
                "name": "deals",
                "bundle": _bundle(
                    "deals",
                    "Deals Dashboard v2",
                    summary="Promoted through direct Git desired state.",
                    revenue="$1.8M",
                ),
            },
        },
        request_id=21,
    )
    assert build_response.status_code == 200
    built = build_response.get_json()["result"]["structuredContent"]["revision"]
    assert built["revision_number"] == 2

    repo_root = Path(app.extensions["git_repo_service"].repo_root)
    live_path = repo_root / "desired-state" / "live" / "deals.yaml"
    desired_live = live_path.read_text()
    desired_live = desired_live.replace("targetRevision: r000001", "targetRevision: r000002")
    desired_live = desired_live.replace(created["current_revision"]["commit_sha"], built["commit_sha"])
    desired_live = desired_live.replace(created["current_revision"]["git_tag"], built["git_tag"])
    desired_live = desired_live.replace(
        created["current_revision"]["release_manifest_path"],
        built["release_manifest_path"],
    )
    live_path.write_text(desired_live)

    reconcile_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "repo_reconcile",
            "arguments": {},
        },
        request_id=22,
    )
    assert reconcile_response.status_code == 200
    reconciled = reconcile_response.get_json()["result"]["structuredContent"]
    assert any(result["app"] == "deals" and result["live_revision"] == 2 for result in reconciled["results"])

    status_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_get_status",
            "arguments": {"name": "deals"},
        },
        request_id=23,
    )
    status = status_response.get_json()["result"]["structuredContent"]
    assert status["current_revision"]["revision_number"] == 2
    layout_texts = _layout_texts(_dash_layout(client, "/apps/deals"))
    assert "Deals Dashboard v2" in layout_texts


@pytest.mark.slow
def test_ps26_bug019_a_cleared_preview_revision_settles_to_archived(client):
    """PS26-BUG-019 regression: promoting a *different* revision than the one
    currently in preview clears the old preview pointer, but used to never update
    that revision's own `lifecycle_state` - it stayed at `"warming"` (set when it
    *entered* preview) forever instead of settling to `"archived"` like every other
    non-current, non-preview revision.
    """

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "stale-preview",
                    "Stale Preview v1",
                    summary="r1 live.",
                    revenue="$1M",
                )
            },
        },
        request_id=700,
    )
    build_2 = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_build",
            "arguments": {
                "name": "stale-preview",
                "bundle": _bundle(
                    "stale-preview", "Stale Preview v2", summary="r2, will only ever be previewed.", revenue="$1.1M"
                ),
            },
        },
        request_id=701,
    )
    revision_2 = build_2.get_json()["result"]["structuredContent"]["revision"]["revision_number"]
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "stale-preview", "revision_number": revision_2}},
        request_id=702,
    )

    build_3 = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_build",
            "arguments": {
                "name": "stale-preview",
                "bundle": _bundle(
                    "stale-preview", "Stale Preview v3", summary="r3, promoted straight to live.", revenue="$1.2M"
                ),
            },
        },
        request_id=703,
    )
    revision_3 = build_3.get_json()["result"]["structuredContent"]["revision"]["revision_number"]
    # Promoting r3 supersedes r2's preview without ever promoting r2 itself - this is
    # the exact transition that used to leave r2 stuck at "warming".
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "stale-preview", "revision_number": revision_3}},
        request_id=704,
    )

    revisions = _resource_json(client, "dash://apps/stale-preview/revisions", request_id=705)["revisions"]
    revision_2_state = next(r["lifecycle_state"] for r in revisions if r["revision_number"] == revision_2)
    assert revision_2_state == "archived"


@pytest.mark.slow
def test_app_deploy_draft_runs_validate_build_and_promote_in_one_step(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "one-shot"}},
        },
        request_id=14,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "one-shot",
                "files": [{"path": "app.py", "content": _app_callback_app_py("One Shot App")}],
            },
        },
        request_id=15,
    )

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "one-shot"}},
        request_id=16,
    )
    assert deploy_response.status_code == 200
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is False
    payload = deploy_result["structuredContent"]
    assert payload["validation"]["validation"]["is_valid"] is True
    assert payload["build"]["revision"]["revision_number"] == 2
    assert payload["build"]["preflight"]["status"] == "passed"
    assert payload["deployment"]["current_revision"]["revision_number"] == 2
    assert "app_run_healthcheck" in payload["guidance"]["suggested_tools"]
    assert payload["app"]["browser_url"].endswith("/apps/one-shot")
    assert client.get("/apps/one-shot").status_code == 200
    one_shot_texts = _layout_texts(_dash_layout(client, "/apps/one-shot"))
    assert "App Callback Revision" in one_shot_texts


@pytest.mark.slow
def test_app_build_force_clean_bypasses_cached_dependency_state(app, client, monkeypatch):
    installer = app.extensions["dependency_installer"]
    installer.enabled = False
    calls: list[list[str]] = []

    def fake_run(command: list[str]):
        calls.append(command)
        return {
            "status": "succeeded",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(installer, "_run_install_command", fake_run)

    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "force-clean-build"}}},
        request_id=17,
    )
    installer.enabled = True

    first_build = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "force-clean-build"}},
        request_id=18,
    )
    second_build = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "force-clean-build"}},
        request_id=19,
    )
    forced_build = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "force-clean-build", "force_clean": True}},
        request_id=20,
    )

    assert first_build.status_code == 200
    assert second_build.status_code == 200
    assert forced_build.status_code == 200
    first_payload = first_build.get_json()["result"]["structuredContent"]
    second_payload = second_build.get_json()["result"]["structuredContent"]
    forced_payload = forced_build.get_json()["result"]["structuredContent"]
    assert first_payload["validation"]["dependency_install"]["status"] == "succeeded"
    assert second_payload["validation"]["dependency_install"]["status"] == "cached"
    assert forced_payload["validation"]["dependency_install"]["status"] == "succeeded"
    assert forced_payload["validation"]["dependency_install"]["force_clean"] is True
    assert forced_payload["force_clean"] is True
    assert len(calls) == 2


@pytest.mark.slow
def test_app_deploy_draft_force_clean_reinstalls_dependencies_once_per_deploy(
    app, client, monkeypatch
):
    installer = app.extensions["dependency_installer"]
    installer.enabled = False
    calls: list[list[str]] = []

    def fake_run(command: list[str]):
        calls.append(command)
        return {
            "status": "succeeded",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(installer, "_run_install_command", fake_run)

    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "force-clean-deploy"}}},
        request_id=21,
    )
    installer.enabled = True

    first_deploy = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "force-clean-deploy"}},
        request_id=22,
    )
    forced_deploy = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {
                "name": "force-clean-deploy",
                "deployment_target": "preview",
                "force_clean": True,
            },
        },
        request_id=23,
    )

    assert first_deploy.status_code == 200
    assert forced_deploy.status_code == 200
    first_payload = first_deploy.get_json()["result"]["structuredContent"]
    forced_payload = forced_deploy.get_json()["result"]["structuredContent"]
    assert first_payload["validation"]["validation"]["dependency_install"]["status"] == "succeeded"
    assert forced_payload["force_clean"] is True
    assert forced_payload["validation"]["validation"]["dependency_install"]["status"] == "succeeded"
    assert forced_payload["validation"]["validation"]["dependency_install"]["force_clean"] is True
    assert forced_payload["build"]["force_clean"] is False
    assert len(calls) == 2


@pytest.mark.slow
def test_app_deploy_draft_can_mount_preview_revision(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "preview-only",
                    "Preview Only v1",
                    summary="Initial live revision.",
                    revenue="$900K",
                )
            },
        },
        request_id=160,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "preview-only",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Preview Only v2")}],
            },
        },
        request_id=161,
    )

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "preview-only", "deployment_target": "preview"},
        },
        request_id=162,
    )
    assert deploy_response.status_code == 200
    result = deploy_response.get_json()["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["deployment_target"] == "preview"
    assert payload["build"]["preflight"]["status"] == "passed"
    assert payload["deployment"]["preview_revision"]["revision_number"] == 2
    assert payload["health"]["target"] == "preview"
    assert payload["app"]["preview_path"] == "/preview/preview-only/2"
    assert payload["app"]["preview_url"].endswith("/preview/preview-only/2")
    assert b"Preview Only v1" in client.get("/apps/preview-only").data
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/preview-only/2"))
    assert "App Callback Revision" in preview_layout_texts


@pytest.mark.slow
def test_preview_health_uses_preview_mount_when_live_app_is_stopped(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "preview-stopped-live",
                    "Preview Stopped Live v1",
                    summary="Initial stopped revision.",
                    revenue="$500K",
                ),
                "start_immediately": False,
            },
        },
        request_id=163,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "preview-stopped-live",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Preview Stopped Live v2")}],
            },
        },
        request_id=164,
    )

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "preview-stopped-live", "deployment_target": "preview"},
        },
        request_id=165,
    )
    assert deploy_response.status_code == 200
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is False
    deploy_payload = deploy_result["structuredContent"]
    assert deploy_payload["app"]["status"] == "stopped"
    assert deploy_payload["app"]["preview_mounted"] is True
    assert deploy_payload["health"]["health"]["status"] == "healthy"

    health_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_run_healthcheck",
            "arguments": {"name": "preview-stopped-live", "target": "preview"},
        },
        request_id=166,
    )
    assert health_response.status_code == 200
    health_payload = health_response.get_json()["result"]["structuredContent"]
    assert health_payload["target"] == "preview"
    assert health_payload["app"]["status"] == "stopped"
    assert health_payload["health"]["status"] == "healthy"
    probes = {probe["name"]: probe for probe in health_payload["health"]["probes"]}
    assert probes["process_alive"]["status"] == "passed"
    assert probes["process_alive"]["details"]["mounted"] is True
    assert probes["http_ready"]["status"] == "passed"
    assert probes["dash_layout"]["status"] == "passed"
    assert probes["dash_dependencies"]["status"] == "passed"
    assert "Preview revision is not mounted." not in str(health_payload["health"]["probes"])
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/preview-stopped-live/2"))
    assert "App Callback Revision" in preview_layout_texts


@pytest.mark.slow
def test_promote_and_deploy_guidance_can_suggest_start_for_stopped_apps(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "guided-app"}, "start_immediately": False}},
        request_id=16,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "guided-app",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Guided App")}],
            },
        },
        request_id=17,
    )
    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "guided-app"}},
        request_id=18,
    )
    revision_number = build_response.get_json()["result"]["structuredContent"]["revision"]["revision_number"]
    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "guided-app", "revision_number": revision_number}},
        request_id=19,
    )
    promoted = promote_response.get_json()["result"]["structuredContent"]
    assert promoted["app"]["mounted"] is False
    assert "app_start" in promoted["guidance"]["suggested_tools"]
    # PS26-BUG-007: "app_start" living only in the structured `suggested_tools` array
    # is easy to miss - the human-readable summary text itself must say so plainly.
    visible_text = promote_response.get_json()["result"]["content"][0]["text"]
    assert "call app_start" in visible_text

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "guided-app"}},
        request_id=20,
    )
    deployed = deploy_response.get_json()["result"]["structuredContent"]
    assert "app_start" in deployed["guidance"]["suggested_tools"]


@pytest.mark.slow
def test_app_deploy_draft_can_auto_rollback_on_failed_live_healthcheck(app, client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "rollback-health",
                    "Rollback Health v1",
                    summary="Initial live revision.",
                    revenue="$1.1M",
                )
            },
        },
        request_id=170,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "rollback-health",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Rollback Health v2")}],
            },
        },
        request_id=171,
    )

    runtime_service = app.extensions["runtime_service"]
    original_run_healthcheck = runtime_service.run_healthcheck

    def failing_live_healthcheck(name: str, *, target: str = "live", record: bool = True):
        payload = original_run_healthcheck(name, target=target, record=record)
        if target == "live" and payload["revision"]["revision_number"] == 2:
            payload["health"]["status"] = "unhealthy"
            for probe in payload["health"]["probes"]:
                if probe["name"] == "http_ready":
                    probe["status"] = "failed"
                    break
        return payload

    runtime_service.run_healthcheck = failing_live_healthcheck
    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {
                "name": "rollback-health",
                "auto_rollback_on_health_failure": True,
            },
        },
        request_id=172,
    )

    assert deploy_response.status_code == 200
    result = deploy_response.get_json()["result"]
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["error"]["category"] == "deployment_healthcheck_failed"
    assert payload["deployment"]["current_revision"]["revision_number"] == 2
    assert payload["rollback"]["current_revision"]["revision_number"] == 1
    assert payload["rollback_health"]["revision"]["revision_number"] == 1
    assert b"Rollback Health v1" in client.get("/apps/rollback-health").data


@pytest.mark.slow
def test_app_build_surfaces_failed_artifact_preflight(app, client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "preflight-build",
                    "Preflight Build v1",
                    summary="Initial live revision.",
                    revenue="$810K",
                )
            },
        },
        request_id=173,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "preflight-build",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Preflight Build v2")}],
            },
        },
        request_id=174,
    )
    runtime_service = app.extensions["runtime_service"]
    original_preflight = runtime_service.preflight_revision

    def failing_preflight(name: str, revision_number: int):
        payload = original_preflight(name, revision_number)
        payload["preflight"]["status"] = "failed"
        payload["preflight"]["error"] = None
        payload["preflight"]["captured_errors"] = []
        updated = False
        for probe in payload["preflight"]["probes"]:
            if probe.get("name") == "static_assets":
                probe["status"] = "failed"
                probe["details"] = {"message": "Synthetic preflight failure."}
                updated = True
                break
        if not updated:
            payload["preflight"]["probes"].append(
                {
                    "name": "static_assets",
                    "status": "failed",
                    "details": {"message": "Synthetic preflight failure."},
                }
            )
        return payload

    runtime_service.preflight_revision = failing_preflight

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "preflight-build"}},
        request_id=175,
    )
    assert build_response.status_code == 200
    build_result = build_response.get_json()["result"]
    assert build_result["isError"] is True
    payload = build_result["structuredContent"]
    assert payload["error"]["category"] == "artifact_preflight_failed"
    assert payload["revision"]["revision_number"] == 2
    assert payload["preflight"]["status"] == "failed"
    failed_probe = next(
        probe for probe in payload["preflight"]["probes"] if probe.get("name") == "static_assets"
    )
    assert failed_probe["status"] == "failed"
    assert "Synthetic preflight failure." in build_result["content"][0]["text"]
    assert b"Preflight Build v1" in client.get("/apps/preflight-build").data

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "preflight-build"}},
        request_id=176,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_build_result"]["status"] == "failed"
    assert diagnostics["latest_build_result"]["preflight"]["status"] == "failed"
    assert diagnostics["latest_build_error"]["category"] == "runtime_crash"


@pytest.mark.slow
def test_app_deploy_draft_blocks_live_promotion_when_preflight_fails(app, client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "preflight-live",
                    "Preflight Live v1",
                    summary="Initial live revision.",
                    revenue="$920K",
                )
            },
        },
        request_id=177,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "preflight-live",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Preflight Live v2")}],
            },
        },
        request_id=178,
    )
    runtime_service = app.extensions["runtime_service"]
    original_preflight = runtime_service.preflight_revision

    def failing_preflight(name: str, revision_number: int):
        payload = original_preflight(name, revision_number)
        payload["preflight"]["status"] = "failed"
        payload["preflight"]["error"] = None
        payload["preflight"]["captured_errors"] = []
        updated = False
        for probe in payload["preflight"]["probes"]:
            if probe.get("name") == "static_assets":
                probe["status"] = "failed"
                probe["details"] = {"message": "Synthetic preflight failure."}
                updated = True
                break
        if not updated:
            payload["preflight"]["probes"].append(
                {
                    "name": "static_assets",
                    "status": "failed",
                    "details": {"message": "Synthetic preflight failure."},
                }
            )
        return payload

    runtime_service.preflight_revision = failing_preflight

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "preflight-live"}},
        request_id=179,
    )
    assert deploy_response.status_code == 200
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is True
    payload = deploy_result["structuredContent"]
    assert payload["error"]["category"] == "artifact_preflight_failed"
    assert payload["build"]["revision"]["revision_number"] == 2
    assert payload["build"]["preflight"]["status"] == "failed"
    assert "app_collect_diagnostics" in payload["guidance"]["suggested_tools"]
    assert b"Preflight Live v1" in client.get("/apps/preflight-live").data


@pytest.mark.slow
def test_app_deploy_draft_preview_can_report_failed_preflight_without_blocking_mount(app, client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "preflight-preview",
                    "Preflight Preview v1",
                    summary="Initial live revision.",
                    revenue="$760K",
                )
            },
        },
        request_id=180,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "preflight-preview",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Preflight Preview v2")}],
            },
        },
        request_id=181,
    )
    runtime_service = app.extensions["runtime_service"]
    original_preflight = runtime_service.preflight_revision

    def failing_preflight(name: str, revision_number: int):
        payload = original_preflight(name, revision_number)
        payload["preflight"]["status"] = "failed"
        payload["preflight"]["error"] = None
        payload["preflight"]["captured_errors"] = []
        updated = False
        for probe in payload["preflight"]["probes"]:
            if probe.get("name") == "static_assets":
                probe["status"] = "failed"
                probe["details"] = {"message": "Synthetic preflight failure."}
                updated = True
                break
        if not updated:
            payload["preflight"]["probes"].append(
                {
                    "name": "static_assets",
                    "status": "failed",
                    "details": {"message": "Synthetic preflight failure."},
                }
            )
        return payload

    runtime_service.preflight_revision = failing_preflight

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "preflight-preview", "deployment_target": "preview"},
        },
        request_id=182,
    )
    assert deploy_response.status_code == 200
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is False
    payload = deploy_result["structuredContent"]
    assert payload["build"]["preflight"]["status"] == "failed"
    assert payload["deployment"]["preview_revision"]["revision_number"] == 2
    assert payload["health"]["target"] == "preview"
    assert payload["health"]["health"]["status"] == "healthy"
    assert payload["app"]["preview_path"] == "/preview/preflight-preview/2"


@pytest.mark.slow
class TestMcpWorkspaceEditToRollback:
    """Decomposition of the former single ``test_mcp_can_edit_validate_build_
    preview_promote_and_rollback_from_workspace`` walkthrough.

    The full create -> edit -> validate -> diff -> build -> preview -> promote
    -> rollback -> delete sequence runs once in the class-scoped ``flow``
    fixture (the end-to-end smoke path); each focused test asserts one phase's
    behaviour against the captured payloads so a failure localises to a phase.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def flow(tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("mcp_edit_rollback")
        app = create_app(base_test_config(tmp_path))
        client = app.test_client()
        captured: dict = {}

        create_response = _call_mcp(
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
            request_id=3,
        )
        captured["create_status"] = create_response.status_code
        captured["created"] = create_response.get_json()["result"]["structuredContent"]
        captured["live_body_after_create"] = client.get("/apps/sales").data
        captured["live_texts_after_create"] = _layout_texts(_dash_layout(client, "/apps/sales"))

        put_response = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_put_files",
                "arguments": {
                    "name": "sales",
                    "files": [{"path": "notes.txt", "content": "Stage 4 draft note\n"}],
                },
            },
            request_id=4,
        )
        captured["put_status"] = put_response.status_code
        captured["put"] = put_response.get_json()["result"]["structuredContent"]

        patch_manifest = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_patch_file",
                "arguments": {
                    "name": "sales",
                    "path": "dash-app.json",
                    "search": '"title": "Sales Dashboard v1"',
                    "replace": '"title": "Sales Dashboard v2"',
                },
            },
            request_id=5,
        )
        captured["patch_manifest_status"] = patch_manifest.status_code
        captured["patch_manifest"] = patch_manifest.get_json()["result"]["structuredContent"]

        patch_app = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_patch_file",
                "arguments": {
                    "name": "sales",
                    "path": "app.py",
                    "search": "Initial live revision.",
                    "replace": "Updated revision through MCP.",
                },
            },
            request_id=6,
        )
        captured["patch_app_status"] = patch_app.status_code
        captured["patch_app"] = patch_app.get_json()["result"]["structuredContent"]

        validate_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_validate", "arguments": {"name": "sales"}},
            request_id=7,
        )
        captured["validate"] = validate_response.get_json()["result"]["structuredContent"]

        diff_response = _call_mcp(
            client,
            "resources/read",
            {"uri": "dash://apps/sales/diff/current...draft"},
            request_id=8,
        )
        captured["diff"] = json.loads(diff_response.get_json()["result"]["contents"][0]["text"])

        artifact_diff_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_diff_draft_vs_artifact", "arguments": {"name": "sales"}},
            request_id=9,
        )
        captured["artifact_diff"] = artifact_diff_response.get_json()["result"]["structuredContent"]

        captured["latest_build_diff_pre"] = _resource_json(
            client,
            "dash://apps/sales/diff/latest-build...draft",
            request_id=10,
        )

        build_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_build", "arguments": {"name": "sales"}},
            request_id=11,
        )
        captured["built"] = build_response.get_json()["result"]["structuredContent"]

        captured["latest_artifact_files"] = _resource_json(
            client,
            "dash://apps/sales/artifacts/latest/files",
            request_id=12,
        )
        captured["post_build_diff"] = _resource_json(
            client,
            "dash://apps/sales/diff/latest-build...draft",
            request_id=13,
        )

        preview_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_start_preview", "arguments": {"name": "sales", "revision_number": 2}},
            request_id=14,
        )
        captured["preview_status"] = preview_response.status_code
        captured["preview"] = preview_response.get_json()["result"]["structuredContent"]
        captured["live_body_during_preview"] = client.get("/apps/sales").data
        captured["preview_body"] = client.get("/preview/sales/2").data
        captured["live_texts_during_preview"] = _layout_texts(_dash_layout(client, "/apps/sales"))
        captured["preview_texts"] = _layout_texts(_dash_layout(client, "/preview/sales/2"))

        promote_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_promote_revision", "arguments": {"name": "sales", "revision_number": 2}},
            request_id=15,
        )
        captured["promoted"] = promote_response.get_json()["result"]["structuredContent"]
        captured["live_body_after_promote"] = client.get("/apps/sales").data
        captured["promoted_texts"] = _layout_texts(_dash_layout(client, "/apps/sales"))

        rollback_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_rollback", "arguments": {"name": "sales"}},
            request_id=16,
        )
        captured["rolled_back"] = rollback_response.get_json()["result"]["structuredContent"]
        captured["live_body_after_rollback"] = client.get("/apps/sales").data
        captured["rolled_back_texts"] = _layout_texts(_dash_layout(client, "/apps/sales"))

        delete_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_delete_file", "arguments": {"name": "sales", "path": "notes.txt"}},
            request_id=17,
        )
        captured["deleted"] = delete_response.get_json()["result"]["structuredContent"]

        return captured

    def test_create_serves_initial_live_revision(self, flow):
        assert flow["create_status"] == 200
        assert flow["created"]["draft"]["candidate_version"] == 1
        assert b"Sales Dashboard v1" in flow["live_body_after_create"]
        texts = flow["live_texts_after_create"]
        assert "Sales Dashboard v1" in texts
        assert "Initial live revision." in texts
        assert "$1.2M" in texts
        assert "Delivered by " in texts
        assert "Exasol" in texts

    def test_draft_edits_increment_candidate_version(self, flow):
        assert flow["put_status"] == 200
        assert flow["put"]["draft"]["candidate_version"] == 2
        assert flow["patch_manifest_status"] == 200
        assert flow["patch_manifest"]["draft"]["candidate_version"] == 3
        assert flow["patch_app_status"] == 200
        assert flow["patch_app"]["draft"]["candidate_version"] == 4

    def test_validate_passes_for_edited_draft(self, flow):
        validation = flow["validate"]["validation"]
        assert validation["is_valid"] is True
        assert validation["imports"]["status"] == "passed"
        assert validation["dependency_install"]["requirements"] == ["dash>=4.0,<5.0"]

    def test_current_vs_draft_diff_shows_edits(self, flow):
        assert "Sales Dashboard v2" in flow["diff"]["diff"]
        assert "notes.txt" in flow["diff"]["diff"]

    def test_draft_vs_artifact_diff_reports_file_statuses(self, flow):
        artifact_diff = flow["artifact_diff"]
        assert artifact_diff["target"] == "latest_build"
        assert artifact_diff["artifact"]["revision"]["revision_number"] == 1
        assert artifact_diff["draft"]["source_hash"] != artifact_diff["artifact"]["source_hash"]
        file_statuses = {entry["path"]: entry["status"] for entry in artifact_diff["files"]}
        assert file_statuses["app.py"] == "changed"
        assert file_statuses["dash-app.json"] == "changed"
        assert file_statuses["notes.txt"] == "draft_only"

    def test_ps26_bug015_diff_tool_guidance_points_at_the_resource_with_real_diff_content(self, flow):
        """PS26-BUG-015 regression: the tool only ever returns changed/unchanged status
        plus byte counts, never actual diff content - its guidance must point at the
        `dash://apps/{app}/diff/...` resource that has the real unified diff, since an
        agent working tool-call-only has no other way to discover that URI exists.
        """

        artifact_diff = flow["artifact_diff"]
        assert not any("diff" in entry for entry in artifact_diff["files"])
        related_resources = artifact_diff["guidance"]["related_resources"]
        assert any(uri.startswith("dash://apps/{app}/diff/") for uri in related_resources)

    def test_latest_build_diff_before_build_targets_revision_one(self, flow):
        latest_build_diff = flow["latest_build_diff_pre"]
        assert latest_build_diff["artifact"]["revision"]["revision_number"] == 1
        assert "Sales Dashboard v2" in latest_build_diff["diff"]
        assert "notes.txt" in latest_build_diff["diff"]

    def test_build_produces_tagged_artifact_revision_two(self, flow):
        built = flow["built"]
        assert built["revision"]["revision_number"] == 2
        assert built["preflight"]["status"] == "passed"
        assert built["revision"]["git_tag"] == "dash-server/sales/r000002"
        assert built["revision"]["git_branch"] == "draft/sales"
        assert built["revision"]["release_manifest_path"] == "releases/sales/r000002.yaml"
        assert len(built["revision"]["commit_sha"]) == 40
        artifact_path = Path(built["revision"]["artifact_path"])
        assert artifact_path.is_dir()
        assert (artifact_path / "app.py").exists()
        assert (artifact_path / "dash-app.json").exists()
        assert artifact_path.name == "r000002"

    def test_latest_artifact_files_lists_built_revision(self, flow):
        latest_artifact_files = flow["latest_artifact_files"]
        assert latest_artifact_files["artifact"]["revision"]["revision_number"] == 2
        assert set(latest_artifact_files["artifact"]["files"]) >= {
            "app.py",
            "dash-app.json",
            "notes.txt",
            "requirements.txt",
        }

    def test_post_build_diff_is_empty(self, flow):
        post_build_diff = flow["post_build_diff"]
        assert post_build_diff["artifact"]["revision"]["revision_number"] == 2
        assert post_build_diff["diff"] == ""
        assert all(entry["status"] == "unchanged" for entry in post_build_diff["files"])

    def test_preview_serves_revision_two_while_live_stays_one(self, flow):
        assert flow["preview_status"] == 200
        assert flow["preview"]["preview_revision"]["revision_number"] == 2
        assert b"Sales Dashboard v1" in flow["live_body_during_preview"]
        assert b"Sales Dashboard v2" in flow["preview_body"]
        assert "Initial live revision." in flow["live_texts_during_preview"]
        assert "Updated revision through MCP." not in flow["live_texts_during_preview"]
        assert "Updated revision through MCP." in flow["preview_texts"]
        assert "$1.2M" in flow["preview_texts"]

    def test_promote_makes_revision_two_live(self, flow):
        promoted = flow["promoted"]
        assert promoted["current_revision"]["revision_number"] == 2
        assert promoted["rollback_revision"]["revision_number"] == 1
        assert b"Sales Dashboard v2" in flow["live_body_after_promote"]
        assert "Updated revision through MCP." in flow["promoted_texts"]

    def test_rollback_restores_revision_one(self, flow):
        rolled_back = flow["rolled_back"]
        assert rolled_back["current_revision"]["revision_number"] == 1
        assert b"Sales Dashboard v1" in flow["live_body_after_rollback"]
        assert "Initial live revision." in flow["rolled_back_texts"]
        assert "Updated revision through MCP." not in flow["rolled_back_texts"]

    def test_delete_file_removes_from_draft(self, flow):
        deleted = flow["deleted"]
        assert deleted["draft"]["candidate_version"] == 5
        assert "notes.txt" not in deleted["draft"]["files"]


@pytest.mark.slow
def test_mcp_can_deploy_multipage_and_asset_backed_workspace_app(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "catalog",
                    "Catalog Dashboard v1",
                    summary="Initial catalog revision.",
                    revenue="$710K",
                )
            },
        },
        request_id=43,
    )
    assert create_response.status_code == 200

    put_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "catalog",
                "files": [
                    {"path": "app.py", "content": _multipage_assets_app_py("Catalog Workspace App")},
                    {
                        "path": "assets/theme.css",
                        "content": ".inventory-shell { background: rgb(248, 246, 236); color: rgb(23, 23, 23); }\n.nav-links { font-weight: 700; }\n",
                    },
                ],
            },
        },
        request_id=44,
    )
    assert put_response.status_code == 200

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "catalog"}},
        request_id=45,
    )
    assert validate_response.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "catalog"}},
        request_id=46,
    )
    built = build_response.get_json()["result"]["structuredContent"]
    assert built["revision"]["revision_number"] == 2
    artifact_path = Path(built["revision"]["artifact_path"])
    assert (artifact_path / "assets" / "theme.css").exists()

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "catalog", "revision_number": 2}},
        request_id=47,
    )
    assert preview_response.status_code == 200
    assert client.get("/preview/catalog/2").status_code == 200
    assert client.get("/preview/catalog/2/details").status_code == 200
    preview_callback = _dash_callback(
        client,
        "/preview/catalog/2",
        output="page-content.children",
        outputs={"id": "page-content", "property": "children"},
        inputs=[
            {
                "id": "page-url",
                "property": "pathname",
                "value": "/preview/catalog/2/details",
            }
        ],
        changed_prop_ids=["page-url.pathname"],
    )
    assert preview_callback.status_code == 200
    assert "Inventory Detail Page" in preview_callback.get_data(as_text=True)

    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "catalog", "revision_number": 2}},
        request_id=48,
    )
    assert promote_response.status_code == 200
    assert client.get("/apps/catalog").status_code == 200
    assert client.get("/apps/catalog/details").status_code == 200

    live_callback = _dash_callback(
        client,
        "/apps/catalog",
        output="page-content.children",
        outputs={"id": "page-content", "property": "children"},
        inputs=[
            {
                "id": "page-url",
                "property": "pathname",
                "value": "/apps/catalog/details",
            }
        ],
        changed_prop_ids=["page-url.pathname"],
    )
    assert live_callback.status_code == 200
    assert "Inventory Detail Page" in live_callback.get_data(as_text=True)

    homepage = client.get("/apps/catalog")
    assert b"/apps/catalog/assets/theme.css" in homepage.data
    asset_response = client.get("/apps/catalog/assets/theme.css")
    assert asset_response.status_code == 200
    assert b"inventory-shell" in asset_response.data

    health_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_run_healthcheck", "arguments": {"name": "catalog"}},
        request_id=49,
    )
    probes = health_response.get_json()["result"]["structuredContent"]["health"]["probes"]
    assert next(probe for probe in probes if probe["name"] == "static_assets")["status"] == "passed"


@pytest.mark.slow
def test_mcp_surfaces_preview_runtime_mount_failure(client):
    create_preview_app = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "shipping",
                    "Shipping Dashboard v1",
                    summary="Initial shipping revision.",
                    revenue="$2.9M",
                )
            },
        },
        request_id=62,
    )
    assert create_preview_app.status_code == 200

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "shipping",
                "files": [
                    {
                        "path": "app.py",
                        "content": _artifact_sensitive_app_py(
                            "Shipping Preview Sensitive",
                            failure_mode="preview",
                        ),
                    }
                ],
            },
        },
        request_id=63,
    )

    build_preview = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "shipping"}},
        request_id=64,
    )
    assert build_preview.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "shipping", "revision_number": 2}},
        request_id=65,
    )
    assert preview_response.status_code == 200
    preview_result = preview_response.get_json()["result"]
    assert preview_result["isError"] is True
    assert preview_result["structuredContent"]["error"]["category"] == "runtime_mount_error"
    assert client.get("/apps/shipping").status_code == 200

    shipping_errors = _resource_json(
        client,
        "dash://apps/shipping/errors",
        request_id=66,
    )
    assert shipping_errors["errors"][-1]["source"] == "runtime"
    assert shipping_errors["errors"][-1]["category"] == "runtime_crash"


@pytest.mark.slow
def test_mcp_surfaces_startup_runtime_mount_failure(client):
    create_standby = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "standby",
                    "Standby Dashboard v1",
                    summary="Initial standby revision.",
                    revenue="$1.1M",
                ),
                "start_immediately": False,
            },
        },
        request_id=67,
    )
    assert create_standby.status_code == 200

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "standby",
                "files": [
                    {
                        "path": "app.py",
                        "content": _artifact_sensitive_app_py(
                            "Standby Live Sensitive",
                            failure_mode="live",
                        ),
                    }
                ],
            },
        },
        request_id=68,
    )

    build_standby = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "standby"}},
        request_id=69,
    )
    assert build_standby.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    promote_standby = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "standby", "revision_number": 2}},
        request_id=70,
    )
    assert promote_standby.status_code == 200
    assert client.get("/apps/standby").status_code == 404

    start_standby = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start", "arguments": {"name": "standby"}},
        request_id=71,
    )
    assert start_standby.status_code == 200
    start_result = start_standby.get_json()["result"]
    assert start_result["isError"] is True
    assert start_result["structuredContent"]["error"]["category"] == "runtime_mount_error"
    assert client.get("/apps/standby").status_code == 404

    health_result = _call_mcp(
        client,
        "tools/call",
        {"name": "app_run_healthcheck", "arguments": {"name": "standby"}},
        request_id=72,
    ).get_json()["result"]["structuredContent"]["health"]
    assert health_result["status"] == "unhealthy"
    http_probe = next(probe for probe in health_result["probes"] if probe["name"] == "http_ready")
    assert http_probe["status"] == "failed"
    assert http_probe["details"]["status_code"] == 404

    standby_errors = _resource_json(
        client,
        "dash://apps/standby/errors",
        request_id=73,
    )
    assert standby_errors["errors"][-1]["source"] == "runtime"
    assert standby_errors["errors"][-1]["category"] == "runtime_crash"

