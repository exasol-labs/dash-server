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
