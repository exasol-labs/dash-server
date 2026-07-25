from __future__ import annotations

from textwrap import dedent

import pytest

from _mcp_helpers import (
    _call_mcp,
    _dash_layout,
    _resource_json,
    _layout_texts,
    _app_callback_app_py,
)

def test_mcp_exposes_app_create_schema_help_for_llm_clients(client):
    tools_response = _call_mcp(
        client,
        "tools/list",
        {},
        request_id=1,
    )
    tools = tools_response.get_json()["result"]["tools"]
    app_create = next(tool for tool in tools if tool["name"] == "app_create")
    app_create_from_files = next(tool for tool in tools if tool["name"] == "app_create_from_files")
    app_create_exasol_dashboard = next(
        tool for tool in tools if tool["name"] == "app_create_exasol_dashboard"
    )
    app_scaffold_from_schema = next(
        tool for tool in tools if tool["name"] == "app_scaffold_from_schema"
    )
    exasol_profile_create_local = next(
        tool for tool in tools if tool["name"] == "exasol_profile_create_local"
    )
    app_read_file = next(tool for tool in tools if tool["name"] == "app_read_file")
    app_patch_file = next(tool for tool in tools if tool["name"] == "app_patch_file")
    app_diff_draft_vs_artifact = next(
        tool for tool in tools if tool["name"] == "app_diff_draft_vs_artifact"
    )
    app_build = next(tool for tool in tools if tool["name"] == "app_build")
    app_deploy_draft = next(tool for tool in tools if tool["name"] == "app_deploy_draft")
    bundle_schema = app_create["inputSchema"]["properties"]["bundle"]
    assert bundle_schema["properties"]["manifest"]["required"] == ["name", "title"]
    assert bundle_schema["properties"]["manifest"]["properties"]["template"]["enum"] == [
        "metric-cards",
        "exasol-analytics",
    ]
    assert "Do not include source files in bundle" in bundle_schema["description"]
    assert app_create_from_files["inputSchema"]["required"] == ["name", "files"]
    assert app_create_exasol_dashboard["inputSchema"]["required"] == ["name", "profile_name"]
    assert app_create_exasol_dashboard["inputSchema"]["properties"]["pattern"]["default"] == "analytics-hub"
    assert "analytics-hub" in app_create_exasol_dashboard["inputSchema"]["properties"]["pattern"]["enum"]
    assert app_scaffold_from_schema["inputSchema"]["required"] == ["name", "profile_name"]
    assert exasol_profile_create_local["inputSchema"]["required"] == [
        "name",
        "backend",
        "credential_mode",
        "dsn",
        "user",
    ]
    assert app_read_file["inputSchema"]["required"] == ["name", "path"]
    assert "compact line-context preview" in app_patch_file["description"]
    assert app_diff_draft_vs_artifact["inputSchema"]["required"] == ["name"]
    assert app_build["inputSchema"]["properties"]["force_clean"]["default"] is False
    assert app_deploy_draft["inputSchema"]["properties"]["deployment_target"]["enum"] == ["live", "preview"]
    assert app_deploy_draft["inputSchema"]["properties"]["force_clean"]["default"] is False
    app_tail_logs = next(tool for tool in tools if tool["name"] == "app_tail_logs")
    # Phase 3.5d / Phase 4f added the worker channels and the session channel added
    # `session.commands`; assert the full list rather than the legacy four so a future
    # channel rename or addition is loud, not silent.
    assert app_tail_logs["inputSchema"]["properties"]["channel"]["enum"] == [
        "latest",
        "build",
        "runtime",
        "health",
        "worker",
        "worker.events",
        "session.commands",
    ]
    app_run_healthcheck = next(tool for tool in tools if tool["name"] == "app_run_healthcheck")
    assert app_run_healthcheck["inputSchema"]["properties"]["target"]["enum"] == ["live", "preview"]

    schema_resource = _resource_json(
        client,
        "dash://meta/app-create-schema",
        request_id=2,
    )
    assert schema_resource["tool"] == "app_create"
    assert schema_resource["help_resource"] == "dash://meta/app-create-schema"
    assert schema_resource["example"]["manifest"]["route"] == "/apps/support"
    assert "app_create_from_files" in schema_resource["related_tools"]

    files_schema_resource = _resource_json(
        client,
        "dash://meta/app-create-from-files-schema",
        request_id=3,
    )
    assert files_schema_resource["tool"] == "app_create_from_files"
    assert files_schema_resource["example"]["files"][0]["path"] == "app.py"

    authoring_guide = _resource_json(
        client,
        "dash://meta/app-authoring-guide",
        request_id=4,
    )
    assert authoring_guide["factory_signature"] == "create_dash_app(server, url_base_pathname, metadata)"
    assert "Do not use global dash.callback for hosted apps." in authoring_guide["required_rules"]

    workflows = _resource_json(
        client,
        "dash://meta/workflows",
        request_id=5,
    )
    assert any(workflow["name"] == "create_from_files" for workflow in workflows["workflows"])

    repo_status = _resource_json(
        client,
        "dash://repo/status",
        request_id=6,
    )
    assert repo_status["repo"]["initialized"] is True
    assert repo_status["repo"]["current_branch"] == "main"
    assert repo_status["repo"]["phase"] == "phase4a"
    assert "demo" in repo_status["repo"]["tracked_apps"]
    assert "dash-server/demo/r000001" in repo_status["repo"]["release_tags"]
    assert "demo" in repo_status["repo"]["history_apps"]
    assert any(worktree["branch"] == "draft/demo" for worktree in repo_status["repo"]["worktrees"])
    assert repo_status["repo"]["desired_preview_apps"] == []

    desired_state = _resource_json(
        client,
        "dash://repo/desired-state",
        request_id=7,
    )
    assert desired_state["live"]["demo"]["spec"]["targetRevision"] == "r000001"
    assert desired_state["live"]["demo"]["spec"]["route"] == "/apps/demo"

    drift = _resource_json(
        client,
        "dash://repo/drift",
        request_id=8,
    )
    demo_drift = next(entry for entry in drift["drift"] if entry["app"] == "demo")
    assert demo_drift["live"]["status"] == "in_sync"

    apps_resource = _resource_json(
        client,
        "dash://apps",
        request_id=9,
    )
    assert apps_resource["apps"][0]["browser_url"].endswith("/apps/demo")


