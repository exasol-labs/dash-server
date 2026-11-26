from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from dash_server.exasol.connection_manager import ExasolConnectionManager
from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.exasol.secrets import ExasolSecretStore
from dash_server.mcp.guidance import _semantic_layer_guidance

PREPROCESSOR = "SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"
MODEL_COLUMNS = [
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
SALES = ("sales", "SEMANTIC_SALES", PREPROCESSOR, "VALID", "OK", 0, 0, 7, "Certified sales model")


GUARD_ERROR = (
    'Lua Error "SEMANTIC_SURFACE_001: semantic query requires the Lua SQL preprocessor. Run EXECUTE SCRIPT '
    'SEMANTIC_ADMIN.ENABLE_SEMANTIC_SQL() for this session, or ask an administrator to set it database-wide."'
)
FRAMEWORK = {"system": "PREPROC_RT.MASTER", "session": "PREPROC_RT.P_3F9A1C"}


class _Connection:
    """A database as Semantic SQL sees it.

    A query on a published view succeeds only when something rewrites it: the session's preprocessor
    is Semantic Views' own (an ALTER SESSION sets it), or ``routes_semantic_sql`` says a database-wide
    preprocessor (a preprocessor framework) dispatches to it. Otherwise the view's guard raises.
    """

    def __init__(
        self,
        models: list[tuple[Any, ...]] | None,
        *,
        system_preprocessor: str | None = None,
        session_preprocessor: str | None = None,
        routes_semantic_sql: bool = False,
        probe_error: str | None = None,
    ) -> None:
        self.models = models
        self.system_preprocessor = system_preprocessor
        self.preprocessor = session_preprocessor or system_preprocessor
        self.routes_semantic_sql = routes_semantic_sql
        self.probe_error = probe_error
        self.probes: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql.startswith("ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT"):
            self.preprocessor = sql.split("=", 1)[1].strip()
            self.routes_semantic_sql = False  # the framework is replaced for this session
            return SimpleNamespace(result_type="rowCount")
        if "MODELS_FOR_AGENT" in sql:
            if self.models is None:
                raise RuntimeError("object SEMANTIC_AGENT.MODELS_FOR_AGENT not found")
            return SimpleNamespace(column_names=lambda: MODEL_COLUMNS, fetchall=lambda: self.models)
        if "EXA_PARAMETERS" in sql:
            return SimpleNamespace(fetchone=lambda: (self.system_preprocessor, self.preprocessor))
        if "SEMANTIC_AGENT.FIELDS_FOR_AGENT" in sql and "SQL_COLUMN_NAME IS NOT NULL" in sql:
            rows = [("SALES", "CUSTOMER_REGION"), ("SALES", "TOTAL_REVENUE")]
            return SimpleNamespace(fetchall=lambda: rows if params == {"model": "sales"} else [])
        if '"SEMANTIC_SALES".' in sql:
            self.probes.append(sql)
            if self.probe_error:
                raise RuntimeError(self.probe_error)
            if not (self.routes_semantic_sql or self.preprocessor == PREPROCESSOR):
                raise RuntimeError(GUARD_ERROR)
            return SimpleNamespace(close=lambda: None)
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self) -> None:
        return None


def _validate(tmp_path, monkeypatch, *, models, session_init_sql=None, **database: Any) -> dict[str, Any]:
    monkeypatch.setenv("EXA_PASSWORD", "test")
    connector = SimpleNamespace(connect=lambda **_kwargs: _Connection(models, **database))
    manager = ExasolConnectionManager(ExasolSecretStore(str(tmp_path)), connector_loader=lambda: connector)
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
        query_defaults={"session_init_sql": session_init_sql} if session_init_sql else None,
    )
    return manager.validate_profile(profile)


