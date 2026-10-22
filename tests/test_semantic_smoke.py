from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.exasol.sql_smoke import run_sql_smoke

PREPROCESSOR = "SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"
SEMANTIC_QUERY = ("queries/kpis.sql", "SELECT total_revenue FROM semantic_sales.SALES")
RAW_QUERY = ("queries/raw.sql", "SELECT COUNT(*) FROM MART.ORDERS")
SALES_MODEL = ("sales", "SEMANTIC_SALES", PREPROCESSOR, "WARNING", "WARNING", 2, 0, 755)


class _Connection:
    """Answers the smoke session's catalog lookups; records every other statement."""

    def __init__(
        self,
        *,
        models: list[tuple[Any, ...]] | None,
        preprocessor: str | None = None,
        model_columns: list[str] | None = None,
    ) -> None:
        self.models = models
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
        ]
        self.statements: list[str] = []
        self.catalog_lookups = 0

    def execute(self, sql: str, params: Any = None) -> Any:
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
