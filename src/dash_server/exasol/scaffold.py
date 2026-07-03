"""Scaffold generation for Exasol-backed hosted Dash apps."""

from __future__ import annotations

import json
from textwrap import dedent
from typing import Any

EXASOL_DASHBOARD_PATTERNS = ("analytics-hub", "overview", "kpi-trend", "ops-monitor")
EXASOL_SCAFFOLD_REQUIREMENTS = "dash>=4.0,<5.0\nplotly>=5.18\npyexasol>=2.2.2,<3.0\n"


def build_exasol_dashboard_bundle(
    *,
    app_name: str,
    title: str,
    route: str,
    description: str,
    profile_name: str,
    pattern: str = "analytics-hub",
) -> dict[str, Any]:
    """Return a files-based bundle for an Exasol dashboard app."""

    if pattern not in EXASOL_DASHBOARD_PATTERNS:
        raise ValueError(f"Unsupported Exasol dashboard pattern: {pattern}")

    manifest = {
        "name": app_name,
        "title": title,
        "route": route,
        "description": description,
        "template": "exasol-analytics",
        "data_sources": {
            "primary": {
                "kind": "exasol",
                "profile": profile_name,
                "auth_mode": "local_direct",
            }
        },
    }
    files = [
        {"path": "dash-app.json", "content": json.dumps(manifest, indent=2) + "\n"},
        {"path": "app.py", "content": _render_app_py(title, pattern)},
        {"path": "dash_server_exasol.py", "content": _render_helper_py()},
        *[
            {"path": relative_path, "content": content}
            for relative_path, content in _pattern_sql_files(pattern).items()
        ],
        {
            "path": "requirements.txt",
            "content": EXASOL_SCAFFOLD_REQUIREMENTS,
        },
    ]
    return {
        "name": app_name,
        "title": title,
        "route": route,
        "description": description,
        "template": "exasol-analytics",
        "data_sources": manifest["data_sources"],
        "pattern": pattern,
        "files": files,
    }


def build_schema_scaffold_bundle(
    *,
    app_name: str,
    title: str,
    route: str,
    description: str,
    profile_name: str,
    blueprint: dict[str, Any],
) -> dict[str, Any]:
    """Return a schema-tailored Exasol analytics bundle."""

    manifest = {
        "name": app_name,
        "title": title,
        "route": route,
        "description": description,
        "template": "exasol-analytics",
        "data_sources": {
            "primary": {
                "kind": "exasol",
                "profile": profile_name,
                "auth_mode": "local_direct",
            }
        },
    }
    files = [
        {"path": "dash-app.json", "content": json.dumps(manifest, indent=2) + "\n"},
        {
            "path": "app.py",
            "content": _render_analytics_hub_app_py(
                title,
                business_context={
                    "caption": blueprint["business_caption"],
                    "summary_heading": blueprint["summary_heading"],
                    "chart_heading": blueprint["chart_heading"],
                    "table_heading": blueprint["table_heading"],
                },
            ),
        },
        {"path": "dash_server_exasol.py", "content": _render_helper_py()},
        {"path": "queries/system/meta.sql", "content": _ops_meta_sql()},
        {"path": "queries/system/monitor.sql", "content": _ops_monitor_sql()},
        {"path": "queries/system/usage.sql", "content": _ops_usage_sql()},
        {"path": "queries/system/sql_hist.sql", "content": _ops_sql_hist_sql()},
        {"path": "queries/business/summary.sql", "content": _schema_summary_sql(blueprint)},
        {"path": "queries/business/trend.sql", "content": _schema_trend_sql(blueprint)},
        {"path": "queries/business/detail.sql", "content": _schema_detail_sql(blueprint)},
        {"path": "SCHEMA_NOTES.md", "content": _schema_notes_md(blueprint)},
        {
            "path": "requirements.txt",
            "content": EXASOL_SCAFFOLD_REQUIREMENTS,
        },
    ]
    return {
        "name": app_name,
        "title": title,
        "route": route,
        "description": description,
        "template": "exasol-analytics",
        "data_sources": manifest["data_sources"],
        "pattern": "analytics-hub",
        "schema_blueprint": blueprint,
        "files": files,
    }


def exasol_connection_modes_help() -> dict[str, Any]:
    return {
        "resource": "dash://exasol/help/connection-modes",
        "summary": "Supported Exasol connection modes for phase 0 local dashboard creation.",
        "security_rules": [
            "Store Exasol profile metadata in the server and keep secret values outside Git.",
            "Generated or uploaded hosted apps must not embed DSN, user, password, PAT, or token values in source files.",
            "Hosted apps should refer to Exasol profiles by name and let the server resolve credentials.",
            "Do not call pyexasol.connect(...) directly from hosted app code unless the server architecture is explicitly extended to support that safely.",
        ],
        "modes": [
            {
                "name": "local_onprem_password",
                "backend": "onprem",
                "credential_mode": "password",
                "required_fields": ["name", "dsn", "user", "secret_value or secret_env_var"],
            },
            {
                "name": "local_onprem_access_token",
                "backend": "onprem",
                "credential_mode": "access_token",
                "required_fields": ["name", "dsn", "user", "secret_value or secret_env_var"],
            },
            {
                "name": "local_onprem_refresh_token",
                "backend": "onprem",
                "credential_mode": "refresh_token",
                "required_fields": ["name", "dsn", "user", "secret_value or secret_env_var"],
            },
            {
                "name": "local_saas_pat",
                "backend": "saas",
                "credential_mode": "saas_pat",
                "required_fields": ["name", "dsn", "user", "secret_value or secret_env_var"],
            },
        ],
        "recommended_workflow": [
            "Create a local Exasol profile with exasol_profile_create_local.",
            "Validate the profile with exasol_profile_validate.",
            "Create a hosted dashboard with app_create_exasol_dashboard.",
            "Inspect or patch the generated SQL and deploy revisions as usual.",
            "Do not rewrite the generated app to own Exasol credentials in app.py.",
        ],
    }


