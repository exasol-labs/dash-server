from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dash_server.exceptions import DashServerError
from dash_server.exasol.semantic_models import MODELS_COLUMNS
from dash_server.exasol.service import ExasolDashboardService

PREPROCESSOR = "SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"
FIELD_COLUMNS = [
    "MODEL_NAME",
    "OBJECT_NAME",
    "PUBLISHED_OBJECT_NAME",
    "FIELD_KIND",
    "FIELD_NAME",
    "SQL_COLUMN_NAME",
    "ORDINAL_POSITION",
    "DATA_TYPE",
    "DISPLAY_NAME",
    "FORMAT_HINT",
    "IS_CERTIFIED",
]


def _field(object_name: str, kind: str, name: str, position: int, data_type: str, certified: bool = True) -> list[Any]:
    label = name.replace("_", " ").title()
    return ["sales", object_name, object_name, kind, name, name.upper(), position, data_type, label, None, certified]


SALES_FIELDS = [
    _field("SALES", "DIMENSION", "customer_region", 1, "VARCHAR(100)"),
    _field("SALES", "DIMENSION", "order_month", 2, "DATE"),
    _field("SALES", "DIMENSION", "product_category", 3, "VARCHAR(100)"),
    _field("SALES", "METRIC", "draft_metric", 4, "DECIMAL(18,2)", certified=False),
    _field("SALES", "METRIC", "total_revenue", 5, "DECIMAL(18,2)"),
    _field("SALES", "METRIC", "unit_price_avg", 6, "DECIMAL(18,2)"),
    _field("ORDER_HEADER", "DIMENSION", "ship_mode", 1, "VARCHAR(32)"),
    _field("ORDER_HEADER", "METRIC", "total_freight", 2, "DECIMAL(18,2)"),
]
VALID = [
    ("SALES", "total_revenue", "order_month"),
    ("SALES", "total_revenue", "product_category"),  # customer_region deliberately not valid
    ("SALES", "draft_metric", "product_category"),
    ("ORDER_HEADER", "total_freight", "ship_mode"),
]


class _Service(ExasolDashboardService):
    """The real scaffold logic over canned catalog results."""

    def __init__(self, *, models: bool, columns: list[tuple[str, str, str, str]] | None = None) -> None:
        self.profile_store = SimpleNamespace(get_profile=lambda _name: SimpleNamespace(name="kit"))
        self._models = models
        self._columns = columns or []

    def execute_profile_query(self, profile_name: str, sql_text: str, *, params: Any = None) -> dict[str, Any]:
        if "MODELS_FOR_AGENT" in sql_text:
            if not self._models:
                return {"status": "error", "error": "object SEMANTIC_AGENT.MODELS_FOR_AGENT not found"}
            row = ["sales", "SEMANTIC_SALES", PREPROCESSOR, "WARNING", "WARNING", 1, 0, 9, "Certified sales"]
            return {"status": "ok", "columns": MODELS_COLUMNS, "rows": [row]}
        if "FIELDS_FOR_AGENT" in sql_text:
            return {"status": "ok", "columns": FIELD_COLUMNS, "rows": SALES_FIELDS}
        if "VALID_COMBINATIONS_FOR_AGENT" in sql_text:
            rows = [[metric, dimension] for obj, metric, dimension in VALID if obj == params["object_name"]]
            return {"status": "ok", "columns": ["METRIC_NAME", "DIMENSION_NAME"], "rows": rows}
        if "EXA_ALL_COLUMNS" in sql_text:
            rows = [[schema, table, column, column_type, 1] for schema, table, column, column_type in self._columns]
            columns = ["COLUMN_SCHEMA", "COLUMN_TABLE", "COLUMN_NAME", "COLUMN_TYPE", "COLUMN_ORDINAL_POSITION"]
            return {"status": "ok", "columns": columns, "rows": rows}
        if "CONSTRAINT" in sql_text:
            return {"status": "ok", "columns": [], "rows": []}
        raise AssertionError(sql_text)


def _blueprint(service: _Service, **kwargs: Any) -> dict[str, Any]:
    return service.build_schema_scaffold_bundle(app_name="demo", profile_name="kit", **kwargs)["schema_blueprint"]


