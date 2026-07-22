"""Scaffold generation for Exasol-backed hosted Dash apps.

This module is a thin loader plus parameterization logic. The bulk of the generated
content lives as package data alongside it:

* ``scaffold_templates/*.py.tmpl`` — the four generated-app sources, rendered with
  :meth:`str.format` (literal braces are doubled in the template files).
* ``scaffold_templates/*.sql`` — the static SQL bodies for the built-in patterns.
* ``scaffold_templates/help_*.json`` — the static guidance payloads.
* ``scaffold_helper.py`` — the runtime helper shipped verbatim as ``dash_server_exasol.py``;
  it is an importable, unit-testable module whose *text* is read at scaffold time.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

EXASOL_DASHBOARD_PATTERNS = ("analytics-hub", "overview", "kpi-trend", "ops-monitor")
EXASOL_SCAFFOLD_REQUIREMENTS = "dash>=4.0,<5.0\nplotly>=5.18\npyexasol>=2.2.2,<3.0\n"

_TEMPLATE_DIR = "scaffold_templates"
_HELPER_MODULE = "scaffold_helper.py"

_ANALYTICS_HUB_DEFAULT_CONTEXT = {
    "caption": "Replace the business SQL files with schema-specific queries after discovery.",
    "summary_heading": "Business KPI Snapshot",
    "chart_heading": "Business Trend",
    "table_heading": "Business Detail",
}


def _template_text(name: str) -> str:
    """Read a raw package-data template file from ``scaffold_templates``."""

    return resources.files(__package__).joinpath(_TEMPLATE_DIR, name).read_text(encoding="utf-8")


def _help_payload(name: str) -> dict[str, Any]:
    return json.loads(_template_text(name))


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
        {"path": "dash_server_exasol.py", "content": render_exasol_helper_py()},
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
        {"path": "dash_server_exasol.py", "content": render_exasol_helper_py()},
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
    return _help_payload("help_connection_modes.json")


def exasol_sql_placeholders_help() -> dict[str, Any]:
    return _help_payload("help_sql_placeholders.json")


def exasol_dashboard_patterns_help() -> dict[str, Any]:
    return _help_payload("help_dashboard_patterns.json")


def exasol_agent_workflow_help() -> dict[str, Any]:
    return _help_payload("help_agent_workflow.json")


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
    """Return the canonical contents of ``dash_server_exasol.py`` for the exasol-analytics template.

    The text is the source of the importable :mod:`dash_server.exasol.scaffold_helper`
    module, read verbatim so the shipped helper and the runtime library stay identical.
    """

    return resources.files(__package__).joinpath(_HELPER_MODULE).read_text(encoding="utf-8")


def _render_app_py(title: str, pattern: str) -> str:
    if pattern == "analytics-hub":
        return _render_analytics_hub_app_py(title)
    if pattern == "overview":
        return _render_overview_app_py(title)
    if pattern == "kpi-trend":
        return _render_kpi_trend_app_py(title)
    return _render_ops_monitor_app_py(title)


def _render_analytics_hub_app_py(title: str, business_context: dict[str, str] | None = None) -> str:
    context = business_context or dict(_ANALYTICS_HUB_DEFAULT_CONTEXT)
    return _template_text("analytics_hub_app.py.tmpl").format(
        title=title,
        caption=context["caption"],
        summary_heading=context["summary_heading"],
        chart_heading=context["chart_heading"],
        table_heading=context["table_heading"],
    )


def _render_overview_app_py(title: str) -> str:
    return _template_text("overview_app.py.tmpl").format(title=title)


def _render_kpi_trend_app_py(title: str) -> str:
    return _template_text("kpi_trend_app.py.tmpl").format(title=title)


def _render_ops_monitor_app_py(title: str) -> str:
    return _template_text("ops_monitor_app.py.tmpl").format(title=title)


def _overview_summary_sql() -> str:
    return _template_text("overview_summary.sql")


def _overview_detail_sql() -> str:
    return _template_text("overview_detail.sql")


def _kpi_summary_sql() -> str:
    return _template_text("kpi_summary.sql")


def _kpi_trend_sql() -> str:
    return _template_text("kpi_trend.sql")


def _kpi_detail_sql() -> str:
    return _template_text("kpi_detail.sql")


def _placeholder_business_summary_sql() -> str:
    return _template_text("placeholder_business_summary.sql")


def _placeholder_business_trend_sql() -> str:
    return _template_text("placeholder_business_trend.sql")


def _placeholder_business_detail_sql() -> str:
    return _template_text("placeholder_business_detail.sql")


def _ops_meta_sql() -> str:
    return _template_text("ops_meta.sql")


def _ops_sessions_sql() -> str:
    return _template_text("ops_sessions.sql")


def _ops_monitor_sql() -> str:
    return _template_text("ops_monitor.sql")


def _ops_usage_sql() -> str:
    return _template_text("ops_usage.sql")


def _ops_sql_hist_sql() -> str:
    return _template_text("ops_sql_hist.sql")


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
