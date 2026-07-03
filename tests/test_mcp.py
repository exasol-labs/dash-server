from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any


def _call_mcp(client, method, params, request_id=1):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
    )


def _dash_layout(client, mount_path: str) -> dict[str, Any]:
    response = client.get(f"{mount_path}/_dash-layout")
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def _resource_json(client, uri: str, *, request_id: int) -> dict[str, Any]:
    response = _call_mcp(
        client,
        "resources/read",
        {"uri": uri},
        request_id=request_id,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    return json.loads(payload["result"]["contents"][0]["text"])


def _dash_callback(
    client,
    mount_path: str,
    *,
    output: str,
    outputs: dict[str, str],
    inputs: list[dict[str, Any]],
    changed_prop_ids: list[str],
    state: list[dict[str, Any]] | None = None,
):
    return client.post(
        f"{mount_path}/_dash-update-component",
        json={
            "output": output,
            "outputs": outputs,
            "inputs": inputs,
            "changedPropIds": changed_prop_ids,
            "state": state or [],
        },
    )


def _layout_texts(node: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(node, str):
        texts.append(node)
    elif isinstance(node, list):
        for item in node:
            texts.extend(_layout_texts(item))
    elif isinstance(node, dict):
        for value in node.values():
            texts.extend(_layout_texts(value))
    return texts


def _layout_ids(node: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(node, list):
        for item in node:
            ids.update(_layout_ids(item))
    elif isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict):
            component_id = props.get("id")
            if isinstance(component_id, str):
                ids.add(component_id)
        for value in node.values():
            ids.update(_layout_ids(value))
    return ids


def _bundle(name: str, title: str, *, summary: str, revenue: str) -> dict[str, object]:
    return {
        "manifest": {
            "name": name,
            "title": title,
            "route": f"/apps/{name}",
            "description": f"{title} created through the Stage 4 MCP control plane.",
            "template": "metric-cards",
        },
        "dashboard": {
            "headline": title,
            "summary": summary,
            "metrics": [
                {"label": "Revenue", "value": revenue},
                {"label": "Conversion", "value": "4.8%"},
            ],
        },
    }


def _multipage_assets_app_py(title: str) -> str:
    return dedent(
        f"""
        from pathlib import Path

        from dash import Dash, Input, Output, dcc, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                assets_folder=str(Path(__file__).with_name("assets")),
                title=metadata.get("title", {title!r}),
            )

            app.layout = html.Div(
                [
                    dcc.Location(id="page-url"),
                    html.H1("Inventory Console"),
                    html.Div(
                        [
                            dcc.Link("Overview", href=prefix),
                            html.Span(" | "),
                            dcc.Link("Details", href=f"{{prefix}}details"),
                        ],
                        className="nav-links",
                    ),
                    html.Div(id="page-content"),
                ],
                className="inventory-shell",
            )

            @app.callback(Output("page-content", "children"), Input("page-url", "pathname"))
            def render_page(pathname):
                if (pathname or "").endswith("/details"):
                    return html.Div(
                        [
                            html.H2("Inventory Detail Page"),
                            html.P("Warehouse split for the selected SKU."),
                        ]
                    )
                return html.Div(
                    [
                        html.H2("Inventory Overview Page"),
                        html.P("Stock on hand by channel."),
                    ]
                )

            return app
        """
    ).strip() + "\n"


def _callback_failure_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, Input, Output, dcc, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )

            app.layout = html.Div(
                [
                    html.H1("Alert Console"),
                    dcc.Dropdown(
                        id="mode",
                        options=[
                            {{"label": "Safe", "value": "safe"}},
                            {{"label": "Explode", "value": "explode"}},
                        ],
                        value="safe",
                        clearable=False,
                    ),
                    html.Div(id="callback-result"),
                ]
            )

            @app.callback(Output("callback-result", "children"), Input("mode", "value"))
            def run_mode(mode):
                if mode == "explode":
                    raise RuntimeError("callback exploded during diagnostics test")
                return f"Mode: {{mode}}"

            return app
        """
    ).strip() + "\n"


def _global_callback_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, Input, Output, callback, dcc, html


        @callback(Output("clock", "children"), Input("tick", "n_intervals"))
        def render_clock(n_intervals):
            return f"Tick: {{n_intervals or 0}}"


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1("Global Callback Revision"),
                    dcc.Interval(id="tick", interval=1000, n_intervals=0),
                    html.Div(id="clock"),
                ]
            )
            return app
        """
    ).strip() + "\n"