def test_published_schema_scaffolds_semantic_sql_from_valid_combinations() -> None:
    blueprint = _blueprint(_Service(models=True), schema_name="semantic_sales")

    assert (blueprint["kind"], blueprint["model"], blueprint["table_name"]) == ("semantic", "sales", "SALES")
    # Certified metrics come first; the uncertified one is kept, but last.
    assert [m["name"] for m in blueprint["summary_metrics"]] == ["total_revenue", "unit_price_avg", "draft_metric"]
    assert blueprint["primary_metric"]["name"] == "total_revenue"
    assert blueprint["time_dimension"]["name"] == "order_month"
    # customer_region comes first in the model but isn't valid with total_revenue.
    assert blueprint["category_dimension"]["name"] == "product_category"
    assert [m["name"] for m in blueprint["detail_metrics"]] == ["total_revenue", "draft_metric"]


def test_table_name_selects_a_semantic_object() -> None:
    blueprint = _blueprint(_Service(models=True), schema_name="SEMANTIC_SALES", table_name="order_header")
    assert blueprint["table_name"] == "ORDER_HEADER"
    assert blueprint["primary_metric"]["name"] == "total_freight"
    assert blueprint["time_dimension"] is None
    assert blueprint["category_dimension"]["name"] == "ship_mode"

    with pytest.raises(DashServerError) as excinfo:
        _blueprint(_Service(models=True), schema_name="SEMANTIC_SALES", table_name="NOPE")
    assert excinfo.value.details["available_objects"] == ["ORDER_HEADER", "SALES"]


def test_blind_scaffold_prefers_a_published_model_over_raw_tables() -> None:
    service = _Service(models=True, columns=[("MART", "ORDERS", "AMOUNT", "DECIMAL(18,2)")])
    blueprint = _blueprint(service)
    assert blueprint["kind"] == "semantic"
    assert blueprint["semantic_candidates"] == [
        {"model": "sales", "published_schema": "SEMANTIC_SALES", "readiness": "WARNING"}
    ]


def test_raw_scaffold_skips_semantic_internals_but_not_lookalike_user_schemas() -> None:
    columns = [
        ("SEMANTIC_CATALOG", "METRICS", "METRIC_ID", "DECIMAL(18,0)"),
        ("SEMANTIC_SALES", "SALES", "TOTAL_REVENUE", "DECIMAL(18,2)"),
        ("SEMANTIC_REPORTING", "BOOKINGS", "AMOUNT", "DECIMAL(18,2)"),
    ]
    # Without Semantic Views, a user schema named SEMANTIC_* is ordinary business data.
    blueprint = _blueprint(_Service(models=False, columns=columns))
    assert "kind" not in blueprint
    candidates = {(c["schema_name"], c["table_name"]) for c in blueprint["table_candidates"]}
    assert ("SEMANTIC_REPORTING", "BOOKINGS") in candidates
    assert ("SEMANTIC_CATALOG", "METRICS") not in candidates

    # With it, a raw scaffold (table_name forces the raw path) never treats a published
    # semantic schema as a raw table.
    blueprint = _blueprint(_Service(models=True, columns=columns), table_name="BOOKINGS")
    candidates = {(c["schema_name"], c["table_name"]) for c in blueprint["table_candidates"]}
    assert ("SEMANTIC_SALES", "SALES") not in candidates
    assert blueprint["table_name"] == "BOOKINGS"


def _bundle(service: _Service, **kwargs: Any) -> dict[str, Any]:
    return service.build_schema_scaffold_bundle(app_name="demo", profile_name="kit", **kwargs)


def test_semantic_scaffold_reads_presentation_from_the_model_at_runtime() -> None:
    bundle = _bundle(_Service(models=True), schema_name="SEMANTIC_SALES")
    files = {entry["path"]: entry["content"] for entry in bundle["files"]}

    assert bundle["pattern"] == "semantic-model"
    # No Exasol system tabs: a viewer of governed metrics doesn't need them.
    assert not [path for path in files if path.startswith("queries/system/")]
    # Columns keep their SQL names so the app can match them to FIELDS_FOR_AGENT.SQL_COLUMN_NAME.
    assert " AS " not in files["queries/business/summary.sql"]
    assert " AS " not in files["queries/business/trend.sql"]
    assert "FROM SEMANTIC_AGENT.FIELDS_FOR_AGENT" in files["queries/business/fields.sql"]
    assert "PUBLISHED_SCHEMA = 'SEMANTIC_SALES'" in files["queries/business/fields.sql"]
    assert "WHERE STATUS <> 'RUNNING'" in files["queries/business/provenance.sql"]
    assert "WHERE m.MODEL_NAME = 'sales'" in files["queries/business/provenance.sql"]


