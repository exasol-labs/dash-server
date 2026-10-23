import importlib.util
from datetime import date, datetime
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
has_error = _HELPER_MODULE.has_error
render_error_panel = _HELPER_MODULE.render_error_panel

# The semantic model this app reads. Labels, formats, units, and certification are read from
# the model at runtime (queries/business/fields.sql), so a change made there shows up here
# without editing the app. Queries select the model's columns by their SQL names.
MODEL = 'sales'
OBJECT_REF = 'SEMANTIC_SALES.SALES'
TREND_X = 'ORDER_MONTH'
TREND_Y = 'TOTAL_REVENUE'
TREND_IS_TIME = True
DETAIL_DIMENSION = 'CUSTOMER_REGION'

_READINESS = {"OK": "VALID", "WARNING": "WARNING", "ERROR": "INVALID", "PRECONDITION": "PRECONDITION"}
_READINESS_COLORS = {
    "VALID": ("#ecfdf5", "#065f46"),
    "WARNING": ("#fffbeb", "#92400e"),
}
_ALERT_COLORS = ("#fef2f2", "#991b1b")


def _fields_by_column(rows):
    if has_error(rows):
        return {}
    return {row["SQL_COLUMN_NAME"]: row for row in rows if row.get("SQL_COLUMN_NAME")}


def _label(column, fields):
    field = fields.get(column) or {}
    return field.get("DISPLAY_NAME") or column


def _format_value(value, field):
    """Format by the model's FORMAT_HINT / UNIT_HINT. A percentage is a fraction (0.25 is 25 %)."""

    if value is None:
        return "-"
    field = field or {}
    hint = str(field.get("FORMAT_HINT") or "").lower()
    unit = field.get("UNIT_HINT")
    if isinstance(value, date):
        if hint == "year":
            return value.strftime("%Y")
        if hint == "month":
            return value.strftime("%Y-%m")
        if isinstance(value, datetime) and (value.hour, value.minute, value.second) != (0, 0, 0):
            return value.strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (str, bool)):
        return str(value)
    number = float(value)
    if hint in ("percentage", "percent"):
        return f"{number * 100:,.1f} %"
    if hint == "currency":
        text = f"{number:,.2f}"
    elif hint in ("count", "integer") or number.is_integer():
        text = f"{number:,.0f}"
    else:
        text = f"{number:,.2f}"
    return f"{text} {unit}" if unit else text


def _certified_mark(column, fields):
    return " (certified)" if (fields.get(column) or {}).get("IS_CERTIFIED") else ""


def _provenance_strip(row, fields):
    """Which model answered, and whether its last finished validation passed."""

    if not row or has_error(row):
        message = row["_error"] if row else f"Semantic model {MODEL} was not found."
        return html.Div(f"Model status unavailable: {message}", style={"color": _ALERT_COLORS[1]})
    readiness = _READINESS.get(str(row.get("VALIDATION_STATUS") or ""), "NOT VALIDATED")
    background, color = _READINESS_COLORS.get(readiness, _ALERT_COLORS)
    metrics = [field for field in fields.values() if field.get("FIELD_KIND") == "METRIC"]
    certified = sum(1 for field in metrics if field.get("IS_CERTIFIED"))
    parts = [
        f"Semantic model {row.get('MODEL_NAME')} v{row.get('VERSION_NUMBER')}",
        f"validation {readiness}"
        + (f" ({row.get('WARNING_COUNT')} warning(s))" if readiness == "WARNING" else ""),
        f"last validated {_format_value(row.get('FINISHED_AT'), {})}" if row.get("FINISHED_AT") else None,
        f"{certified} of {len(metrics)} metrics certified" if metrics else None,
    ]
    return html.Div(
        " · ".join(part for part in parts if part),
        style={"backgroundColor": background, "color": color, "padding": "0.5rem 1rem", "borderRadius": "8px"},
    )


def _metric_cards(row, fields):
    if row and has_error(row):
        return render_error_panel(row["_error"])
    cards = []
    for column, value in (row or {}).items():
        cards.append(
            html.Div(
                [
                    html.Div(
                        _label(column, fields) + _certified_mark(column, fields),
                        style={"fontSize": "12px", "color": "#64748b"},
                    ),
                    html.Strong(_format_value(value, fields.get(column)), style={"fontSize": "28px"}),
                ],
                style={"padding": "1rem", "border": "1px solid #e2e8f0", "borderRadius": "12px"},
            )
        )
    return cards