def exasol_sql_placeholders_help() -> dict[str, Any]:
    return {
        "resource": "dash://exasol/help/sql-placeholders",
        "summary": (
            "How to parameterize SQL files in hosted dashboards. dash-server uses pyexasol's "
            "client-side format-style placeholders. SQL-driver-style :name placeholders are NOT "
            "supported and produce 'Feature not supported: host parameter specification' from Exasol."
        ),
        "placeholders": [
            {
                "syntax": "{name} or {name!s}",
                "use_for": "string literal",
                "renders_to": "single-quoted SQL string",
                "example_sql": "WHERE region = {region!s}",
                "example_params": {"region": "EMEA"},
                "rendered": "WHERE region = 'EMEA'",
                "notes": "Empty Python string '' renders as SQL NULL, so 'cohort = {x!s}' with x='' will not match empty-string rows. Branch on the param being None/empty in Python instead.",
            },
            {
                "syntax": "{name!d}",
                "use_for": "validated decimal/integer",
                "renders_to": "unquoted numeric literal",
                "example_sql": "WHERE created_at >= ADD_DAYS(CURRENT_TIMESTAMP, -{days!d})",
                "example_params": {"days": 30},
                "rendered": "WHERE created_at >= ADD_DAYS(CURRENT_TIMESTAMP, -30)",
            },
            {
                "syntax": "{name!f}",
                "use_for": "validated float",
                "renders_to": "unquoted float literal",
                "example_sql": "WHERE temperature >= {threshold!f}",
                "example_params": {"threshold": 0.85},
                "rendered": "WHERE temperature >= 0.85",
            },
            {
                "syntax": "{name!i}",
                "use_for": "safe identifier (table/column name)",
                "renders_to": "unquoted identifier with validation",
                "example_sql": "SELECT * FROM {table!i}",
                "example_params": {"table": "SALES"},
                "rendered": "SELECT * FROM SALES",
            },
            {
                "syntax": "{name!q}",
                "use_for": "quoted identifier (case-preserving / reserved-word safe)",
                "renders_to": "double-quoted identifier",
                "example_sql": "SELECT {col!q} FROM SALES",
                "example_params": {"col": "DAY"},
                "rendered": "SELECT \"DAY\" FROM SALES",
            },
            {
                "syntax": "{name!r}",
                "use_for": "raw SQL fragment (no escaping)",
                "renders_to": "verbatim insertion",
                "example_sql": "ORDER BY revenue {direction!r}",
                "example_params": {"direction": "DESC"},
                "rendered": "ORDER BY revenue DESC",
                "notes": "Never substitute user-controlled values via !r; it is the SQL-injection path.",
            },
        ],
        "rules": [
            "Never write :name placeholders. Exasol rejects them with 0A000 / 'Feature not supported: host parameter specification'.",
            (
                "Reserved identifiers in AS clauses must be quoted. Common offenders include: "
                "VALUE, COUNT, SUM, KEY, TYPE, NAME, STATUS, DAY, MONTH, YEAR, LEVEL, ORDER, "
                "GROUP, USER, ROLE, SESSION, TIME, ZONE. Quote with double quotes: AS \"VALUE\"."
            ),
            "Empty Python strings substitute to NULL. Branch on None/'' in Python rather than relying on '= ''' filters.",
        ],
        "common_mistakes": [
            "Using :name like SQLAlchemy or psycopg2; pyexasol does not recognize it.",
            "Using %s or %(name)s; pyexasol does not recognize it.",
            "Writing AS VALUE (or AS COUNT, AS SUM, AS TYPE, AS KEY) without quoting. These are reserved in Exasol.",
            "Treating '' the same as NULL; in Exasol they are equivalent for equality, which can produce zero rows from a 'show-all' filter.",
        ],
        "related_resources": [
            "dash://exasol/help/dashboard-patterns",
            "dash://meta/app-authoring-guide",
        ],
    }


def exasol_dashboard_patterns_help() -> dict[str, Any]:
    return {
        "resource": "dash://exasol/help/dashboard-patterns",
        "summary": "Built-in Exasol dashboard patterns for profile-bound hosted app scaffolds.",
        "patterns": [
            {
                "name": "analytics-hub",
                "best_for": "The default exasol-analytics scaffold: a multi-tab app with system health, query history, and a business analytics placeholder ready for schema-specific SQL.",
                "sql_kind": "real",
                "generated_files": [
                    "queries/system/meta.sql",
                    "queries/system/monitor.sql",
                    "queries/system/usage.sql",
                    "queries/system/sql_hist.sql",
                    "queries/business/summary.sql",
                    "queries/business/trend.sql",
                    "queries/business/detail.sql",
                ],
            },
            {
                "name": "overview",
                "best_for": (
                    "Demo-only KPI/detail layout. Use this when you want to look at the layout "
                    "shape before binding real SQL — the generated `queries/summary.sql` and "
                    "`queries/detail.sql` are `SELECT … FROM DUAL` stubs with hard-coded "
                    "numbers, not catalog-backed queries. For a real schema-bound scaffold, "
                    "call `app_scaffold_from_schema` instead."
                ),
                "sql_kind": "demo_placeholder",
                "generated_files": ["queries/summary.sql", "queries/detail.sql"],
            },
            {
                "name": "kpi-trend",
                "best_for": (
                    "Demo-only KPI + trend + detail layout. Like `overview`, the generated "
                    "`queries/*.sql` are `SELECT … FROM DUAL` stubs with hard-coded data. "
                    "For a real schema-bound trend dashboard, call `app_scaffold_from_schema` "
                    "(it picks a date column from your schema and emits catalog-backed SQL)."
                ),
                "sql_kind": "demo_placeholder",
                "generated_files": ["queries/summary.sql", "queries/trend.sql", "queries/detail.sql"],
            },
            {
                "name": "ops-monitor",
                "best_for": "Operational Exasol monitoring dashboards using metadata, session, usage, and SQL history queries.",
                "sql_kind": "real",
                "generated_files": [
                    "queries/meta.sql",
                    "queries/sessions.sql",
                    "queries/monitor.sql",
                    "queries/usage.sql",
                    "queries/sql_hist.sql",
                ],
            },
        ],
        "template_guide": {
            "metric-cards": "Generic starter app with static metric cards. Use it when you are not building around a live Exasol profile.",
            "exasol-analytics": "Profile-bound analytical scaffold with SQL files, a runtime helper, and a multi-tab structure designed for live Exasol queries.",
        },
        "recommendation": (
            "For catalog-bound business dashboards prefer `app_scaffold_from_schema` over `app_create_exasol_dashboard {pattern: kpi-trend/overview}` — "
            "the schema-aware path emits real SQL against a chosen table, while `kpi-trend` and `overview` ship demo `SELECT … FROM DUAL` stubs. "
            "Use `analytics-hub` as the default Exasol scaffold and `ops-monitor` for database-operations views."
        ),
    }


def exasol_agent_workflow_help() -> dict[str, Any]:
    return {
        "resource": "dash://exasol/help/agent-workflow",
        "summary": "How dash-server should coexist with a separate Exasol MCP server in agent workflows.",
        "roles": {
            "dash_server": [
                "Owns Exasol profile metadata and secret references for hosted dashboards.",
                "Executes dashboard SQL at runtime through the configured profile.",
                "Generates safe Exasol dashboard scaffolds and validates hosted app safety rules.",
            ],
            "external_exasol_mcp": [
                "Explore schemas, tables, columns, and system metadata interactively.",
                "Prototype and refine SQL during authoring time.",
                "Help the agent understand business entities and available database structures.",
            ],
        },
        "rules": [
            "Use the external Exasol MCP server for discovery and SQL design, not for dashboard runtime execution.",
            "Use dash-server profiles for hosted dashboards so credentials stay server-side.",
            "Do not copy DSN, user, password, PAT, token, or raw secret values from Exasol MCP output into app files.",
            "Do not make a hosted dashboard depend on live MCP tool calls. Put final runtime SQL in queries/*.sql and execute it through dash_server_exasol.py or server-side helpers.",
        ],
        "recommended_workflow": [
            "Use the external Exasol MCP server to inspect schema, test candidate SQL, and understand the dataset.",
            "Create or validate a dash-server Exasol profile with exasol_profile_create_local and exasol_profile_validate.",
            "Generate the hosted app with app_create_exasol_dashboard or app_scaffold_from_schema.",
            "Copy only safe SQL and structural findings into queries/*.sql; keep credentials and connection setup in the dash-server profile.",
            "Deploy to a preview URL first, then promote the approved revision live.",
        ],
    }


