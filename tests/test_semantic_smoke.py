from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
import json
from typing import Any

from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.exasol.sql_smoke import run_sql_smoke

PREPROCESSOR = "SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"
SEMANTIC_QUERY = ("queries/kpis.sql", "SELECT total_revenue FROM semantic_sales.SALES")
RAW_QUERY = ("queries/raw.sql", "SELECT COUNT(*) FROM MART.ORDERS")
SALES_MODEL = ("sales", "SEMANTIC_SALES", PREPROCESSOR, "WARNING", "WARNING", 2, 0, 755, "Certified sales model")


class _Connection:
    """Answers the smoke session's catalog lookups; records every other statement."""

    def __init__(
        self,
        *,
        models: list[tuple[Any, ...]] | None,
        preprocessor: str | None = None,
        model_columns: list[str] | None = None,
        governed: list[tuple[str, str, str]] | None = None,
        compile_status: str = "OK",
        finished_runs: list[tuple[Any, ...]] | None = None,
        issues: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.compile_status = compile_status
        # None: SEMANTIC_SOURCE isn't readable.
        self.finished_runs = finished_runs
        self.issues = issues or []
        self.models = models
        self.governed = governed or []
        self.preprocessor = preprocessor
        self.model_columns = model_columns or [
            "MODEL_NAME",
            "PUBLISHED_SCHEMA",
            "PREPROCESSOR_QUALIFIED_NAME",
            "AGENT_READINESS",
            "VALIDATION_STATUS",
            "VALIDATION_WARNING_COUNT",
            "VALIDATION_ERROR_COUNT",
            "LATEST_VALIDATION_RUN_ID",
            "DESCRIPTION",
        ]
        self.statements: list[str] = []
        self.catalog_lookups = 0

    def execute(self, sql: str, params: Any = None) -> Any:
        if "COMPILE_SQL" in sql:
            plan = {
                "model": "sales",
                "object": "SALES",
                "definition_hashes": {"metrics": {"total_revenue": "h-revenue"}, "dimensions": {}},
            }
            row = (self.compile_status, json.dumps(plan))
            return SimpleNamespace(column_names=lambda: ["STATUS", "PLAN_JSON"], fetchone=lambda: row)
        if "SEMANTIC_SOURCE.VALIDATION_RUNS" in sql:
            if self.finished_runs is None:
                raise RuntimeError("insufficient privileges: SELECT on SEMANTIC_SOURCE")
            columns = ["MODEL_NAME", "VALIDATION_RUN_ID", "STATUS", "ERROR_COUNT", "WARNING_COUNT", "PRECONDITION_COUNT"]
            return SimpleNamespace(column_names=lambda: columns, fetchall=lambda: self.finished_runs)
        if "SEMANTIC_SOURCE.VALIDATION_RESULTS" in sql:
            return SimpleNamespace(fetchall=lambda: self.issues)
        if "FUSION_FOR_AGENT" in sql:
            columns = ["SOURCE_OBJECT", "MODEL_NAME", "PUBLISHED_SCHEMA"]
            return SimpleNamespace(column_names=lambda: columns, fetchall=lambda: self.governed)
        if "MODELS_FOR_AGENT" in sql:
            self.catalog_lookups += 1
            if self.models is None:
                raise RuntimeError("object SEMANTIC_AGENT.MODELS_FOR_AGENT not found")
            return SimpleNamespace(column_names=lambda: self.model_columns, fetchall=lambda: self.models)
        if "EXA_PARAMETERS" in sql:
            return SimpleNamespace(fetchval=lambda: self.preprocessor)
        self.statements.append(sql)
        return SimpleNamespace(close=lambda: None)

    def close(self) -> None:
        return None


def _smoke(connection: _Connection, files: list[tuple[str, str]]):
    profile = ExasolProfile(
        name="kit",
        backend="onprem",
        deployment_mode="local_direct",
        credential_mode="password",
        user="sys",
        dsn="localhost:8563",
        description="",
        tls_verify=False,
        secret_ref=ExasolSecretRef(provider="env", key="EXA_PASSWORD"),
    )
    manager = SimpleNamespace(
        connect_uncached=lambda _profile, **_kwargs: connection,
        statement_timeout_seconds=lambda _profile: 30,
    )
    return run_sql_smoke(profile=profile, sql_files=files, connection_manager=manager)  # type: ignore[arg-type]


def test_semantic_query_without_active_preprocessor_fails_with_the_fix() -> None:
    connection = _Connection(models=[SALES_MODEL])

    report = _smoke(connection, [SEMANTIC_QUERY, RAW_QUERY])

    semantic, raw = report.files
    assert report.overall_status == "failed"
    assert semantic.outcome == "failed"
    assert semantic.semantic_models == ("sales",)
    assert "published semantic model 'sales' (SEMANTIC_SALES)" in (semantic.error_text or "")
    assert f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}" in (semantic.error_text or "")
    assert raw.outcome == "passed"
    # The semantic file isn't executed: its WHERE 1=0 probe could pass and hide the failure.
    assert connection.statements == [f"SELECT * FROM (\n{RAW_QUERY[1]}\n) WHERE 1=0"]