def test_validate_reports_published_models_and_whether_sessions_can_query_them(tmp_path, monkeypatch) -> None:
    without_init = _validate(tmp_path, monkeypatch, models=[SALES])["semantic_layer"]
    assert without_init["status"] == "found"
    (model,) = without_init["models"]
    assert (model["model"], model["published_schema"], model["description"]) == (
        "sales",
        "SEMANTIC_SALES",
        "Certified sales model",
    )
    assert model["preprocessor_active"] is False
    assert without_init["database_preprocessor"] is None
    assert f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}" in model["setup"]
    # The guard's own advice (ENABLE_SEMANTIC_SQL) is not passed on.
    assert "ENABLE_SEMANTIC_SQL" not in model["setup"]
    assert model["semantic_sql_check"]["statement"] == 'SELECT "CUSTOMER_REGION" FROM "SEMANTIC_SALES"."SALES" LIMIT 0'

    with_init = _validate(
        tmp_path,
        monkeypatch,
        models=[SALES],
        session_init_sql=[f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}"],
    )
    assert with_init["is_valid"] is True
    assert with_init["semantic_layer"]["models"][0]["preprocessor_active"] is True
    assert "setup" not in with_init["semantic_layer"]["models"][0]


def test_a_preprocessor_framework_that_routes_semantic_sql_counts_as_active(tmp_path, monkeypatch) -> None:
    """The session reads PREPROC_RT.P_<hash>, never Semantic Views' preprocessor, yet Semantic SQL works."""

    result = _validate(tmp_path, monkeypatch, models=[SALES], routes_semantic_sql=True, **_framework())
    layer = result["semantic_layer"]
    assert layer["database_preprocessor"] == "PREPROC_RT.MASTER"
    assert layer["session_preprocessor"] == "PREPROC_RT.P_3F9A1C"
    (model,) = layer["models"]
    assert model["preprocessor_active"] is True
    assert "setup" not in model
    guidance = _semantic_layer_guidance(layer)
    assert guidance is not None
    assert "SQL_PREPROCESSOR_SCRIPT" not in guidance["next_step"]
    assert "exasol_profile_create_local" not in guidance["suggested_tools"]


def test_a_framework_that_doesnt_route_semantic_sql_is_never_told_to_switch_preprocessors(
    tmp_path, monkeypatch
) -> None:
    """Setting the session preprocessor would replace the framework, so the advice goes to an admin."""

    layer = _validate(tmp_path, monkeypatch, models=[SALES], **_framework())["semantic_layer"]
    (model,) = layer["models"]
    assert model["preprocessor_active"] is False
    assert "PREPROC_RT.MASTER" in model["setup"]
    assert "Don't set SQL_PREPROCESSOR_SCRIPT" in model["setup"]
    assert f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}" not in model["setup"]
    # Not in the raw error either: the guard says to run ENABLE_SEMANTIC_SQL(), which would replace the
    # framework. Its code stays, so the cause can still be told apart.
    assert "ENABLE_SEMANTIC_SQL" not in json.dumps(model["semantic_sql_check"])
    assert model["semantic_sql_check"]["error"].startswith("SEMANTIC_SURFACE_001: the published view's guard")
    guidance = _semantic_layer_guidance(layer)
    assert guidance is not None
    assert f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}" not in guidance["next_step"]
    assert guidance["suggested_tools"] == ["exasol_profile_validate"]


def test_a_probe_failing_for_another_reason_names_that_error(tmp_path, monkeypatch) -> None:
    layer = _validate(
        tmp_path,
        monkeypatch,
        models=[SALES],
        probe_error="insufficient privileges: SELECT on view SEMANTIC_SALES.SALES",
        **_framework(),
    )["semantic_layer"]
    (model,) = layer["models"]
    assert model["preprocessor_active"] is False
    assert "insufficient privileges: SELECT on view SEMANTIC_SALES.SALES" in model["setup"]
    assert model["semantic_sql_check"]["error"].startswith("insufficient privileges")


def test_the_model_preprocessor_set_database_wide_points_at_grants(tmp_path, monkeypatch) -> None:
    layer = _validate(tmp_path, monkeypatch, models=[SALES], system_preprocessor=PREPROCESSOR, probe_error=GUARD_ERROR)[
        "semantic_layer"
    ]
    (model,) = layer["models"]
    assert model["preprocessor_active"] is False
    assert "SEMANTIC_USER" in model["setup"]
    assert "ALTER SESSION" not in model["setup"]


def _framework() -> dict[str, Any]:
    return {"system_preprocessor": FRAMEWORK["system"], "session_preprocessor": FRAMEWORK["session"]}


def test_validate_without_semantic_views_reports_none_found(tmp_path, monkeypatch) -> None:
    result = _validate(tmp_path, monkeypatch, models=None)
    assert result["is_valid"] is True
    assert result["semantic_layer"] == {"status": "none_found"}


