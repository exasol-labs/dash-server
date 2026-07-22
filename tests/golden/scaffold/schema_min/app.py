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

BUSINESS_CAPTION = 'Minimal scaffold; no measures/time/dimension discovered.'
BUSINESS_SUMMARY_HEADING = 'Events KPI Snapshot'
BUSINESS_CHART_HEADING = 'Events Trend'
BUSINESS_TABLE_HEADING = 'Events Detail'


def _metric_cards(row):
    cards = []
    for label, value in (row or {}).items():
        cards.append(
            html.Div(
                [
                    html.Div(label, style={"fontSize": "12px", "color": "#64748b"}),
                    html.Strong(str(value), style={"fontSize": "28px"}),
                ],
                style={"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"},
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
        title=metadata.get("title", 'Golden schema_min'),
    )
    app.layout = html.Div(
        [
            dcc.Interval(id="refresh", interval=15000, n_intervals=0),
            html.H1(metadata.get("title", 'Golden schema_min')),
            html.P(metadata.get("description", "Live Exasol analytics workspace.")),
            dcc.Tabs(
                [
                    dcc.Tab(
                        label="System Health",
                        children=[
                            html.Div(id="health-caption", style={"color": "#64748b", "marginBottom": "1rem"}),
                            html.Div(
                                id="health-summary",
                                style={
                                    "display": "grid",
                                    "gridTemplateColumns": "repeat(4, minmax(0, 1fr))",
                                    "gap": "1rem",
                                },
                            ),
                            html.Div(
                                [
                                    dcc.Graph(id="monitor-chart", style={"flex": "1"}),
                                    dcc.Graph(id="usage-chart", style={"flex": "1"}),
                                ],
                                style={"display": "flex", "gap": "1rem", "marginTop": "1rem"},
                            ),
                        ],
                    ),
                    dcc.Tab(
                        label="Query History",
                        children=[
                            html.P(
                                "Recent activity from EXA_SQL_LAST_DAY for live review before promotion.",
                                style={"color": "#64748b", "marginTop": "1rem"},
                            ),
                            html.Div(id="sql-history-table"),
                        ],
                    ),
                    dcc.Tab(
                        label="Business Analytics",
                        children=[
                            html.P(BUSINESS_CAPTION, id="business-caption", style={"color": "#64748b", "marginTop": "1rem"}),
                            html.H3(BUSINESS_SUMMARY_HEADING),
                            html.Div(
                                id="business-summary",
                                style={
                                    "display": "grid",
                                    "gridTemplateColumns": "repeat(4, minmax(0, 1fr))",
                                    "gap": "1rem",
                                },
                            ),
                            html.H3(BUSINESS_CHART_HEADING, style={"marginTop": "1rem"}),
                            dcc.Graph(id="business-chart"),
                            html.H3(BUSINESS_TABLE_HEADING, style={"marginTop": "1rem"}),
                            html.Div(id="business-table"),
                        ],
                    ),
                ]
            ),
        ],
        style={"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "1200px"},
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
            f"Exasol system view · version {version} · "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        health_cards = _metric_cards(
            {
                "Version": version,
                "Monitor Samples": len([row for row in monitor_rows if "_error" not in row]),
                "Usage Samples": len([row for row in usage_rows if "_error" not in row]),
                "SQL History Rows": len([row for row in sql_rows if "_error" not in row]),
            }
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
            usage_figure.update_layout(yaxis2={"overlaying": "y", "side": "right"})
        else:
            usage_figure = _empty_figure("No usage data available.")

        business_figure = go.Figure()
        if business_trend and "_error" not in business_trend[0]:
            business_figure.add_trace(
                go.Scatter(
                    x=[row.get("LABEL") for row in business_trend],
                    y=[row.get("VALUE") for row in business_trend],
                    mode="lines+markers",
                    line={"color": "#1E5EFF", "width": 3},
                    name="Trend",
                )
            )
        elif business_trend and "_error" in business_trend[0]:
            business_figure = _empty_figure(
                f"Exasol query failed for queries/business/trend.sql: {business_trend[0]['_error']}"
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
                        domain={"x": [0.72, 1.0], "y": [0.52, 1.0]},
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