def test_semantic_query_with_active_preprocessor_is_smoke_tested_normally() -> None:
    connection = _Connection(
        models=[SALES_MODEL], preprocessor='"SEMANTIC_ADMIN"."semantic_preprocessor"'
    )

    report = _smoke(connection, [SEMANTIC_QUERY])

    assert report.overall_status == "passed"
    assert report.files[0].semantic_models == ("sales",)
    assert connection.statements == [f"SELECT * FROM (\n{SEMANTIC_QUERY[1]}\n) WHERE 1=0"]
    (model,) = report.semantic_models
    assert (model.model, model.readiness, model.validation_warning_count, model.latest_validation_run_id) == (
        "sales",
        "WARNING",
        2,
        755,
    )


def test_without_semantic_views_installed_every_query_is_smoke_tested() -> None:
    connection = _Connection(models=None)

    report = _smoke(connection, [SEMANTIC_QUERY, RAW_QUERY])

    assert report.overall_status == "passed"
    assert [file.semantic_models for file in report.files] == [(), ()]
    assert len(connection.statements) == 2


def test_unexpected_catalog_shape_is_ignored() -> None:
    connection = _Connection(models=[("2026-03-30", "SEMANTIC_SALES", "sys")], model_columns=["A", "B", "C"])

    report = _smoke(connection, [SEMANTIC_QUERY])

    assert report.overall_status == "passed"
    assert report.files[0].semantic_models == ()


def test_catalog_is_looked_up_once_and_only_when_a_query_is_executed() -> None:
    connection = _Connection(models=[SALES_MODEL], preprocessor=PREPROCESSOR)
    _smoke(connection, [SEMANTIC_QUERY, SEMANTIC_QUERY, RAW_QUERY])
    assert connection.catalog_lookups == 1

    unparameterized = _Connection(models=[SALES_MODEL])
    report = _smoke(unparameterized, [("queries/p.sql", "SELECT * FROM SEMANTIC_SALES.SALES WHERE x = {x!s}")])
    assert report.files[0].outcome == "failed"
    assert unparameterized.catalog_lookups == 0


def _semantic_probe(models: list[dict[str, Any]] | None) -> dict[str, Any]:
    from dash_server.runtime.prober import HealthProber

    details = {"semantic_models": models} if models is not None else {}
    return HealthProber(svc=None)._semantic_models_probe({"name": "sql_smoke", "details": details})  # type: ignore[arg-type]


def test_semantic_models_probe_is_not_applicable_without_semantic_queries() -> None:
    assert _semantic_probe(None)["status"] == "not_applicable"


def test_semantic_models_probe_passes_but_lists_warnings() -> None:
    probe = _semantic_probe([{"model": "sales", "published_schema": "SEMANTIC_SALES", "readiness": "WARNING"}])
    assert probe["status"] == "passed"
    assert probe["details"]["models_with_warnings"] == ["sales"]


def test_semantic_models_probe_fails_on_an_unusable_model() -> None:
    probe = _semantic_probe(
        [
            {"model": "sales", "published_schema": "SEMANTIC_SALES", "readiness": "VALID"},
            {"model": "fusion", "published_schema": "SEMANTIC_FUSION", "readiness": "INVALID"},
        ]
    )
    assert probe["status"] == "failed"
    assert "'fusion' (SEMANTIC_FUSION) is INVALID" in probe["details"]["latest_error"]