def _app_callback_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, Input, Output, dcc, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1("App Callback Revision"),
                    dcc.Interval(id="tick", interval=1000, n_intervals=0),
                    html.Div(id="clock"),
                ]
            )

            @app.callback(Output("clock", "children"), Input("tick", "n_intervals"))
            def render_clock(n_intervals):
                return f"Tick: {{n_intervals or 0}}"

            return app
        """
    ).strip() + "\n"


def _cross_module_from_import_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, html
        from theme import labeled_bar


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div([labeled_bar("Revenue")])
            return app
        """
    ).strip() + "\n"


def _cross_module_alias_app_py(title: str) -> str:
    return dedent(
        f"""
        import theme as T
        from dash import Dash, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div([T.labeled_bar("Revenue")])
            return app
        """
    ).strip() + "\n"


def _cross_module_wildcard_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, html
        from theme import *


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div([html.H1("Wildcard Theme Import")])
            return app
        """
    ).strip() + "\n"


def _theme_labeled_bar_py() -> str:
    return dedent(
        """
        from dash import html


        def labeled_bar(label):
            return html.Div(f"Labeled {label}", className="labeled-bar")
        """
    ).strip() + "\n"


def _missing_callback_id_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, Input, Output, dcc, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1("Missing Callback Id"),
                    dcc.Interval(id="tick", interval=1000, n_intervals=0),
                    html.Div(id="clock"),
                ]
            )

            @app.callback(Output("missing-output", "children"), Input("tick", "n_intervals"))
            def render_clock(n_intervals):
                return f"Tick: {{n_intervals or 0}}"

            return app
        """
    ).strip() + "\n"


def _plotly_lint_app_py(title: str) -> str:
    return dedent(
        f"""
        import plotly.graph_objects as go
        from dash import Dash, dcc, html


        LAYOUT_KWARGS = {{"margin": {{"l": 24, "r": 24}}}}


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            figure = go.Figure()
            figure.add_trace(
                go.Scatter(
                    x=[1, 2, 3],
                    y=[2, 1, 3],
                    fill="tozeroy",
                    fillcolor="#11223344",
                )
            )
            figure.update_layout(**LAYOUT_KWARGS, margin={{"l": 16}})
            app.layout = html.Div([html.H1("Plotly Lint"), dcc.Graph(figure=figure)])
            return app
        """
    ).strip() + "\n"


def _artifact_sensitive_app_py(title: str, *, failure_mode: str) -> str:
    return dedent(
        f"""
        from pathlib import Path

        from dash import Dash, html

        FAILURE_MODE = {failure_mode!r}


        def create_dash_app(server, url_base_pathname, metadata):
            location = str(Path(__file__).resolve())
            route = metadata.get("route", "")
            if "artifacts" in location:
                if FAILURE_MODE == "preview" and route.startswith("/preview/"):
                    raise RuntimeError("preview artifact mount exploded")
                if FAILURE_MODE == "live" and route.startswith("/apps/"):
                    raise RuntimeError("live artifact mount exploded")

            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div([html.H1({title!r}), html.P("Artifact-sensitive runtime app.")])
            return app
        """
    ).strip() + "\n"


def _artifact_first_load_crash_app_py(title: str) -> str:
    return dedent(
        f"""
        from pathlib import Path

        from dash import Dash, html


        def create_dash_app(server, url_base_pathname, metadata):
            artifact_mode = "artifacts" in str(Path(__file__).resolve())
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div([html.H1({title!r}), html.P("Workspace validation remains clean.")])
            if artifact_mode:
                app.index_string = '''
                <!DOCTYPE html>
                <html>
                    <head>
                        {{%metas%}}
                        <script src="__BROKEN_PREFIX___dash-component-suites/does-not-exist.js"></script>
                        <title>{{%title%}}</title>
                        {{%favicon%}}
                        {{%css%}}
                    </head>
                    <body>
                        {{%app_entry%}}
                        <footer>
                            {{%config%}}
                            {{%scripts%}}
                            {{%renderer%}}
                        </footer>
                    </body>
                </html>
                '''.replace("__BROKEN_PREFIX__", prefix)
            return app
        """
    ).strip() + "\n"


def _misconfigured_prefix_app_py(title: str) -> str:
    return dedent(
        f"""
        from dash import Dash, html


        def create_dash_app(server, url_base_pathname, metadata):
            prefix = url_base_pathname.rstrip("/") + "/"
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix=prefix,
                requests_pathname_prefix=prefix,
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1({title!r}),
                    html.P("This app is intentionally mounted with the wrong internal Dash prefixes."),
                ]
            )
            return app
        """
    ).strip() + "\n"