def _pattern_sql_files(pattern: str) -> dict[str, str]:
    if pattern == "analytics-hub":
        return {
            "queries/system/meta.sql": _ops_meta_sql(),
            "queries/system/monitor.sql": _ops_monitor_sql(),
            "queries/system/usage.sql": _ops_usage_sql(),
            "queries/system/sql_hist.sql": _ops_sql_hist_sql(),
            "queries/business/summary.sql": _placeholder_business_summary_sql(),
            "queries/business/trend.sql": _placeholder_business_trend_sql(),
            "queries/business/detail.sql": _placeholder_business_detail_sql(),
        }
    if pattern == "overview":
        return {
            "queries/summary.sql": _overview_summary_sql(),
            "queries/detail.sql": _overview_detail_sql(),
        }
    if pattern == "kpi-trend":
        return {
            "queries/summary.sql": _kpi_summary_sql(),
            "queries/trend.sql": _kpi_trend_sql(),
            "queries/detail.sql": _kpi_detail_sql(),
        }
    return {
        "queries/meta.sql": _ops_meta_sql(),
        "queries/sessions.sql": _ops_sessions_sql(),
        "queries/monitor.sql": _ops_monitor_sql(),
        "queries/usage.sql": _ops_usage_sql(),
        "queries/sql_hist.sql": _ops_sql_hist_sql(),
    }


def render_exasol_helper_py() -> str:
    """Return the canonical contents of ``dash_server_exasol.py`` for the exasol-analytics template."""

    return _render_helper_py()


def _render_helper_py() -> str:
    return dedent(
        '''
        """Hosted Dash → Exasol helpers (auto-generated by app_create_exasol_dashboard).

        Pyexasol placeholder syntax (use this, not :name):

            queries/example.sql:
                SELECT region, SUM(revenue) AS REV
                FROM SALES
                WHERE order_ts >= ADD_DAYS(CURRENT_TIMESTAMP, -{days!d})
                  AND region = {region!s}
                GROUP BY region

            params={"days": 30, "region": "EMEA"}

        Placeholder cheat sheet (see dash://exasol/help/sql-placeholders for full table):

            {x!s}  -> 'string'        empty string substitutes to SQL NULL
            {x!d}  -> 1234            validated integer/decimal, unquoted
            {x!f}  -> 1.23            validated float, unquoted
            {x!i}  -> SALES           safe identifier
            {x!q}  -> "DAY"           quoted identifier (use for reserved words)
            {x!r}  -> verbatim        raw SQL fragment - never with user input

        Return-contract guarantee:

            `load_rows` / `load_row` / `query_rows` / `query_one` NEVER raise on
            data-layer failure. On error they return a single-row envelope
            `[{"_error": "<message>"}]` (or that dict from `load_row`). Use the
            `has_error` helper below before iterating:

                rows = load_rows(server, metadata, __file__, "queries/x.sql")
                if has_error(rows):
                    return render_error_panel(rows[0]["_error"])
                # rows is safe to iterate
        """
        from pathlib import Path

        from dash import html

        from dash_server.exasol.runtime import query_one, query_rows, query_scalar
        from dash_server.exasol.runtime import has_error as _runtime_has_error


        def has_error(rows):
            """Detect the `[{"_error": "..."}]` envelope every helper returns on failure."""

            return _runtime_has_error(rows)


        def load_rows(server, metadata, current_file, sql_relative_path, *, params=None):
            return query_rows(
                server,
                metadata,
                base_dir=str(Path(current_file).resolve().parent),
                sql_relative_path=sql_relative_path,
                params=params or {},
            )


        def load_row(server, metadata, current_file, sql_relative_path, *, params=None):
            return query_one(
                server,
                metadata,
                base_dir=str(Path(current_file).resolve().parent),
                sql_relative_path=sql_relative_path,
                params=params or {},
            )


        def load_scalar(server, metadata, current_file, sql_relative_path, *, column=None, params=None):
            return query_scalar(
                server,
                metadata,
                base_dir=str(Path(current_file).resolve().parent),
                sql_relative_path=sql_relative_path,
                params=params or {},
                column=column,
            )


        def render_error_panel(message):
            return html.Div(
                [
                    html.Strong("Exasol query failed"),
                    html.Pre(message or "Unknown Exasol error"),
                ],
                style={"backgroundColor": "#fff7ed", "border": "1px solid #fdba74", "padding": "1rem"},
            )


        def render_table(rows):
            if not rows:
                return html.Div("No rows returned.", style={"color": "#94a3b8"})
            if rows and rows[0].get("_error"):
                return render_error_panel(rows[0]["_error"])

            columns = list(rows[0].keys())
            return html.Table(
                [
                    html.Thead(html.Tr([html.Th(column) for column in columns])),
                    html.Tbody(
                        [
                            html.Tr([html.Td(str(row.get(column, ""))) for column in columns])
                            for row in rows
                        ]
                    ),
                ],
                style={"borderCollapse": "collapse", "width": "100%"},
            )
        '''
    ).strip() + "\n"


def _render_app_py(title: str, pattern: str) -> str:
    if pattern == "analytics-hub":
        return _render_analytics_hub_app_py(title)
    if pattern == "overview":
        return _render_overview_app_py(title)
    if pattern == "kpi-trend":
        return _render_kpi_trend_app_py(title)
    return _render_ops_monitor_app_py(title)


