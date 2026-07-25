"""Shared helpers for the split ``test_mcp_*`` feature modules.

Extracted verbatim from the former monolithic ``tests/test_mcp.py`` so the
authoring / lifecycle / validation / diagnostics / resources modules share one
copy of the JSON-RPC plumbing, layout walkers, and Dash-app source builders.
"""

from __future__ import annotations

import json
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