def test_mcp_get_exposes_streamable_http_endpoint(client):
    response = client.get("/mcp")

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert b"dash-server MCP endpoint ready" in response.data


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
    # Phase 3.5d / Phase 4f added the worker channels; assert all six rather than the legacy
    # four so a future channel rename or addition is loud, not silent.
    assert app_tail_logs["inputSchema"]["properties"]["channel"]["enum"] == [
        "latest",
        "build",
        "runtime",
        "health",
        "worker",
        "worker.events",
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

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "guided-app"}},
        request_id=20,
    )
    deployed = deploy_response.get_json()["result"]["structuredContent"]
    assert "app_start" in deployed["guidance"]["suggested_tools"]


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


def test_mcp_can_edit_validate_build_preview_promote_and_rollback_from_workspace(client):
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
    assert create_response.status_code == 200
    created = create_response.get_json()["result"]["structuredContent"]
    assert created["draft"]["candidate_version"] == 1
    assert b"Sales Dashboard v1" in client.get("/apps/sales").data
    live_layout_texts = _layout_texts(_dash_layout(client, "/apps/sales"))
    assert "Sales Dashboard v1" in live_layout_texts
    assert "Initial live revision." in live_layout_texts
    assert "$1.2M" in live_layout_texts
    assert "Delivered by " in live_layout_texts
    assert "Exasol" in live_layout_texts

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
    assert put_response.status_code == 200
    assert put_response.get_json()["result"]["structuredContent"]["draft"]["candidate_version"] == 2

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
    assert patch_manifest.status_code == 200
    assert patch_manifest.get_json()["result"]["structuredContent"]["draft"]["candidate_version"] == 3

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
    assert patch_app.status_code == 200
    assert patch_app.get_json()["result"]["structuredContent"]["draft"]["candidate_version"] == 4

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "sales"}},
        request_id=7,
    )
    validate_payload = validate_response.get_json()["result"]["structuredContent"]
    assert validate_payload["validation"]["is_valid"] is True
    assert validate_payload["validation"]["imports"]["status"] == "passed"
    assert validate_payload["validation"]["dependency_install"]["requirements"] == ["dash>=4.0,<5.0"]

    diff_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/sales/diff/current...draft"},
        request_id=8,
    )
    diff_payload = json.loads(diff_response.get_json()["result"]["contents"][0]["text"])
    assert "Sales Dashboard v2" in diff_payload["diff"]
    assert "notes.txt" in diff_payload["diff"]

    artifact_diff_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_diff_draft_vs_artifact", "arguments": {"name": "sales"}},
        request_id=9,
    )
    artifact_diff = artifact_diff_response.get_json()["result"]["structuredContent"]
    assert artifact_diff["target"] == "latest_build"
    assert artifact_diff["artifact"]["revision"]["revision_number"] == 1
    assert artifact_diff["draft"]["source_hash"] != artifact_diff["artifact"]["source_hash"]
    file_statuses = {entry["path"]: entry["status"] for entry in artifact_diff["files"]}
    assert file_statuses["app.py"] == "changed"
    assert file_statuses["dash-app.json"] == "changed"
    assert file_statuses["notes.txt"] == "draft_only"

    latest_build_diff = _resource_json(
        client,
        "dash://apps/sales/diff/latest-build...draft",
        request_id=10,
    )
    assert latest_build_diff["artifact"]["revision"]["revision_number"] == 1
    assert "Sales Dashboard v2" in latest_build_diff["diff"]
    assert "notes.txt" in latest_build_diff["diff"]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "sales"}},
        request_id=11,
    )
    built = build_response.get_json()["result"]["structuredContent"]
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

    latest_artifact_files = _resource_json(
        client,
        "dash://apps/sales/artifacts/latest/files",
        request_id=12,
    )
    assert latest_artifact_files["artifact"]["revision"]["revision_number"] == 2
    assert set(latest_artifact_files["artifact"]["files"]) >= {
        "app.py",
        "dash-app.json",
        "notes.txt",
        "requirements.txt",
    }

    post_build_diff = _resource_json(
        client,
        "dash://apps/sales/diff/latest-build...draft",
        request_id=13,
    )
    assert post_build_diff["artifact"]["revision"]["revision_number"] == 2
    assert post_build_diff["diff"] == ""
    assert all(entry["status"] == "unchanged" for entry in post_build_diff["files"])

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "sales", "revision_number": 2}},
        request_id=14,
    )
    assert preview_response.status_code == 200
    assert preview_response.get_json()["result"]["structuredContent"]["preview_revision"]["revision_number"] == 2
    assert b"Sales Dashboard v1" in client.get("/apps/sales").data
    assert b"Sales Dashboard v2" in client.get("/preview/sales/2").data
    live_layout_texts = _layout_texts(_dash_layout(client, "/apps/sales"))
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/sales/2"))
    assert "Initial live revision." in live_layout_texts
    assert "Updated revision through MCP." not in live_layout_texts
    assert "Updated revision through MCP." in preview_layout_texts
    assert "$1.2M" in preview_layout_texts

    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "sales", "revision_number": 2}},
        request_id=15,
    )
    promoted = promote_response.get_json()["result"]["structuredContent"]
    assert promoted["current_revision"]["revision_number"] == 2
    assert promoted["rollback_revision"]["revision_number"] == 1
    assert b"Sales Dashboard v2" in client.get("/apps/sales").data
    promoted_layout_texts = _layout_texts(_dash_layout(client, "/apps/sales"))
    assert "Updated revision through MCP." in promoted_layout_texts

    rollback_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_rollback", "arguments": {"name": "sales"}},
        request_id=16,
    )
    rolled_back = rollback_response.get_json()["result"]["structuredContent"]
    assert rolled_back["current_revision"]["revision_number"] == 1
    assert b"Sales Dashboard v1" in client.get("/apps/sales").data
    rolled_back_layout_texts = _layout_texts(_dash_layout(client, "/apps/sales"))
    assert "Initial live revision." in rolled_back_layout_texts
    assert "Updated revision through MCP." not in rolled_back_layout_texts

    delete_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_delete_file", "arguments": {"name": "sales", "path": "notes.txt"}},
        request_id=17,
    )
    deleted = delete_response.get_json()["result"]["structuredContent"]
    assert deleted["draft"]["candidate_version"] == 5
    assert "notes.txt" not in deleted["draft"]["files"]