def _render_analytics_hub_app_py(title: str, business_context: dict[str, str] | None = None) -> str:
    context = business_context or {
        "caption": "Replace the business SQL files with schema-specific queries after discovery.",
        "summary_heading": "Business KPI Snapshot",
        "chart_heading": "Business Trend",
        "table_heading": "Business Detail",
    }
    return dedent(
        f"""
        import importlib.util
        from collections import Counter
        from datetime import datetime, timezone
        from pathlib import Path

        import plotly.graph_objects as go
        from dash import Dash, Input, Output, dcc, html


        _HELPER_SPEC = importlib.util.spec_from_file_location(
            "dash_server_generated_exasol_helper",
            Path(__file__).with_name("dash_server_exasol.py"),
        )
        assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
        _HELPER_MODULE = importlib.util.module_from_spec(_HELPER_SPEC)
        _HELPER_SPEC.loader.exec_module(_HELPER_MODULE)
        load_row = _HELPER_MODULE.load_row
        load_rows = _HELPER_MODULE.load_rows
        render_error_panel = _HELPER_MODULE.render_error_panel
        render_table = _HELPER_MODULE.render_table

        BUSINESS_CAPTION = {context["caption"]!r}
        BUSINESS_SUMMARY_HEADING = {context["summary_heading"]!r}
        BUSINESS_CHART_HEADING = {context["chart_heading"]!r}
        BUSINESS_TABLE_HEADING = {context["table_heading"]!r}


        def _metric_cards(row):
            cards = []
            for label, value in (row or {{}}).items():
                cards.append(
                    html.Div(
                        [
                            html.Div(label, style={{"fontSize": "12px", "color": "#64748b"}}),
                            html.Strong(str(value), style={{"fontSize": "28px"}}),
                        ],
                        style={{"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"}},
                    )
                )
            return cards


        def _empty_figure(message):
            figure = go.Figure()
            figure.add_annotation(text=message, showarrow=False)
            figure.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            return figure


        def create_dash_app(server, url_base_pathname, metadata):
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=url_base_pathname.rstrip("/") + "/",
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    dcc.Interval(id="refresh", interval=15000, n_intervals=0),
                    html.H1(metadata.get("title", {title!r})),
                    html.P(metadata.get("description", "Live Exasol analytics workspace.")),
                    dcc.Tabs(
                        [
                            dcc.Tab(
                                label="System Health",
                                children=[
                                    html.Div(id="health-caption", style={{"color": "#64748b", "marginBottom": "1rem"}}),
                                    html.Div(
                                        id="health-summary",
                                        style={{
                                            "display": "grid",
                                            "gridTemplateColumns": "repeat(4, minmax(0, 1fr))",
                                            "gap": "1rem",
                                        }},
                                    ),
                                    html.Div(
                                        [
                                            dcc.Graph(id="monitor-chart", style={{"flex": "1"}}),
                                            dcc.Graph(id="usage-chart", style={{"flex": "1"}}),
                                        ],
                                        style={{"display": "flex", "gap": "1rem", "marginTop": "1rem"}},
                                    ),
                                ],
                            ),
                            dcc.Tab(
                                label="Query History",
                                children=[
                                    html.P(
                                        "Recent activity from EXA_SQL_LAST_DAY for live review before promotion.",
                                        style={{"color": "#64748b", "marginTop": "1rem"}},
                                    ),
                                    html.Div(id="sql-history-table"),
                                ],
                            ),
                            dcc.Tab(
                                label="Business Analytics",
                                children=[
                                    html.P(BUSINESS_CAPTION, id="business-caption", style={{"color": "#64748b", "marginTop": "1rem"}}),
                                    html.H3(BUSINESS_SUMMARY_HEADING),
                                    html.Div(
                                        id="business-summary",
                                        style={{
                                            "display": "grid",
                                            "gridTemplateColumns": "repeat(4, minmax(0, 1fr))",
                                            "gap": "1rem",
                                        }},
                                    ),
                                    html.H3(BUSINESS_CHART_HEADING, style={{"marginTop": "1rem"}}),
                                    dcc.Graph(id="business-chart"),
                                    html.H3(BUSINESS_TABLE_HEADING, style={{"marginTop": "1rem"}}),
                                    html.Div(id="business-table"),
                                ],
                            ),
                        ]
                    ),
                ],
                style={{"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "1200px"}},
            )

            @app.callback(
                Output("health-caption", "children"),
                Output("health-summary", "children"),
                Output("monitor-chart", "figure"),
                Output("usage-chart", "figure"),
                Output("sql-history-table", "children"),
                Output("business-summary", "children"),
                Output("business-chart", "figure"),
                Output("business-table", "children"),
                Input("refresh", "n_intervals"),
            )
            def refresh_dashboard(_n_intervals):
                meta_rows = load_rows(server, metadata, __file__, "queries/system/meta.sql")
                monitor_rows = load_rows(server, metadata, __file__, "queries/system/monitor.sql")
                usage_rows = load_rows(server, metadata, __file__, "queries/system/usage.sql")
                sql_rows = load_rows(server, metadata, __file__, "queries/system/sql_hist.sql")
                business_summary = load_row(server, metadata, __file__, "queries/business/summary.sql")
                business_trend = load_rows(server, metadata, __file__, "queries/business/trend.sql")
                business_detail = load_rows(server, metadata, __file__, "queries/business/detail.sql")

                if meta_rows and "_error" in meta_rows[0]:
                    error = meta_rows[0]["_error"]
                    return (
                        error,
                        render_error_panel(error),
                        _empty_figure(error),
                        _empty_figure(error),
                        render_error_panel(error),
                        render_error_panel(error),
                        _empty_figure(error),
                        render_error_panel(error),
                    )

                version = next((row.get("PARAM_VALUE") for row in meta_rows if row.get("PARAM_NAME") == "databaseProductVersion"), "?")
                caption = (
                    f"Exasol system view · version {{version}} · "
                    f"{{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}}"
                )
                health_cards = _metric_cards(
                    {{
                        "Version": version,
                        "Monitor Samples": len([row for row in monitor_rows if "_error" not in row]),
                        "Usage Samples": len([row for row in usage_rows if "_error" not in row]),
                        "SQL History Rows": len([row for row in sql_rows if "_error" not in row]),
                    }}
                )

                monitor_figure = go.Figure()
                if monitor_rows and "_error" not in monitor_rows[0]:
                    monitor_figure.add_trace(
                        go.Scatter(
                            x=[row.get("T") for row in monitor_rows],
                            y=[row.get("CPU") for row in monitor_rows],
                            mode="lines",
                            name="CPU",
                        )
                    )
                    monitor_figure.add_trace(
                        go.Scatter(
                            x=[row.get("T") for row in monitor_rows],
                            y=[row.get("RAM") for row in monitor_rows],
                            mode="lines",
                            name="RAM",
                        )
                    )
                else:
                    monitor_figure = _empty_figure("No monitor data available.")

                usage_figure = go.Figure()
                if usage_rows and "_error" not in usage_rows[0]:
                    usage_figure.add_trace(
                        go.Bar(
                            x=[row.get("T") for row in usage_rows],
                            y=[row.get("QUERIES") for row in usage_rows],
                            name="Queries",
                        )
                    )
                    usage_figure.add_trace(
                        go.Scatter(
                            x=[row.get("T") for row in usage_rows],
                            y=[row.get("USERS") for row in usage_rows],
                            name="Users",
                            yaxis="y2",
                        )
                    )
                    usage_figure.update_layout(yaxis2={{"overlaying": "y", "side": "right"}})
                else:
                    usage_figure = _empty_figure("No usage data available.")

                business_figure = go.Figure()
                if business_trend and "_error" not in business_trend[0]:
                    business_figure.add_trace(
                        go.Scatter(
                            x=[row.get("LABEL") for row in business_trend],
                            y=[row.get("VALUE") for row in business_trend],
                            mode="lines+markers",
                            line={{"color": "#1E5EFF", "width": 3}},
                            name="Trend",
                        )
                    )
                elif business_trend and "_error" in business_trend[0]:
                    business_figure = _empty_figure(
                        f"Exasol query failed for queries/business/trend.sql: {{business_trend[0]['_error']}}"
                    )
                else:
                    business_figure = _empty_figure(
                        "queries/business/trend.sql returned no rows. Replace with a domain query that produces LABEL,VALUE rows."
                    )

                if sql_rows and "_error" not in sql_rows[0]:
                    counts = Counter(row.get("COMMAND_CLASS", "Unknown") or "Unknown" for row in sql_rows)
                    if counts:
                        usage_figure.add_trace(
                            go.Pie(
                                labels=list(counts.keys()),
                                values=list(counts.values()),
                                hole=0.55,
                                domain={{"x": [0.72, 1.0], "y": [0.52, 1.0]}},
                                name="SQL Mix",
                            )
                        )

                if business_summary and "_error" in business_summary:
                    business_summary_cards = render_error_panel(business_summary["_error"])
                else:
                    business_summary_cards = _metric_cards(business_summary)

                return (
                    caption,
                    health_cards,
                    monitor_figure,
                    usage_figure,
                    render_table(sql_rows[:25]),
                    business_summary_cards,
                    business_figure,
                    render_table(business_detail[:25]),
                )

            return app
        """
    ).strip() + "\n"