def test_raw_reads_of_a_governed_table_pass_with_a_warning() -> None:
    connection = _Connection(
        models=[SALES_MODEL],
        preprocessor=PREPROCESSOR,
        governed=[("MART.ORDER_LINES", "sales", "SEMANTIC_SALES"), ("MART.ORDERS", "sales", "SEMANTIC_SALES")],
    )
    files = [
        ("queries/margin.sql", 'SELECT SUM(ol.quantity) FROM "MART"."ORDER_LINES" ol GROUP BY 1'),
        ("queries/orders_x.sql", "SELECT COUNT(*) FROM MART.ORDERS_X"),  # a lookalike name isn't governed
        SEMANTIC_QUERY,
    ]

    report = _smoke(connection, files)

    assert report.overall_status == "passed"
    margin, lookalike, semantic = report.files
    assert margin.warnings == (
        "This query reads MART.ORDER_LINES, which backs the published semantic model 'sales' (SEMANTIC_SALES). "
        "Metrics written by hand over it can disagree with the certified definitions; query SEMANTIC_SALES "
        "instead. See dash://exasol/help/semantic-layer.",
    )
    assert "query SEMANTIC_SALES instead" in margin.warnings[0]
    assert lookalike.warnings == ()
    assert semantic.warnings == ()


def test_semantic_queries_record_the_definitions_they_use() -> None:
    connection = _Connection(models=[SALES_MODEL], preprocessor=PREPROCESSOR)
    semantic, raw = _smoke(connection, [SEMANTIC_QUERY, RAW_QUERY]).files
    assert semantic.definitions == {
        "model": "sales",
        "object": "SALES",
        "metrics": {"total_revenue": "h-revenue"},
        "dimensions": {},
    }
    assert raw.definitions is None

    refused = _Connection(models=[SALES_MODEL], preprocessor=PREPROCESSOR, compile_status="ERROR")
    assert _smoke(refused, [SEMANTIC_QUERY]).files[0].definitions is None


def test_semantic_models_probe_lists_definitions_changed_since_build() -> None:
    from dash_server.runtime.prober import HealthProber

    model = {"model": "sales", "published_schema": "SEMANTIC_SALES", "readiness": "VALID"}
    built = [{"path": "queries/kpis.sql", "model": "sales", "metrics": {"total_revenue": "old", "total_cost": "same"}}]
    current = [{"path": "queries/kpis.sql", "model": "sales", "metrics": {"total_revenue": "new", "total_cost": "same"}}]
    smoke_probe = {"name": "sql_smoke", "details": {"semantic_models": [model], "definitions": current}}

    probe = HealthProber(svc=None)._semantic_models_probe(smoke_probe, built_definitions=built)  # type: ignore[arg-type]

    assert probe["status"] == "passed"
    assert probe["details"]["changed_since_build"] == [
        {
            "path": "queries/kpis.sql",
            "model": "sales",
            "field": "total_revenue",
            "kind": "metric",
            "built_hash": "old",
            "current_hash": "new",
        }
    ]
    unchanged = HealthProber(svc=None)._semantic_models_probe(smoke_probe, built_definitions=current)  # type: ignore[arg-type]
    assert "changed_since_build" not in unchanged["details"]


# The newest run never finished (as Semantic Views' CHECK_PARTITION_COVERAGE leaves it): no
# errors yet, so MODELS_FOR_AGENT says VALID.
UNFINISHED_FUSION = ("fusion", "SEMANTIC_FUSION", PREPROCESSOR, "VALID", "RUNNING", 0, 0, 101, None)
FUSION_QUERY = ("queries/fusion.sql", "SELECT total_revenue FROM SEMANTIC_FUSION.ORDERS")