@pytest.mark.slow
def test_app_create_accepts_shorthand_root_bundle_shape(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    "name": "shorthand-support",
                    "title": "Shorthand Support",
                    "route": "/apps/shorthand-support",
                    "description": "Created from shorthand root-level fields.",
                    "template": "metric-cards",
                    "headline": "Shorthand Support",
                    "summary": "Created by a client that omitted bundle.manifest.",
                    "metrics": [
                        {"label": "Revenue", "value": "$750K"},
                        {"label": "Conversion", "value": "5.1%"},
                    ],
                }
            },
        },
        request_id=3,
    )
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["app"]["name"] == "shorthand-support"
    assert created["app"]["route"] == "/apps/shorthand-support"
    assert created["app"]["browser_url"].endswith("/apps/shorthand-support")
    assert "app_put_files" in created["guidance"]["suggested_tools"]
    assert created["current_revision"]["git_tag"] == "dash-server/shorthand-support/r000001"
    assert len(created["current_revision"]["commit_sha"]) == 40
    assert b"Shorthand Support" in client.get("/apps/shorthand-support").data


@pytest.mark.slow
def test_app_create_accepts_minimal_name_only_bundle(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    "name": "markets-dashboard",
                }
            },
        },
        request_id=4,
    )
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["app"]["name"] == "markets-dashboard"
    assert created["app"]["title"] == "Markets Dashboard"
    assert created["app"]["route"] == "/apps/markets-dashboard"
    assert created["app"]["browser_url"].endswith("/apps/markets-dashboard")
    assert created["current_revision"]["git_tag"] == "dash-server/markets-dashboard/r000001"
    assert b"Markets Dashboard" in client.get("/apps/markets-dashboard").data


@pytest.mark.slow
def test_app_create_accepts_top_level_name_shorthand(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "name": "pricing-dashboard",
                "start_immediately": True,
            },
        },
        request_id=5,
    )
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["app"]["name"] == "pricing-dashboard"
    assert created["app"]["title"] == "Pricing Dashboard"
    assert created["app"]["route"] == "/apps/pricing-dashboard"
    assert created["current_revision"]["git_tag"] == "dash-server/pricing-dashboard/r000001"
    assert b"Pricing Dashboard" in client.get("/apps/pricing-dashboard").data


def test_invalid_app_create_returns_structured_schema_guidance(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    "manifest": "not-an-object",
                    "dashboard": {"headline": "Broken"},
                }
            },
        },
        request_id=6,
    )
    assert create_response.status_code == 200
    result = create_response.get_json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "manifest_validation_error"
    assert error["details"]["help_resource"] == "dash://meta/app-create-schema"
    assert error["details"]["tool"] == "app_create"
    assert "common_mistakes" in error["details"]
    assert error["details"]["example"]["manifest"]["name"] == "support"


@pytest.mark.slow
def test_app_create_from_files_bootstraps_workspace_and_returns_guidance(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "markets-dashboard",
                "files": [
                    {
                        "path": "app.py",
                        "content": dedent(
                            """
                            from dash import Dash, html


                            def create_dash_app(server, url_base_pathname, metadata):
                                prefix = url_base_pathname.rstrip("/") + "/"
                                app = Dash(
                                    __name__,
                                    server=server,
                                    routes_pathname_prefix="/",
                                    requests_pathname_prefix=prefix,
                                    title=metadata.get("title", "Markets Dashboard"),
                                )
                                app.layout = html.Div(
                                    [
                                        html.H1(metadata.get("title", "Markets Dashboard")),
                                        html.P("Created from a files-based app_create bundle."),
                                    ]
                                )
                                return app
                            """
                        ).strip()
                        + "\n",
                    },
                ],
                "start_immediately": True,
            },
        },
        request_id=7,
    )
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["app"]["name"] == "markets-dashboard"
    assert created["app"]["route"] == "/apps/markets-dashboard"
    assert created["app"]["browser_url"].endswith("/apps/markets-dashboard")
    assert "app_validate" in created["guidance"]["suggested_tools"]
    assert created["current_revision"]["git_tag"] == "dash-server/markets-dashboard/r000001"
    assert b"Markets Dashboard" in client.get("/apps/markets-dashboard").data
    layout_texts = _layout_texts(_dash_layout(client, "/apps/markets-dashboard"))
    assert "Created from a files-based app_create bundle." in layout_texts