def _trend_figure(rows, fields):
    figure = go.Figure()
    if has_error(rows):
        figure.add_annotation(text=rows[0]["_error"], showarrow=False)
        return figure
    y_values = [row.get(TREND_Y) for row in rows]
    if TREND_X is None:
        x_values = [_label(TREND_Y, fields)]
    else:
        x_values = [row.get(TREND_X) for row in rows]
    y_field = fields.get(TREND_Y) or {}
    if TREND_IS_TIME:
        figure.add_trace(go.Scatter(x=x_values, y=y_values, mode="lines+markers", line={"color": "#1E5EFF", "width": 3}))
    else:
        figure.add_trace(go.Bar(x=x_values, y=y_values, marker_color="#1E5EFF"))
    tickformat = ".0%" if str(y_field.get("FORMAT_HINT") or "").lower() in ("percentage", "percent") else ",.0f"
    figure.update_layout(
        xaxis_title=_label(TREND_X, fields) if TREND_X else None,
        yaxis_title=_label(TREND_Y, fields) + (f" ({y_field['UNIT_HINT']})" if y_field.get("UNIT_HINT") else ""),
        yaxis_tickformat=tickformat,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 20},
    )
    return figure


def _table(rows, fields):
    if has_error(rows):
        return render_error_panel(rows[0]["_error"])
    if not rows:
        return html.Div("No rows returned.", style={"color": "#94a3b8"})
    columns = list(rows[0].keys())
    return html.Table(
        [
            html.Thead(html.Tr([html.Th(_label(column, fields), style={"textAlign": "left"}) for column in columns])),
            html.Tbody(
                [
                    html.Tr([html.Td(_format_value(row.get(column), fields.get(column))) for column in columns])
                    for row in rows
                ]
            ),
        ],
        style={"width": "100%", "borderCollapse": "collapse"},
    )


def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix="/",
        requests_pathname_prefix=url_base_pathname.rstrip("/") + "/",
        title=metadata.get("title", 'Golden semantic_sales'),
    )
    app.layout = html.Div(
        [
            dcc.Interval(id="refresh", interval=60000, n_intervals=0),
            html.H1(metadata.get("title", 'Golden semantic_sales')),
            html.P(metadata.get("description", "")),
            html.Div(id="provenance", style={"marginBottom": "1rem"}),
            html.Div(
                id="summary",
                style={"display": "grid", "gridTemplateColumns": "repeat(4, minmax(0, 1fr))", "gap": "1rem"},
            ),
            html.H3(id="chart-heading", style={"marginTop": "1.5rem"}),
            dcc.Graph(id="chart"),
            html.H3(id="table-heading", style={"marginTop": "1rem"}),
            html.Div(id="table"),
            html.P(
                f"Governed metrics from {OBJECT_REF}. Definitions, labels and formats come from the semantic model; "
                "change them there, not in this app.",
                style={"color": "#64748b", "marginTop": "1.5rem", "fontSize": "12px"},
            ),
        ],
        style={"fontFamily": "sans-serif", "margin": "2rem auto", "maxWidth": "1200px"},
    )

    @app.callback(
        Output("provenance", "children"),
        Output("summary", "children"),
        Output("chart-heading", "children"),
        Output("chart", "figure"),
        Output("table-heading", "children"),
        Output("table", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh(_n_intervals):
        fields = _fields_by_column(load_rows(server, metadata, __file__, "queries/business/fields.sql"))
        provenance = load_row(server, metadata, __file__, "queries/business/provenance.sql")
        summary = load_row(server, metadata, __file__, "queries/business/summary.sql")
        trend = load_rows(server, metadata, __file__, "queries/business/trend.sql")
        detail = load_rows(server, metadata, __file__, "queries/business/detail.sql")

        chart_heading = _label(TREND_Y, fields) + (f" by {_label(TREND_X, fields)}" if TREND_X else "")
        table_heading = f"By {_label(DETAIL_DIMENSION, fields)}" if DETAIL_DIMENSION else "Detail"
        return (
            _provenance_strip(provenance, fields),
            _metric_cards(summary, fields),
            chart_heading,
            _trend_figure(trend, fields),
            table_heading,
            _table(detail, fields),
        )

    return app