def test_mcp_validation_and_build_fail_for_invalid_python_draft(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "ops",
                    "Ops Dashboard v1",
                    summary="Initial ops revision.",
                    revenue="$3.2M",
                )
            },
        },
        request_id=14,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "ops",
                "files": [{"path": "app.py", "content": "def broken(\n"}],
            },
        },
        request_id=15,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "ops"}},
        request_id=16,
    )
    validation = validate_response.get_json()["result"]["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["syntax"]["status"] == "failed"
    assert validation["imports"]["status"] == "skipped"

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "ops"}},
        request_id=17,
    )
    assert build_response.status_code == 200
    result = build_response.get_json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "workspace_validation_error"
    assert "Syntax error in app.py" in result["content"][0]["text"]


def test_mcp_can_collect_diagnostics_repair_import_failure_and_redeploy(client):
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "support",
                    "Support Dashboard v1",
                    summary="Initial support revision.",
                    revenue="$640K",
                )
            },
        },
        request_id=18,
    )
    assert create_response.status_code == 200

    break_imports = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_patch_file",
            "arguments": {
                "name": "support",
                "path": "app.py",
                "search": "from dash import Dash, Input, Output, dcc, html",
                "replace": "from totally_missing_package import Dash, Input, Output, dcc, html",
            },
        },
        request_id=19,
    )
    assert break_imports.status_code == 200

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "support"}},
        request_id=20,
    )
    validation = validate_response.get_json()["result"]["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["imports"]["status"] == "failed"
    assert "totally_missing_package" in validation["imports"]["error"]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "support"}},
        request_id=21,
    )
    assert build_response.status_code == 200

    diagnostics_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "support"}},
        request_id=22,
    )
    diagnostics = diagnostics_response.get_json()["result"]["structuredContent"]
    assert diagnostics["latest_build_result"]["status"] == "failed"
    assert diagnostics["latest_error"] is None
    assert diagnostics["latest_build_error"]["category"] == "import_error"
    assert diagnostics["latest_built_revision"]["revision_number"] == 1
    draft_vs_latest_statuses = {
        entry["path"]: entry["status"] for entry in diagnostics["draft_vs_latest_build"]["files"]
    }
    assert draft_vs_latest_statuses["app.py"] == "changed"
    assert diagnostics["artifact_comparison"]["focused_file"]["path"] == "app.py"
    assert diagnostics["artifact_comparison"]["source_context"] == "current_draft"
    assert diagnostics["parsed_traceback"]["category"] == "import_error"
    assert diagnostics["health"]["status"] == "healthy"
    assert "Restore valid imports in app.py or requirements.txt." in diagnostics["suggested_recovery_steps"]

    logs_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/support/logs/latest"},
        request_id=23,
    )
    logs_payload = json.loads(logs_response.get_json()["result"]["contents"][0]["text"])
    assert any(
        "Workspace validation failed" in entry["message"]
        for entry in logs_payload["logs"]["entries"]
    )

    errors_response = _call_mcp(
        client,
        "resources/read",
        {"uri": "dash://apps/support/errors"},
        request_id=24,
    )
    errors_payload = json.loads(errors_response.get_json()["result"]["contents"][0]["text"])
    assert errors_payload["errors"][-1]["category"] == "import_error"

    health_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_run_healthcheck", "arguments": {"name": "support"}},
        request_id=25,
    )
    health_payload = health_response.get_json()["result"]["structuredContent"]
    assert health_payload["health"]["status"] == "healthy"
    # `worker_alive` returns status=not_applicable when running in in_process mode, which
    # is the default for this test. All other probes must pass.
    assert all(
        probe["status"] in {"passed", "not_applicable"}
        for probe in health_payload["health"]["probes"]
    )

    build_logs_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_tail_logs", "arguments": {"name": "support", "channel": "build", "limit": 5}},
        request_id=26,
    )
    build_logs_result = build_logs_response.get_json()["result"]
    assert build_logs_result["isError"] is False
    assert '"channel": "build"' in build_logs_result["content"][0]["text"]
    assert "Workspace validation failed" in build_logs_result["content"][0]["text"]

    fix_imports = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_patch_file",
            "arguments": {
                "name": "support",
                "path": "app.py",
                "search": "from totally_missing_package import Dash, Input, Output, dcc, html",
                "replace": "from dash import Dash, Input, Output, dcc, html",
            },
        },
        request_id=26,
    )
    assert fix_imports.status_code == 200

    validate_fixed = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "support"}},
        request_id=27,
    )
    assert validate_fixed.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_fixed = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "support"}},
        request_id=28,
    )
    built = build_fixed.get_json()["result"]["structuredContent"]
    assert built["revision"]["revision_number"] == 2

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "support", "revision_number": 2}},
        request_id=29,
    )
    assert preview_response.status_code == 200
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/support/2"))
    assert "Support Dashboard v1" in preview_layout_texts

    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "support", "revision_number": 2}},
        request_id=30,
    )
    assert promote_response.status_code == 200
    live_layout_texts = _layout_texts(_dash_layout(client, "/apps/support"))
    assert "Support Dashboard v1" in live_layout_texts

    diagnostics_after_fix = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "support"}},
        request_id=31,
    )
    after_fix_payload = diagnostics_after_fix.get_json()["result"]["structuredContent"]
    assert after_fix_payload["latest_build_result"]["status"] == "succeeded"
    assert after_fix_payload["health"]["status"] == "healthy"