@pytest.mark.slow
def test_app_create_rejects_files_and_points_to_app_create_from_files(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    "name": "ops-console",
                    "files": [{"path": "app.py", "content": "print('x')\n"}],
                }
            },
        },
        request_id=8,
    )
    assert create_response.status_code == 200
    result = create_response.get_json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "tool_validation_error"
    assert result["structuredContent"]["guidance"]["related_resources"][0] == "dash://meta/app-create-from-files-schema"


@pytest.mark.slow
def test_app_create_from_files_requires_non_empty_files_array(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "ops-console",
                "files": [],
            },
        },
        request_id=9,
    )
    assert create_response.status_code == 200
    result = create_response.get_json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "bundle_validation_error"
    assert error["details"]["field"] == "bundle.files"
    assert error["details"]["help_resource"] == "dash://meta/app-create-from-files-schema"


@pytest.mark.slow
def test_registry_can_hold_more_than_three_apps(client):
    for request_id, name in enumerate(
        ["alpha-dashboard", "beta-dashboard", "gamma-dashboard", "delta-dashboard"],
        start=9,
    ):
        response = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_create",
                "arguments": {
                    "bundle": {"name": name},
                },
            },
            request_id=request_id,
        )
        assert response.status_code == 200
        assert response.get_json()["result"]["structuredContent"]["app"]["name"] == name

    apps_list_response = _call_mcp(
        client,
        "tools/call",
        {"name": "apps_list", "arguments": {}},
        request_id=13,
    )
    apps_list_payload = apps_list_response.get_json()["result"]
    assert "delta-dashboard" in apps_list_payload["content"][0]["text"]
    listed_names = [app["name"] for app in apps_list_payload["structuredContent"]["apps"]]
    assert "delta-dashboard" in listed_names


@pytest.mark.slow
def test_app_patch_file_returns_line_context_preview_and_guidance(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "patch-preview"}},
        },
        request_id=16,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "patch-preview",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Patch Preview App")}],
            },
        },
        request_id=17,
    )

    patch_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_patch_file",
            "arguments": {
                "name": "patch-preview",
                "path": "app.py",
                "search": 'html.H1("App Callback Revision"),',
                "replace": 'html.H1("Patched Callback Revision"),',
            },
        },
        request_id=18,
    )
    assert patch_response.status_code == 200
    payload = patch_response.get_json()["result"]["structuredContent"]
    operation = payload["operation"]

    assert operation["matched_line_numbers"] == [15]
    assert operation["before_context"][-1] == {"line_number": 14, "text": "        ["}
    assert operation["after_context"][0] == {
        "line_number": 16,
        "text": '            dcc.Interval(id="tick", interval=1000, n_intervals=0),',
    }
    assert operation["preview"] == [
        {
            "match_index": 1,
            "start_line": 15,
            "end_line": 15,
            "before_context": [
                {"line_number": 12, "text": "    )"},
                {"line_number": 13, "text": "    app.layout = html.Div("},
                {"line_number": 14, "text": "        ["},
            ],
            "after_context": [
                {
                    "line_number": 16,
                    "text": '            dcc.Interval(id="tick", interval=1000, n_intervals=0),',
                },
                {"line_number": 17, "text": '            html.Div(id="clock"),'},
                {"line_number": 18, "text": "        ]"},
            ],
            "lines": [
                {"line_number": 12, "text": "    )", "kind": "context"},
                {"line_number": 13, "text": "    app.layout = html.Div(", "kind": "context"},
                {"line_number": 14, "text": "        [", "kind": "context"},
                {
                    "line_number": 15,
                    "text": '            html.H1("Patched Callback Revision"),',
                    "kind": "replacement",
                },
                {
                    "line_number": 16,
                    "text": '            dcc.Interval(id="tick", interval=1000, n_intervals=0),',
                    "kind": "context",
                },
                {"line_number": 17, "text": '            html.Div(id="clock"),', "kind": "context"},
                {"line_number": 18, "text": "        ]", "kind": "context"},
            ],
        }
    ]
    assert payload["guidance"]["next_step"] == "Review the patch preview, then validate the updated draft workspace."
    assert payload["guidance"]["suggested_tools"] == ["app_validate", "app_patch_file", "app_put_files"]