def test_guidance_points_at_the_models_or_at_the_missing_session_setup() -> None:
    model = {"model": "sales", "published_schema": "SEMANTIC_SALES", "preprocessor": PREPROCESSOR}

    ready = _semantic_layer_guidance({"status": "found", "models": [{**model, "preprocessor_active": True}]})
    assert ready is not None
    assert "sales (SEMANTIC_SALES)" in ready["next_step"]
    assert "dash://exasol/help/semantic-layer" in ready["related_resources"]

    setup = f"Add this statement ...: ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}"
    blocked = _semantic_layer_guidance(
        {
            "status": "found",
            "database_preprocessor": None,
            "models": [{**model, "preprocessor_active": False, "setup": setup}],
        }
    )
    assert blocked is not None
    assert setup in blocked["next_step"]
    assert blocked["suggested_tools"][0] == "exasol_profile_create_local"

    assert _semantic_layer_guidance({"status": "none_found"}) is None


def test_validate_tool_names_published_models_in_its_text(app, client, monkeypatch) -> None:
    monkeypatch.setenv("EXA_PASSWORD", "test")
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: SimpleNamespace(
        connect=lambda **_kwargs: _Connection([SALES])
    )

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        )
        return response.get_json()["result"]

    call(
        "exasol_profile_create_local",
        {
            "name": "kit",
            "backend": "onprem",
            "credential_mode": "password",
            "dsn": "localhost:8563",
            "user": "sys",
            "secret_env_var": "EXA_PASSWORD",
        },
    )
    result = call("exasol_profile_validate", {"name": "kit"})

    assert "Published semantic models: sales (SEMANTIC_SALES)." in result["content"][0]["text"]
    guidance = result["structuredContent"]["guidance"]
    assert "session_init_sql" in guidance["next_step"]


def test_semantic_usage_lists_the_fields_each_live_dashboard_was_built_against(app, client) -> None:
    runtime = app.extensions["runtime_service"]
    demo_revision = runtime.registry.get_app("demo").current_revision_number
    runtime.diagnostics_service.record_semantic_lock(
        "demo",
        demo_revision,
        [
            {
                "path": "queries/kpis.sql",
                "model": "sales",
                "object": "SALES",
                "metrics": {"total_revenue": "h1"},
                "dimensions": {},
            },
            {"path": "queries/frozen.sql", "lane": "frozen", "models": ["sales"]},
            {"path": "queries/raw.sql", "lane": "raw", "models": ["sales"]},
        ],
    )
    assert runtime.diagnostics_service.semantic_lock("demo", demo_revision)[0]["metrics"] == {"total_revenue": "h1"}

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "dash://exasol/semantic-usage"}},
    )
    import json

    usage = json.loads(response.get_json()["result"]["contents"][0]["text"])
    assert usage["models"] == {
        "sales": {
            "total_revenue": [
                {
                    "app": "demo",
                    "revision_number": demo_revision,
                    "path": "queries/kpis.sql",
                    "kind": "metric",
                    "definition_hash": "h1",
                }
            ]
        }
    }
    assert usage["bypasses"] == {
        "sales": [
            {"app": "demo", "revision_number": demo_revision, "path": "queries/frozen.sql", "lane": "frozen"},
            {"app": "demo", "revision_number": demo_revision, "path": "queries/raw.sql", "lane": "raw"},
        ]
    }


def test_scaffold_model_lookup_ignores_an_unfinished_validation_run(app) -> None:
    from dash_server.exasol.semantic_models import FINISHED_RUNS_COLUMNS, FINISHED_RUNS_QUERY, MODELS_COLUMNS

    service = app.extensions["exasol_dashboard_service"]
    unfinished = [
        "fusion",
        "SEMANTIC_FUSION",
        "SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR",
        "VALID",
        "RUNNING",
        0,
        0,
        101,
        None,
    ]

    def execute(profile_name, sql_text, *, params=None):
        if sql_text == FINISHED_RUNS_QUERY:
            return {"status": "ok", "columns": FINISHED_RUNS_COLUMNS, "rows": [["fusion", 98, "ERROR", 1, 0, 0]]}
        return {"status": "ok", "columns": MODELS_COLUMNS, "rows": [unfinished]}

    service.execute_profile_query = execute

    (model,) = service._published_semantic_models("kit")

    assert (model.readiness, model.latest_validation_run_id, model.unfinished_validation_run_id) == ("INVALID", 98, 101)