def test_app_validate_reports_callback_inventory_for_healthy_app(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "callback-report"}}},
        request_id=31,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "callback-report",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Callback Report")}],
            },
        },
        request_id=32,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "callback-report"}},
        request_id=33,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is True
    assert validation["callbacks"]["status"] == "passed"
    assert validation["callbacks"]["count"] == 1
    callback = validation["callbacks"]["callbacks"][0]
    assert callback["outputs"][0]["id"] == "clock"
    assert callback["inputs"][0]["id"] == "tick"
    assert "Registered callbacks: 1" in result["content"][0]["text"]


def test_app_validate_fails_fast_for_missing_local_symbol_import_and_recovers(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-import"}}},
        request_id=34,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-import",
                "files": [
                    {
                        "path": "app.py",
                        "content": _cross_module_from_import_app_py("Cross Module Import"),
                    },
                    {"path": "theme.py", "content": "from dash import html\n"},
                ],
            },
        },
        request_id=35,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-import"}},
        request_id=36,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    issue = validation["cross_module_symbols"]["issues"][0]
    assert validation["is_valid"] is False
    assert validation["cross_module_symbols"]["status"] == "failed"
    assert issue["path"] == "app.py"
    assert issue["line"] == 2
    assert issue["reference"] == "labeled_bar"
    assert issue["target_path"] == "theme.py"
    assert "theme.labeled_bar" in issue["message"]
    assert validation["dependency_install"]["status"] == "skipped"
    assert validation["imports"]["status"] == "skipped"
    assert validation["imports"]["category"] == "cross_module_symbols_failed"
    assert "Cross-module symbol validation failed" in result["content"][0]["text"]
    assert (
        result["structuredContent"]["guidance"]["next_step"]
        == "Patch the missing local symbol or import path, then validate the draft again."
    )
    assert "force_clean" not in result["content"][0]["text"]
    assert "force_clean" not in result["structuredContent"]["guidance"]["next_step"]
    assert result["structuredContent"]["guidance"]["suggested_tools"] == [
        "app_patch_file",
        "app_put_files",
        "app_validate",
    ]

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-import",
                "files": [{"path": "theme.py", "content": _theme_labeled_bar_py()}],
            },
        },
        request_id=37,
    )

    validate_fixed = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-import"}},
        request_id=38,
    )
    fixed_validation = validate_fixed.get_json()["result"]["structuredContent"]["validation"]
    assert fixed_validation["is_valid"] is True
    assert fixed_validation["cross_module_symbols"]["status"] == "passed"
    assert fixed_validation["imports"]["status"] == "passed"

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "cross-module-import", "deployment_target": "preview"},
        },
        request_id=39,
    )
    deploy_payload = deploy_response.get_json()["result"]["structuredContent"]
    assert deploy_payload["deployment"]["preview_revision"]["revision_number"] == 2
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/cross-module-import/2"))
    assert "Labeled Revenue" in preview_layout_texts


