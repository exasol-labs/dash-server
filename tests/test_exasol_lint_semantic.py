from __future__ import annotations

from types import SimpleNamespace

from dash_server.workspace.exasol_lint import exasol_validation_report

COMPILED_SIMPLE = (
    'SELECT "customer_region" AS "CUSTOMER_REGION", "total_revenue" AS "TOTAL_REVENUE"\n'
    "FROM (\n"
    'SELECT mat."CUSTOMER_REGION" AS "customer_region", SUM(mat."TOTAL_REVENUE") AS "total_revenue"\n'
    'FROM "MART"."SALES_REVENUE_BY_REGION" mat\n'
    'GROUP BY mat."CUSTOMER_REGION"\n'
    ")\n"
)
COMPILED_FUSED = (
    'SELECT "customer_segment" AS "CUSTOMER_SEGMENT" FROM (WITH "__esv_b_1" AS (SELECT o.amount AS "__esv_s_1" '
    'FROM "SRC"."ORDERS_HOT" o) SELECT "__esv_s_1" FROM "__esv_b_1")'
)


def _messages(sql: str) -> list[str]:
    files = {"queries/q.sql": sql, "app.py": "# queries/q.sql\n"}
    manifest = SimpleNamespace(template="exasol-analytics", data_sources={"primary": {"kind": "exasol", "profile": "kit"}})
    report = exasol_validation_report(files, manifest, python_files={"app.py": files["app.py"]})
    return [issue["message"] for issue in report["issues"] if issue.get("path") == "queries/q.sql"]


def test_compiled_semantic_sql_is_flagged() -> None:
    for sql in (COMPILED_SIMPLE, COMPILED_FUSED):
        assert any("compiled by Exasol Semantic Views" in message for message in _messages(sql))


def test_hand_written_sql_is_not_flagged_as_compiled() -> None:
    sql = 'SELECT o."REGION" AS "Region", SUM(o."AMOUNT") AS "AMOUNT" FROM MART.ORDERS o GROUP BY o."REGION"'
    assert not any("compiled by Exasol Semantic Views" in message for message in _messages(sql))


def test_semantic_sql_has_no_limit_or_aggregation_warning() -> None:
    assert _messages("SELECT total_revenue, gross_margin FROM SEMANTIC_SALES.SALES") == []


def test_semicolons_in_comments_and_literals_are_not_extra_statements() -> None:
    sql = "-- revenue; excluding cancelled\nSELECT region, SUM(amount) FROM MART.ORDERS WHERE note <> 'a;b' GROUP BY region"
    assert not any("one statement" in message for message in _messages(sql))
    assert any("one statement" in message for message in _messages("SELECT 1 FROM DUAL; SELECT 2 FROM DUAL"))