def _render_overview_app_py(title: str) -> str:
    return dedent(
        f"""
        import importlib.util
        from pathlib import Path

        from dash import Dash, Input, Output, dcc, html


        _HELPER_SPEC = importlib.util.spec_from_file_location(
            "dash_server_generated_exasol_helper",
            Path(__file__).with_name("dash_server_exasol.py"),
        )
        assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
        _HELPER_MODULE = importlib.util.module_from_spec(_HELPER_SPEC)
        _HELPER_SPEC.loader.exec_module(_HELPER_MODULE)
        load_row = _HELPER_MODULE.load_row
        load_rows = _HELPER_MODULE.load_rows
        render_error_panel = _HELPER_MODULE.render_error_panel
        render_table = _HELPER_MODULE.render_table


        def create_dash_app(server, url_base_pathname, metadata):
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=url_base_pathname.rstrip("/") + "/",
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1(metadata.get("title", {title!r})),
                    html.P(metadata.get("description", "Live Exasol dashboard.")),
                    html.Div(
                        [
                            html.Button("Refresh", id="refresh", n_clicks=0),
                            dcc.Interval(id="initial-load", interval=250, max_intervals=1, n_intervals=0),
                        ],
                        style={{"display": "flex", "gap": "0.75rem", "alignItems": "center"}},
                    ),
                    html.Div(id="summary-row", style={{"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "1rem", "marginTop": "1rem"}}),
                    html.Div(id="detail-table", style={{"marginTop": "1rem"}}),
                ],
                style={{"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "960px"}},
            )

            @app.callback(
                Output("summary-row", "children"),
                Output("detail-table", "children"),
                Input("refresh", "n_clicks"),
                Input("initial-load", "n_intervals"),
            )
            def refresh_dashboard(_n_clicks, _n_intervals):
                summary = load_row(server, metadata, __file__, "queries/summary.sql")
                detail = load_rows(server, metadata, __file__, "queries/detail.sql")
                if summary and "_error" in summary:
                    return render_error_panel(summary["_error"]), render_error_panel(summary["_error"])
                cards = []
                for label, value in (summary or {{}}).items():
                    cards.append(
                        html.Div(
                            [html.Div(label, style={{"fontSize": "12px", "color": "#64748b"}}), html.Strong(str(value), style={{"fontSize": "28px"}})],
                            style={{"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"}},
                        )
                    )
                return cards, render_table(detail)

            return app
        """
    ).strip() + "\n"


def _render_kpi_trend_app_py(title: str) -> str:
    return dedent(
        f"""
        import importlib.util
        from pathlib import Path

        import plotly.graph_objects as go
        from dash import Dash, Input, Output, dcc, html


        _HELPER_SPEC = importlib.util.spec_from_file_location(
            "dash_server_generated_exasol_helper",
            Path(__file__).with_name("dash_server_exasol.py"),
        )
        assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
        _HELPER_MODULE = importlib.util.module_from_spec(_HELPER_SPEC)
        _HELPER_SPEC.loader.exec_module(_HELPER_MODULE)
        load_row = _HELPER_MODULE.load_row
        load_rows = _HELPER_MODULE.load_rows
        render_error_panel = _HELPER_MODULE.render_error_panel
        render_table = _HELPER_MODULE.render_table


        def create_dash_app(server, url_base_pathname, metadata):
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=url_base_pathname.rstrip("/") + "/",
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    html.H1(metadata.get("title", {title!r})),
                    html.P(metadata.get("description", "Live Exasol dashboard.")),
                    html.Div(
                        [
                            html.Button("Refresh", id="refresh", n_clicks=0),
                            dcc.Interval(id="initial-load", interval=250, max_intervals=1, n_intervals=0),
                        ],
                        style={{"display": "flex", "gap": "0.75rem", "alignItems": "center"}},
                    ),
                    html.Div(id="summary-row", style={{"display": "grid", "gridTemplateColumns": "repeat(4, 1fr)", "gap": "1rem", "marginTop": "1rem"}}),
                    dcc.Graph(id="trend-chart", style={{"marginTop": "1rem"}}),
                    html.Div(id="detail-table", style={{"marginTop": "1rem"}}),
                ],
                style={{"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "1100px"}},
            )

            @app.callback(
                Output("summary-row", "children"),
                Output("trend-chart", "figure"),
                Output("detail-table", "children"),
                Input("refresh", "n_clicks"),
                Input("initial-load", "n_intervals"),
            )
            def refresh_dashboard(_n_clicks, _n_intervals):
                summary = load_row(server, metadata, __file__, "queries/summary.sql")
                trend = load_rows(server, metadata, __file__, "queries/trend.sql")
                detail = load_rows(server, metadata, __file__, "queries/detail.sql")
                if summary and "_error" in summary:
                    return render_error_panel(summary["_error"]), go.Figure(), render_error_panel(summary["_error"])

                cards = []
                for label, value in (summary or {{}}).items():
                    cards.append(
                        html.Div(
                            [html.Div(label, style={{"fontSize": "12px", "color": "#64748b"}}), html.Strong(str(value), style={{"fontSize": "28px"}})],
                            style={{"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"}},
                        )
                    )

                figure = go.Figure()
                if trend and "_error" not in trend[0]:
                    figure.add_trace(
                        go.Scatter(
                            x=[row["LABEL"] for row in trend],
                            y=[row["VALUE"] for row in trend],
                            mode="lines+markers",
                            line={{"color": "#1E5EFF", "width": 3}},
                        )
                    )
                else:
                    figure.add_annotation(text="No trend data available.", showarrow=False)
                figure.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin={{"l": 30, "r": 10, "t": 20, "b": 30}},
                )
                return cards, figure, render_table(detail)

            return app
        """
    ).strip() + "\n"