def test_app_validate_reports_missing_local_symbol_for_aliased_module_attribute(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-alias"}}},
        request_id=40,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-alias",
                "files": [
                    {"path": "app.py", "content": _cross_module_alias_app_py("Cross Module Alias")},
                    {"path": "theme.py", "content": "from dash import html\n"},
                ],
            },
        },
        request_id=41,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-alias"}},
        request_id=42,
    )
    validation = validate_response.get_json()["result"]["structuredContent"]["validation"]
    issue = validation["cross_module_symbols"]["issues"][0]
    assert validation["is_valid"] is False
    assert validation["cross_module_symbols"]["status"] == "failed"
    assert issue["path"] == "app.py"
    assert issue["line"] == 14
    assert issue["reference"] == "T.labeled_bar"
    assert issue["target_path"] == "theme.py"
    assert "theme.labeled_bar" in issue["message"]


def test_app_validate_fails_when_callback_references_missing_layout_ids(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "missing-callback-id"}}},
        request_id=34,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "missing-callback-id",
                "files": [{"path": "app.py", "content": _missing_callback_id_app_py("Missing Callback Id")}],
            },
        },
        request_id=35,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "missing-callback-id"}},
        request_id=36,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["imports"]["status"] == "passed"
    assert validation["callbacks"]["status"] == "failed"
    assert validation["callbacks"]["missing_layout_ids"] == ["missing-output"]
    assert "Callback validation failed" in result["content"][0]["text"]


def test_app_validate_keeps_wildcard_local_import_checks_non_fatal(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-wildcard"}}},
        request_id=37,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-wildcard",
                "files": [
                    {
                        "path": "app.py",
                        "content": _cross_module_wildcard_app_py("Cross Module Wildcard"),
                    },
                    {"path": "theme.py", "content": "VALUE = 1\n"},
                ],
            },
        },
        request_id=38,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-wildcard"}},
        request_id=39,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    warning = validation["cross_module_symbols"]["warnings"][0]
    assert validation["is_valid"] is True
    assert validation["cross_module_symbols"]["status"] == "passed_with_warnings"
    assert warning["path"] == "app.py"
    assert warning["line"] == 2
    assert "wildcard import from theme" in warning["message"]
    assert "Cross-module symbol warning" in result["content"][0]["text"]


def test_app_validate_surfaces_plotly_specific_lint_warnings(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "plotly-lint"}}},
        request_id=37,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "plotly-lint",
                "files": [
                    {"path": "app.py", "content": _plotly_lint_app_py("Plotly Lint")},
                    {"path": "requirements.txt", "content": "dash\nplotly\n"},
                ],
            },
        },
        request_id=38,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "plotly-lint"}},
        request_id=39,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["imports"]["status"] == "failed"
    assert "fillcolor" in validation["imports"]["error"]
    assert validation["lint"]["status"] == "passed_with_warnings"
    warning_messages = [warning["message"] for warning in validation["lint"]["warnings"]]
    assert any("fillcolor uses 8-digit hex" in message for message in warning_messages)
    assert any("update_layout may set 'margin' twice" in message for message in warning_messages)
    assert "Import smoke check failed" in result["content"][0]["text"]
    assert "Lint warning in app.py" in result["content"][0]["text"]


def test_mcp_can_inspect_runtime_traceback_text(client):
    traceback_text = """Traceback (most recent call last):
  File "/srv/support/app.py", line 19, in create_dash_app
    raise RuntimeError("boom")
RuntimeError: boom
"""
    inspect_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_inspect_traceback",
            "arguments": {
                "name": "demo",
                "traceback_text": traceback_text,
            },
        },
        request_id=32,
    )
    inspected = inspect_response.get_json()["result"]["structuredContent"]["traceback"]
    assert inspected["category"] == "runtime_crash"
    assert inspected["exception_type"] == "RuntimeError"
    assert inspected["frames"][0]["file"] == "/srv/support/app.py"