def _load_semantic_app(tmp_path: Any) -> Any:
    import importlib.util

    bundle = _bundle(_Service(models=True), schema_name="SEMANTIC_SALES")
    for entry in bundle["files"]:
        path = tmp_path / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry["content"])
    spec = importlib.util.spec_from_file_location("generated_semantic_app", tmp_path / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_semantic_app_formats_and_labels_by_the_model(tmp_path: Any) -> None:
    from datetime import date, datetime
    from decimal import Decimal

    app = _load_semantic_app(tmp_path)
    fields = app._fields_by_column(
        [
            {
                "SQL_COLUMN_NAME": "TOTAL_REVENUE",
                "FIELD_KIND": "METRIC",
                "DISPLAY_NAME": "Total Revenue",
                "FORMAT_HINT": "currency",
                "UNIT_HINT": "EUR",
                "IS_CERTIFIED": True,
            },
            {
                "SQL_COLUMN_NAME": "GROSS_MARGIN_PCT",
                "FIELD_KIND": "METRIC",
                "DISPLAY_NAME": "Gross Margin %",
                "FORMAT_HINT": "percentage",
                "UNIT_HINT": None,
                "IS_CERTIFIED": False,
            },
            {
                "SQL_COLUMN_NAME": "ORDER_MONTH",
                "FIELD_KIND": "DIMENSION",
                "DISPLAY_NAME": "Order Month",
                "FORMAT_HINT": "month",
                "UNIT_HINT": None,
                "IS_CERTIFIED": True,
            },
        ]
    )

    assert app._format_value(Decimal("5270"), fields["TOTAL_REVENUE"]) == "5,270.00 EUR"
    assert app._format_value(0.32827324478178366, fields["GROSS_MARGIN_PCT"]) == "32.8 %"
    assert app._format_value(datetime(2026, 3, 1), fields["ORDER_MONTH"]) == "2026-03"
    assert app._format_value(date(2026, 3, 1), None) == "2026-03-01"
    assert app._format_value(1234, None) == "1,234"
    assert app._format_value(None, None) == "-"
    # An unknown column (or no field metadata at all) falls back to its SQL name.
    assert app._label("TOTAL_REVENUE", fields) == "Total Revenue"
    assert app._label("UNIT_PRICE_AVG", fields) == "UNIT_PRICE_AVG"
    assert app._fields_by_column([{"_error": "no access"}]) == {}

    cards = app._metric_cards({"TOTAL_REVENUE": Decimal("5270"), "GROSS_MARGIN_PCT": 0.328}, fields)
    assert [card.children[0].children for card in cards] == ["Total Revenue (certified)", "Gross Margin %"]
    assert [card.children[1].children for card in cards] == ["5,270.00 EUR", "32.8 %"]


def test_generated_semantic_app_shows_the_last_finished_validation(tmp_path: Any) -> None:
    from datetime import datetime

    app = _load_semantic_app(tmp_path)
    fields = {
        "TOTAL_REVENUE": {"FIELD_KIND": "METRIC", "IS_CERTIFIED": True},
        "DRAFT": {"FIELD_KIND": "METRIC", "IS_CERTIFIED": False},
    }
    row = {
        "MODEL_NAME": "sales",
        "VERSION_NUMBER": 3,
        "VALIDATION_STATUS": "WARNING",
        "WARNING_COUNT": 2,
        "FINISHED_AT": datetime(2026, 10, 23, 12, 15),
    }

    strip = app._provenance_strip(row, fields)

    assert strip.children == (
        "Semantic model sales v3 · validation WARNING (2 warning(s)) · last validated 2026-10-23 12:15 · "
        "1 of 2 metrics certified"
    )
    invalid = app._provenance_strip({**row, "VALIDATION_STATUS": "ERROR"}, fields)
    assert "validation INVALID" in invalid.children
    assert invalid.style["color"] == "#991b1b"
    assert "validation NOT VALIDATED" in app._provenance_strip({**row, "VALIDATION_STATUS": None}, fields).children