def _render_ops_monitor_app_py(title: str) -> str:
    return dedent(
        f"""
        import importlib.util
        from collections import Counter
        from datetime import datetime, timezone
        from pathlib import Path

        import plotly.graph_objects as go
        from dash import Dash, Input, Output, dcc, html


        _HELPER_SPEC = importlib.util.spec_from_file_location(
            "dash_server_generated_exasol_helper",
            Path(__file__).with_name("dash_server_exasol.py"),
        )
        assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
        _HELPER_MODULE = importlib.util.module_from_spec(_HELPER_SPEC)
        _HELPER_SPEC.loader.exec_module(_HELPER_MODULE)
        load_rows = _HELPER_MODULE.load_rows
        render_error_panel = _HELPER_MODULE.render_error_panel
        render_table = _HELPER_MODULE.render_table

        REFRESH = 10000


        def create_dash_app(server, url_base_pathname, metadata):
            app = Dash(
                __name__,
                server=server,
                routes_pathname_prefix="/",
                requests_pathname_prefix=url_base_pathname.rstrip("/") + "/",
                title=metadata.get("title", {title!r}),
            )
            app.layout = html.Div(
                [
                    dcc.Interval(id="tick", interval=REFRESH, n_intervals=0),
                    html.H1(metadata.get("title", {title!r})),
                    html.Div(id="subhead", style={{"color": "#64748b", "marginBottom": "1rem"}}),
                    html.Div(id="summary-row", style={{"display": "grid", "gridTemplateColumns": "repeat(4, 1fr)", "gap": "1rem"}}),
                    html.Div(
                        [
                            dcc.Graph(id="monitor-chart", style={{"flex": "1"}}),
                            dcc.Graph(id="usage-chart", style={{"flex": "1"}}),
                        ],
                        style={{"display": "flex", "gap": "1rem", "marginTop": "1rem"}},
                    ),
                    html.Div(id="sql-table", style={{"marginTop": "1rem"}}),
                    html.Div(id="sessions-table", style={{"marginTop": "1rem"}}),
                ],
                style={{"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "1200px"}},
            )

            @app.callback(
                Output("subhead", "children"),
                Output("summary-row", "children"),
                Output("monitor-chart", "figure"),
                Output("usage-chart", "figure"),
                Output("sql-table", "children"),
                Output("sessions-table", "children"),
                Input("tick", "n_intervals"),
            )
            def refresh_dashboard(_):
                meta_rows = load_rows(server, metadata, __file__, "queries/meta.sql")
                sessions_rows = load_rows(server, metadata, __file__, "queries/sessions.sql")
                monitor_rows = load_rows(server, metadata, __file__, "queries/monitor.sql")
                usage_rows = load_rows(server, metadata, __file__, "queries/usage.sql")
                sql_rows = load_rows(server, metadata, __file__, "queries/sql_hist.sql")

                if meta_rows and "_error" in meta_rows[0]:
                    error = meta_rows[0]["_error"]
                    return (
                        error,
                        render_error_panel(error),
                        go.Figure(),
                        go.Figure(),
                        render_error_panel(error),
                        render_error_panel(error),
                    )

                version = next((row["PARAM_VALUE"] for row in meta_rows if row.get("PARAM_NAME") == "databaseProductVersion"), "?")
                cards = []
                for label, value in [
                    ("Version", version),
                    ("Active Sessions", len([row for row in sessions_rows if "_error" not in row])),
                    ("Monitor Samples", len([row for row in monitor_rows if "_error" not in row])),
                    ("Recent SQL Rows", len([row for row in sql_rows if "_error" not in row])),
                ]:
                    cards.append(
                        html.Div(
                            [html.Div(label, style={{"fontSize": "12px", "color": "#64748b"}}), html.Strong(str(value), style={{"fontSize": "28px"}})],
                            style={{"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"}},
                        )
                    )

                monitor_figure = go.Figure()
                if monitor_rows and "_error" not in monitor_rows[0]:
                    monitor_figure.add_trace(go.Scatter(x=[row.get("T") for row in monitor_rows], y=[row.get("CPU") for row in monitor_rows], name="CPU"))
                    monitor_figure.add_trace(go.Scatter(x=[row.get("T") for row in monitor_rows], y=[row.get("LOAD") for row in monitor_rows], name="Load"))
                    monitor_figure.add_trace(go.Scatter(x=[row.get("T") for row in monitor_rows], y=[row.get("RAM") for row in monitor_rows], name="RAM"))
                else:
                    monitor_figure.add_annotation(text="No monitor data available.", showarrow=False)

                usage_figure = go.Figure()
                if usage_rows and "_error" not in usage_rows[0]:
                    usage_figure.add_trace(go.Bar(x=[row.get("T") for row in usage_rows], y=[row.get("QUERIES") for row in usage_rows], name="Queries"))
                    usage_figure.add_trace(go.Scatter(x=[row.get("T") for row in usage_rows], y=[row.get("USERS") for row in usage_rows], name="Users", yaxis="y2"))
                    if sql_rows and "_error" not in sql_rows[0]:
                        counts = Counter(row.get("COMMAND_CLASS", "Unknown") or "Unknown" for row in sql_rows)
                        usage_figure.add_trace(
                            go.Pie(
                                labels=list(counts.keys()),
                                values=list(counts.values()),
                                hole=0.5,
                                domain={{"x": [0.72, 1.0], "y": [0.52, 1.0]}},
                                name="SQL Mix",
                            )
                        )
                    usage_figure.update_layout(yaxis2={{"overlaying": "y", "side": "right"}})
                else:
                    usage_figure.add_annotation(text="No usage data available.", showarrow=False)

                return (
                    f"Live Exasol ops view · EXASolution {{version}} · {{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}}",
                    cards,
                    monitor_figure,
                    usage_figure,
                    render_table(sql_rows[:20]),
                    render_table(sessions_rows[:20]),
                )

            return app
        """
    ).strip() + "\n"


def _overview_summary_sql() -> str:
    return (
        "SELECT CURRENT_DATE AS SNAPSHOT_DATE,\n"
        "       CURRENT_USER AS CURRENT_USER,\n"
        "       CURRENT_SCHEMA AS CURRENT_SCHEMA\n"
        "FROM DUAL\n"
    )


def _overview_detail_sql() -> str:
    return (
        "SELECT 'Profile binding' AS ITEM, CURRENT_USER AS \"VALUE\" FROM DUAL\n"
        "UNION ALL\n"
        "SELECT 'Today' AS ITEM, TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD') AS \"VALUE\" FROM DUAL\n"
        "UNION ALL\n"
        "SELECT 'Schema' AS ITEM, CURRENT_SCHEMA AS \"VALUE\" FROM DUAL\n"
    )


def _kpi_summary_sql() -> str:
    return (
        "SELECT 184 AS TICKETS,\n"
        "       98.2 AS SLA_PERCENT,\n"
        "       27 AS OPEN_ESCALATIONS,\n"
        "       12.4 AS AVG_RESPONSE_HOURS\n"
        "FROM DUAL\n"
    )