def test_app_inspect_traceback_ignores_stale_errors_from_older_revisions(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "traceback-app"}}},
        request_id=33,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "traceback-app",
                "files": [
                    {
                        "path": "app.py",
                        "content": _callback_failure_app_py("Traceback App"),
                    }
                ],
            },
        },
        request_id=34,
    )
    failing_deploy = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "traceback-app"}},
        request_id=35,
    )
    assert failing_deploy.get_json()["result"]["isError"] is False

    callback_fail = _dash_callback(
        client,
        "/apps/traceback-app",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "explode"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_fail.status_code == 500

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "traceback-app",
                "files": [
                    {
                        "path": "app.py",
                        "content": _app_callback_app_py("Traceback App Healthy"),
                    }
                ],
            },
        },
        request_id=36,
    )
    healthy_deploy = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "traceback-app"}},
        request_id=37,
    )
    assert healthy_deploy.get_json()["result"]["isError"] is False
    assert client.get("/apps/traceback-app").status_code == 200

    inspect_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_inspect_traceback", "arguments": {"name": "traceback-app"}},
        request_id=38,
    )
    result = inspect_response.get_json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "diagnostics_not_found"
    assert "current revision or latest failed build" in result["structuredContent"]["error"]["summary"]


def test_collect_diagnostics_and_inspect_traceback_attribute_artifact_mismatch(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "artifact-mismatch",
                    "Artifact Mismatch v1",
                    summary="Initial live revision.",
                    revenue="$540K",
                )
            },
        },
        request_id=380,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "artifact-mismatch",
                "files": [
                    {
                        "path": "app.py",
                        "content": _artifact_sensitive_app_py(
                            "Artifact Mismatch v2",
                            failure_mode="preview",
                        ),
                    }
                ],
            },
        },
        request_id=381,
    )
    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "artifact-mismatch"}},
        request_id=382,
    )
    built = build_response.get_json()["result"]["structuredContent"]
    assert built["revision"]["revision_number"] == 2

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "artifact-mismatch", "revision_number": 2}},
        request_id=383,
    )
    preview_result = preview_response.get_json()["result"]
    assert preview_result["isError"] is True
    traceback_text = preview_result["structuredContent"]["error"]["details"]["traceback_text"]
    assert "preview artifact mount exploded" in traceback_text

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "artifact-mismatch",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Artifact Mismatch Healthy")}],
            },
        },
        request_id=384,
    )

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "artifact-mismatch"}},
        request_id=385,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_runtime_error"]["category"] == "runtime_crash"
    assert diagnostics["latest_built_revision"]["revision_number"] == 2
    draft_vs_latest_statuses = {
        entry["path"]: entry["status"] for entry in diagnostics["draft_vs_latest_build"]["files"]
    }
    assert draft_vs_latest_statuses["app.py"] == "changed"
    assert diagnostics["artifact_comparison"]["focused_file"]["path"] == "app.py"
    assert diagnostics["artifact_comparison"]["source_context"] == "latest_built_artifact"
    assert any(
        "newer content for app.py" in hint
        for hint in diagnostics["artifact_comparison"]["hints"]
    )

    inspect_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_inspect_traceback",
            "arguments": {
                "name": "artifact-mismatch",
                "traceback_text": traceback_text,
            },
        },
        request_id=386,
    )
    inspected = inspect_response.get_json()["result"]["structuredContent"]
    assert inspected["artifact_comparison"]["focused_file"]["path"] == "app.py"
    assert inspected["artifact_comparison"]["source_context"] == "latest_built_artifact"
    assert any(
        "latest built artifact" in hint or "newer content for app.py" in hint
        for hint in inspected["artifact_guidance"]
    )
    assert inspected["suggested_recovery_steps"][0].startswith(
        "Compare the current draft against the latest built artifact"
    )


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


def test_mcp_records_callback_failures_from_live_dash_app(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "alerts",
                    "Alerts Dashboard v1",
                    summary="Initial alerts revision.",
                    revenue="$320K",
                )
            },
        },
        request_id=50,
    )

    put_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "alerts",
                "files": [{"path": "app.py", "content": _callback_failure_app_py("Alerts Workspace App")}],
            },
        },
        request_id=51,
    )
    assert put_response.status_code == 200

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "alerts"}},
        request_id=52,
    )
    assert build_response.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "alerts", "revision_number": 2}},
        request_id=53,
    )
    assert promote_response.status_code == 200

    callback_ok = _dash_callback(
        client,
        "/apps/alerts",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "safe"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_ok.status_code == 200
    assert "Mode: safe" in callback_ok.get_data(as_text=True)

    callback_fail = _dash_callback(
        client,
        "/apps/alerts",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "explode"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_fail.status_code == 500

    callback_failures = _resource_json(
        client,
        "dash://apps/alerts/callback-failures",
        request_id=54,
    )
    latest_failure = callback_failures["callback_failures"][-1]
    assert latest_failure["category"] == "dash_callback_error"
    assert latest_failure["details"]["path"].endswith("/_dash-update-component")
    assert latest_failure["details"]["output"] == "callback-result.children"
    assert latest_failure["details"]["changed_prop_ids"] == ["mode.value"]
    assert latest_failure["details"]["inputs"][0]["value"] == "explode"
    assert latest_failure["parsed_traceback"]["exception_type"] == "RuntimeError"

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "alerts"}},
        request_id=55,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["callback_failure_summary"]["callback_failures"][-1]["category"] == "dash_callback_error"
    assert "Inspect the callback function and the referenced component ids." in diagnostics["suggested_recovery_steps"]