def test_readiness_comes_from_the_newest_finished_validation_run() -> None:
    connection = _Connection(
        models=[UNFINISHED_FUSION],
        preprocessor=PREPROCESSOR,
        finished_runs=[("fusion", 98, "ERROR", 1, 1, 0)],
        issues=[
            (98, "ERROR", "SEMANTIC_MODEL_049", "customer", "Identity map CUSTOMER_XREF lost a key."),
            (98, "WARNING", "SEMANTIC_MODEL_074", "order", "Rows fall outside every partition."),
        ],
    )

    (model,) = _smoke(connection, [FUSION_QUERY]).semantic_models

    assert (model.readiness, model.validation_status, model.latest_validation_run_id) == ("INVALID", "ERROR", 98)
    assert model.unfinished_validation_run_id == 101
    assert [issue.rule_code for issue in model.issues] == ["SEMANTIC_MODEL_049", "SEMANTIC_MODEL_074"]
    probe = _semantic_probe([asdict(model)])
    assert probe["status"] == "failed"
    assert probe["details"]["latest_error"] == (
        "Semantic model 'fusion' (SEMANTIC_FUSION) is INVALID: SEMANTIC_MODEL_049 on customer: Identity map "
        "CUSTOMER_XREF lost a key. Check SEMANTIC_AGENT.MODELS_FOR_AGENT and SEMANTIC_SOURCE.VALIDATION_RESULTS."
    )


def test_unfinished_validation_without_access_to_runs_is_unknown_not_valid() -> None:
    connection = _Connection(models=[UNFINISHED_FUSION], preprocessor=PREPROCESSOR, finished_runs=None)

    (model,) = _smoke(connection, [FUSION_QUERY]).semantic_models

    assert model.readiness == "UNKNOWN"
    probe = _semantic_probe([asdict(model)])
    assert probe["status"] == "passed"
    assert probe["details"]["models_not_assessed"] == ["fusion"]


def test_unfinished_validation_without_a_finished_run_needs_validation() -> None:
    connection = _Connection(models=[UNFINISHED_FUSION], preprocessor=PREPROCESSOR, finished_runs=[])

    (model,) = _smoke(connection, [FUSION_QUERY]).semantic_models

    assert model.readiness == "NEEDS_VALIDATION"


COMPILED_QUERY = (
    "queries/frozen.sql",
    'SELECT "customer_region" AS "CUSTOMER_REGION", SUM(ol.net_amount) AS "TOTAL_REVENUE" '
    'FROM "MART"."ORDER_LINES" ol GROUP BY 1',
)


def test_saved_compiled_sql_fails_the_semantic_models_probe() -> None:
    connection = _Connection(
        models=[SALES_MODEL],
        preprocessor=PREPROCESSOR,
        governed=[("MART.ORDER_LINES", "sales", "SEMANTIC_SALES")],
    )

    report = _smoke(connection, [COMPILED_QUERY, ("queries/raw.sql", "SELECT SUM(qty) FROM MART.ORDER_LINES")])

    frozen, raw = report.files
    assert (frozen.frozen_semantic_sql, frozen.governed_reads) == (True, ("sales",))
    assert "saved from the semantic layer's compiler for 'sales' (SEMANTIC_SALES)" in frozen.warnings[0]
    assert (raw.frozen_semantic_sql, raw.governed_reads) == (False, ("sales",))
    # A model read only through its source tables is still reported.
    assert [model.model for model in report.semantic_models] == ["sales"]

    bypasses = [
        {"path": "queries/frozen.sql", "lane": "frozen", "models": ["sales"]},
        {"path": "queries/raw.sql", "lane": "raw", "models": ["sales"]},
    ]
    details = {"semantic_models": [asdict(model) for model in report.semantic_models], "semantic_bypasses": bypasses}
    probe = _probe_from_details(details)
    assert probe["status"] == "failed"
    assert probe["details"]["frozen_files"] == bypasses[:1]
    assert probe["details"]["raw_governed_reads"] == bypasses[1:]
    assert probe["details"]["latest_error"].startswith("queries/frozen.sql is SQL saved from the semantic layer's compiler")


def test_raw_governed_reads_alone_pass_the_semantic_models_probe() -> None:
    raw = [{"path": "queries/raw.sql", "lane": "raw", "models": ["sales"]}]

    probe = _probe_from_details({"semantic_bypasses": raw})

    assert probe["status"] == "passed"
    assert probe["details"]["raw_governed_reads"] == raw


def _probe_from_details(details: dict[str, Any]) -> dict[str, Any]:
    from dash_server.runtime.prober import HealthProber

    return HealthProber(svc=None)._semantic_models_probe({"name": "sql_smoke", "details": details})  # type: ignore[arg-type]