def _kpi_trend_sql() -> str:
    return (
        "SELECT 'Mon' AS LABEL, 120 AS \"VALUE\" FROM DUAL\n"
        "UNION ALL SELECT 'Tue', 138 FROM DUAL\n"
        "UNION ALL SELECT 'Wed', 142 FROM DUAL\n"
        "UNION ALL SELECT 'Thu', 151 FROM DUAL\n"
        "UNION ALL SELECT 'Fri', 166 FROM DUAL\n"
    )


def _kpi_detail_sql() -> str:
    return (
        "SELECT 'North' AS SEGMENT, 46 AS TICKETS, 98.8 AS SLA_PERCENT FROM DUAL\n"
        "UNION ALL SELECT 'South', 39, 97.2 FROM DUAL\n"
        "UNION ALL SELECT 'East', 52, 98.9 FROM DUAL\n"
        "UNION ALL SELECT 'West', 47, 97.8 FROM DUAL\n"
    )


def _placeholder_business_summary_sql() -> str:
    return (
        "-- Replace this with a summary query from the target business schema.\n"
        "SELECT 1240 AS ACTIVE_CUSTOMERS,\n"
        "       18.7 AS AVG_ORDER_VALUE,\n"
        "       92.4 AS FULFILLMENT_RATE,\n"
        "       14 AS OPEN_EXCEPTIONS\n"
        "FROM DUAL\n"
    )


def _placeholder_business_trend_sql() -> str:
    return (
        "-- Replace this with a time-series query from the target business schema.\n"
        "SELECT 'Mon' AS LABEL, 84 AS \"VALUE\" FROM DUAL\n"
        "UNION ALL SELECT 'Tue', 88 FROM DUAL\n"
        "UNION ALL SELECT 'Wed', 93 FROM DUAL\n"
        "UNION ALL SELECT 'Thu', 97 FROM DUAL\n"
        "UNION ALL SELECT 'Fri', 101 FROM DUAL\n"
    )


def _placeholder_business_detail_sql() -> str:
    return (
        "-- Replace this with a detail table query from the target business schema.\n"
        "SELECT 'North' AS SEGMENT, 410 AS ORDERS, 18.1 AS AVG_ORDER_VALUE FROM DUAL\n"
        "UNION ALL SELECT 'South', 355, 17.9 FROM DUAL\n"
        "UNION ALL SELECT 'East', 289, 19.2 FROM DUAL\n"
        "UNION ALL SELECT 'West', 186, 20.1 FROM DUAL\n"
    )


def _ops_meta_sql() -> str:
    return (
        "SELECT \"PARAM_NAME\", \"PARAM_VALUE\"\n"
        "FROM EXA_METADATA\n"
        "WHERE \"PARAM_NAME\" IN ('databaseProductVersion', 'databaseProductName', 'maxConnections')\n"
    )


def _ops_sessions_sql() -> str:
    return (
        "SELECT \"SESSION_ID\",\n"
        "       \"USER_NAME\",\n"
        "       \"STATUS\",\n"
        "       \"COMMAND_NAME\",\n"
        "       \"CLIENT\",\n"
        "       \"TEMP_DB_RAM\",\n"
        "       \"LOGIN_TIME\",\n"
        "       \"CLUSTER_NAME\"\n"
        "FROM EXA_ALL_SESSIONS\n"
        "ORDER BY \"LOGIN_TIME\" DESC\n"
    )


def _ops_monitor_sql() -> str:
    return (
        "SELECT TO_CHAR(\"MEASURE_TIME\", 'HH24:MI') AS \"T\",\n"
        "       CAST(\"CPU\" AS DOUBLE) AS \"CPU\",\n"
        "       CAST(\"LOAD\" AS DOUBLE) AS \"LOAD\",\n"
        "       CAST(\"TEMP_DB_RAM\" AS DOUBLE) AS \"RAM\"\n"
        "FROM EXA_STATISTICS.EXA_MONITOR_LAST_DAY\n"
        "WHERE \"MEASURE_TIME\" >= ADD_HOURS(NOW(), -8)\n"
        "ORDER BY \"MEASURE_TIME\"\n"
    )


def _ops_usage_sql() -> str:
    return (
        "SELECT TO_CHAR(\"MEASURE_TIME\", 'HH24:MI') AS \"T\",\n"
        "       CAST(\"USERS\" AS INTEGER) AS \"USERS\",\n"
        "       CAST(\"QUERIES\" AS INTEGER) AS \"QUERIES\"\n"
        "FROM EXA_STATISTICS.EXA_USAGE_LAST_DAY\n"
        "WHERE \"MEASURE_TIME\" >= ADD_HOURS(NOW(), -8)\n"
        "ORDER BY \"MEASURE_TIME\"\n"
    )


def _ops_sql_hist_sql() -> str:
    return (
        "SELECT TO_CHAR(\"START_TIME\", 'HH24:MI:SS') AS \"START_TIME\",\n"
        "       \"COMMAND_NAME\",\n"
        "       \"COMMAND_CLASS\",\n"
        "       CAST(\"DURATION\" AS DOUBLE) AS \"DURATION\",\n"
        "       CAST(\"CPU\" AS DOUBLE) AS \"CPU\",\n"
        "       \"SUCCESS\",\n"
        "       \"ROW_COUNT\"\n"
        "FROM EXA_STATISTICS.EXA_SQL_LAST_DAY\n"
        "ORDER BY \"START_TIME\" DESC\n"
    )


def _schema_notes_md(blueprint: dict[str, Any]) -> str:
    measures = ", ".join(blueprint.get("measure_columns") or []) or "none"
    relationships = blueprint.get("relationship_hints") or []
    lines = [
        f"# Schema Scaffold Notes for {blueprint['schema_name']}.{blueprint['table_name']}",
        "",
        f"- Summary heading: `{blueprint['summary_heading']}`",
        f"- Chart heading: `{blueprint['chart_heading']}`",
        f"- Table heading: `{blueprint['table_heading']}`",
        f"- Time column: `{blueprint.get('time_column') or 'none'}`",
        f"- Dimension column: `{blueprint.get('dimension_column') or 'none'}`",
        f"- Measure columns: `{measures}`",
    ]
    if relationships:
        lines.append("- Relationship hints:")
        for relationship in relationships:
            lines.append(
                f"  - `{relationship['column_name']}` also appears in `{relationship['other_schema']}.{relationship['other_table']}`"
            )
    return "\n".join(lines) + "\n"