def test_validation_does_not_leak_dash_global_callbacks_across_revisions(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "clock-app"}},
        },
        request_id=56,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "clock-app",
                "files": [{"path": "app.py", "content": _global_callback_app_py("Clock App")}],
            },
        },
        request_id=57,
    )

    validate_global = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "clock-app"}},
        request_id=58,
    )
    assert validate_global.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_global = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "clock-app"}},
        request_id=59,
    )
    assert build_global.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "clock-app",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Clock App")}],
            },
        },
        request_id=60,
    )

    validate_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "clock-app"}},
        request_id=61,
    )
    assert validate_app.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "clock-app"}},
        request_id=62,
    )
    assert build_app.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 3

    promote_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "clock-app", "revision_number": 3}},
        request_id=63,
    )
    assert promote_app.get_json()["result"]["structuredContent"]["current_revision"]["revision_number"] == 3

    live_response = client.get("/apps/clock-app")
    assert live_response.status_code == 200
    live_layout_texts = _layout_texts(_dash_layout(client, "/apps/clock-app"))
    assert "App Callback Revision" in live_layout_texts


def test_mcp_rejects_dash_apps_that_do_not_serve_the_mounted_root(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "broken-prefix"}},
        },
        request_id=64,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "broken-prefix",
                "files": [{"path": "app.py", "content": _misconfigured_prefix_app_py("Broken Prefix")}],
            },
        },
        request_id=65,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "broken-prefix"}},
        request_id=66,
    )
    validate_result = validate_response.get_json()["result"]
    validate_payload = validate_result["structuredContent"]["validation"]
    assert validate_payload["is_valid"] is False
    assert validate_payload["imports"]["category"] == "route_misconfiguration"
    assert validate_payload["imports"]["details"]["path"] in {"/", "/_dash-layout"}
    assert "Mounted route verification failed" in validate_result["content"][0]["text"]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "broken-prefix"}},
        request_id=67,
    )
    build_result = build_response.get_json()["result"]
    assert build_result["isError"] is True
    assert build_result["structuredContent"]["error"]["category"] == "workspace_validation_error"
    assert "Mounted route verification failed" in build_result["content"][0]["text"]

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "broken-prefix"}},
        request_id=68,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_error"] is None
    assert diagnostics["latest_build_error"]["category"] == "route_misconfiguration"
    assert "Use routes_pathname_prefix='/'" in diagnostics["suggested_recovery_steps"][1]


def test_mcp_dependency_report_captures_invalid_requirement_entries(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "finance",
                    "Finance Dashboard v1",
                    summary="Initial finance revision.",
                    revenue="$4.1M",
                )
            },
        },
        request_id=56,
    )

    requirements_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "finance",
                "files": [
                    {
                        "path": "requirements.txt",
                        "content": "dash=>2.18\npandas=2.2\nplotly>=5\n",
                    }
                ],
            },
        },
        request_id=57,
    )
    assert requirements_response.status_code == 200

    validation = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "finance"}},
        request_id=58,
    ).get_json()["result"]["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["requirements"]["invalid"] == ["dash=>2.18", "pandas=2.2"]

    dependency_report = _resource_json(
        client,
        "dash://apps/finance/dependency-report",
        request_id=59,
    )
    assert dependency_report["dependency_report"]["declared_requirements"] == ["plotly>=5"]
    assert dependency_report["dependency_report"]["invalid_requirements"] == [
        "dash=>2.18",
        "pandas=2.2",
    ]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "finance"}},
        request_id=60,
    )
    assert build_response.status_code == 200
    build_result = build_response.get_json()["result"]
    assert build_result["isError"] is True
    assert build_result["structuredContent"]["error"]["category"] == "workspace_validation_error"
    assert "Invalid requirements: dash=>2.18, pandas=2.2" in build_result["content"][0]["text"]

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "finance"}},
        request_id=61,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_error"] is None
    assert diagnostics["latest_build_error"]["category"] == "dependency_conflict"
    assert "Correct invalid or conflicting requirement specifiers in requirements.txt." in diagnostics["suggested_recovery_steps"]


def test_mcp_surfaces_preview_and_startup_runtime_mount_failures(client):
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