def test_probe_quotes_identifiers_and_falls_back_without_a_published_column() -> None:
    from dash_server.exasol.semantic_models import (
        PreprocessorSettings,
        SemanticModelRef,
        SemanticSqlProbe,
        probe_failure_note,
        probe_semantic_sql,
    )

    class _Catalog:
        def __init__(self, row: tuple[str, str] | None) -> None:
            self.row = row
            self.statements: list[str] = []

        def execute(self, sql: str, params: Any = None) -> Any:
            if "FIELDS_FOR_AGENT" in sql:
                return SimpleNamespace(fetchall=lambda: [self.row] if self.row else [])
            self.statements.append(sql)
            return SimpleNamespace(close=lambda: None)

    ref = SemanticModelRef(model="odd", published_schema='SEMANTIC_"ODD"', preprocessor=PREPROCESSOR)
    catalog = _Catalog(("my view", 'col"x'))
    probe = probe_semantic_sql(catalog, ref, PreprocessorSettings(system=None, session=None))
    assert probe == SemanticSqlProbe(
        active=True, probed=True, statement='SELECT "col""x" FROM "SEMANTIC_""ODD"""."my view" LIMIT 0'
    )

    # The model's fields can't be read: compare preprocessor names, as before.
    empty = _Catalog(None)
    assert probe_semantic_sql(empty, ref, PreprocessorSettings(system=None, session=PREPROCESSOR)).active is True
    assert probe_semantic_sql(empty, ref, PreprocessorSettings(system=None, session=None)).active is False
    assert empty.statements == []

    assert probe_failure_note(SemanticSqlProbe(active=False, probed=True, statement="q", error=GUARD_ERROR)) == ""
    assert "boom" in probe_failure_note(SemanticSqlProbe(active=False, probed=True, statement="q", error="boom"))


class _Views:
    """Published views whose probe queries answer with the given errors, in order (None: success)."""

    def __init__(self, outcomes: list[str | None]) -> None:
        self.outcomes = outcomes
        self.probes: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        if "FIELDS_FOR_AGENT" in sql:
            rows = [(f"VIEW_{index}", "FIELD") for index in range(len(self.outcomes))] + [("VIEW_0", "OTHER")]
            return SimpleNamespace(fetchall=lambda: rows)
        outcome = self.outcomes[len(self.probes)]
        self.probes.append(sql)
        if outcome is not None:
            raise RuntimeError(outcome)
        return SimpleNamespace(close=lambda: None)


def _probe(views: _Views):
    from dash_server.exasol.semantic_models import PreprocessorSettings, SemanticModelRef, probe_semantic_sql

    ref = SemanticModelRef(model="m", published_schema="SEMANTIC_M", preprocessor=PREPROCESSOR)
    return probe_semantic_sql(views, ref, PreprocessorSettings(system=None, session=None))


def test_a_compiler_refusal_still_shows_semantic_sql_ran() -> None:
    """Seen live: a partitioned joined dimension is refused (SEMANTIC_QUERY_074) with the preprocessor active."""

    refusal = (
        "While preprocessing SQL with SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR: 43000:SEMANTIC_QUERY_074: "
        "Dimension 'leg_mode' resolves to partitioned entity 'leg'"
    )
    views = _Views([refusal])
    assert _probe(views).active is True
    assert len(views.probes) == 1


def test_probe_tries_further_views_before_reporting_semantic_sql_off() -> None:
    # A framework can swallow a refusal and let the statement reach the guard: try the next view.
    views = _Views([GUARD_ERROR, None])
    assert _probe(views).active is True
    assert views.probes == [
        'SELECT "FIELD" FROM "SEMANTIC_M"."VIEW_0" LIMIT 0',
        'SELECT "FIELD" FROM "SEMANTIC_M"."VIEW_1" LIMIT 0',
    ]

    off = _Views([GUARD_ERROR, "insufficient privileges", GUARD_ERROR, None])
    probe = _probe(off)
    assert probe.active is False
    assert len(off.probes) == 3  # at most three views
    assert probe.error == "insufficient privileges"  # a real cause beats the guard's message