def _schema_summary_sql(blueprint: dict[str, Any]) -> str:
    table_ref = _qualified_table_name(blueprint["schema_name"], blueprint["table_name"])
    measure = blueprint.get("primary_measure")
    time_column = blueprint.get("time_column")
    parts = ['COUNT(*) AS "ROW_COUNT"']
    if measure:
        measure_ref = _quoted_identifier(measure)
        parts.extend(
            [
                f'CAST(SUM({measure_ref}) AS DOUBLE) AS "TOTAL_{measure.upper()}"',
                f'CAST(AVG({measure_ref}) AS DOUBLE) AS "AVG_{measure.upper()}"',
            ]
        )
    # BUG-007: when the table has both a quantity-like and a price-like column,
    # propose their product as a derived REVENUE measure. The scaffold's purpose is
    # to give a useful starter SQL, and on retail/ops schemas the (QTY × NET_PRICE)
    # shape is overwhelmingly the metric an exec dashboard wants.
    derived = _find_derived_revenue_pair(blueprint.get("measure_columns") or [])
    if derived is not None:
        qty_ref = _quoted_identifier(derived[0])
        price_ref = _quoted_identifier(derived[1])
        parts.append(f'CAST(SUM({qty_ref} * {price_ref}) AS DOUBLE) AS "REVENUE"')
    if time_column:
        time_ref = _quoted_identifier(time_column)
        parts.extend(
            [
                f'MIN({time_ref}) AS "FIRST_{time_column.upper()}"',
                f'MAX({time_ref}) AS "LAST_{time_column.upper()}"',
            ]
        )
    return "SELECT " + ",\n       ".join(parts) + f"\nFROM {table_ref}\n"


def _schema_trend_sql(blueprint: dict[str, Any]) -> str:
    table_ref = _qualified_table_name(blueprint["schema_name"], blueprint["table_name"])
    measure = blueprint.get("primary_measure")
    time_column = blueprint.get("time_column")
    if measure and time_column:
        measure_ref = _quoted_identifier(measure)
        time_ref = _quoted_identifier(time_column)
        return (
            "SELECT TO_CHAR(CAST("
            + time_ref
            + " AS DATE), 'YYYY-MM-DD') AS \"LABEL\",\n"
            + f"       CAST(SUM({measure_ref}) AS DOUBLE) AS \"VALUE\"\n"
            + f"FROM {table_ref}\n"
            + f"WHERE {time_ref} IS NOT NULL\n"
            + "GROUP BY 1\n"
            + "ORDER BY 1 DESC\n"
            + "LIMIT 30\n"
        )
    # BUG-007: when the picked table has no time column of its own but the
    # `_discover_schema_blueprint` step recorded an FK-ish hint into a foreign table that
    # *does* have one (e.g. `ORDER_LINES.ORDER_ID → ORDERS.ORDER_DATE`), emit a join so
    # the trend chart has real dates on its axis instead of falling back to a dimension
    # or a placeholder.
    if measure:
        joined = _find_time_via_relationship(blueprint)
        if joined is not None:
            measure_ref = _quoted_identifier(measure)
            join_table_ref = _qualified_table_name(joined["other_schema"], joined["other_table"])
            time_ref = _quoted_identifier(joined["other_time_column"])
            local_key = _quoted_identifier(joined["column_name"])
            foreign_key = _quoted_identifier(joined["other_key_column"])
            return (
                f"SELECT TO_CHAR(CAST(j.{time_ref} AS DATE), 'YYYY-MM-DD') AS \"LABEL\",\n"
                f"       CAST(SUM(t.{measure_ref}) AS DOUBLE) AS \"VALUE\"\n"
                f"FROM {table_ref} t\n"
                f"JOIN {join_table_ref} j ON t.{local_key} = j.{foreign_key}\n"
                f"WHERE j.{time_ref} IS NOT NULL\n"
                "GROUP BY 1\n"
                "ORDER BY 1 DESC\n"
                "LIMIT 30\n"
            )
    dimension = blueprint.get("dimension_column")
    if measure and dimension:
        measure_ref = _quoted_identifier(measure)
        dimension_ref = _quoted_identifier(dimension)
        return (
            f"SELECT CAST({dimension_ref} AS VARCHAR(128)) AS \"LABEL\",\n"
            f"       CAST(SUM({measure_ref}) AS DOUBLE) AS \"VALUE\"\n"
            f"FROM {table_ref}\n"
            f"WHERE {dimension_ref} IS NOT NULL\n"
            "GROUP BY 1\n"
            "ORDER BY 2 DESC\n"
            "LIMIT 15\n"
        )
    return (
        "SELECT 'Populate business trend SQL' AS \"LABEL\", 1 AS \"VALUE\"\n"
        "FROM DUAL\n"
    )


def _schema_detail_sql(blueprint: dict[str, Any]) -> str:
    table_ref = _qualified_table_name(blueprint["schema_name"], blueprint["table_name"])
    select_columns: list[str] = []
    for column_name in [
        blueprint.get("dimension_column"),
        blueprint.get("time_column"),
        *(blueprint.get("measure_columns") or [])[:3],
    ]:
        if column_name and column_name not in select_columns:
            select_columns.append(column_name)
    if not select_columns:
        return (
            "SELECT 'Populate business detail SQL' AS \"DETAIL_NOTE\"\n"
            "FROM DUAL\n"
        )
    rendered_columns = ",\n       ".join(_quoted_identifier(column_name) for column_name in select_columns)
    order_column = blueprint.get("time_column") or select_columns[0]
    return (
        "SELECT "
        + rendered_columns
        + f"\nFROM {table_ref}\nORDER BY {_quoted_identifier(order_column)} DESC\nLIMIT 25\n"
    )


def _qualified_table_name(schema_name: str, table_name: str) -> str:
    return f"{_quoted_identifier(schema_name)}.{_quoted_identifier(table_name)}"


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _find_time_via_relationship(blueprint: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first relationship hint whose foreign table has a time column.

    Used by `_schema_trend_sql` to emit a JOIN when the picked table has no date of
    its own but a related table does — e.g. `ORDER_LINES` joining to `ORDERS.ORDER_DATE`.
    Returns None when no relationship hint exposes a usable time column.
    """

    for hint in blueprint.get("relationship_hints") or []:
        if not isinstance(hint, dict):
            continue
        if hint.get("other_time_column") and hint.get("column_name") and hint.get("other_key_column"):
            return hint
    return None


# Recognized column-name shapes for the (quantity × price) derived-revenue heuristic.
# Match against the upper-cased column name with substring tests so we catch
# `QUANTITY`, `NET_QUANTITY`, `LINE_QTY`, etc.
_QUANTITY_TOKENS = ("QUANTITY", "QTY", "UNITS")
_PRICE_TOKENS = ("PRICE", "AMOUNT", "RATE", "FEE", "REVENUE", "COST")


def _find_derived_revenue_pair(measure_columns: list[str]) -> tuple[str, str] | None:
    """Look for a (quantity-like, price-like) pair among the table's measure columns.

    Returns ``(quantity_column, price_column)`` when one is found, otherwise None.
    Avoids the same column appearing on both sides (a single `NET_AMOUNT` column
    isn't a derived measure — it's already the answer).
    """

    if not measure_columns:
        return None
    qty_col: str | None = None
    price_col: str | None = None
    for column in measure_columns:
        upper = column.upper()
        if qty_col is None and any(token in upper for token in _QUANTITY_TOKENS):
            qty_col = column
            continue
        if price_col is None and any(token in upper for token in _PRICE_TOKENS):
            price_col = column
    if qty_col and price_col and qty_col != price_col:
        return qty_col, price_col
    return None
