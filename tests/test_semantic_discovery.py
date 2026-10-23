from __future__ import annotations

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


class _Connection:
    """Answers the Semantic Views catalog lookup; an ALTER SESSION activates the preprocessor."""

    def __init__(self, models: list[tuple[Any, ...]] | None) -> None:
        self.models = models
        self.preprocessor: str | None = None

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql.startswith("ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT"):
            self.preprocessor = sql.split("=", 1)[1].strip()
            return SimpleNamespace(result_type="rowCount")
        if "MODELS_FOR_AGENT" in sql:
            if self.models is None:
                raise RuntimeError("object SEMANTIC_AGENT.MODELS_FOR_AGENT not found")
            return SimpleNamespace(column_names=lambda: MODEL_COLUMNS, fetchall=lambda: self.models)
        if "EXA_PARAMETERS" in sql:
            return SimpleNamespace(fetchval=lambda: self.preprocessor)
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self) -> None:
        return None


def _validate(tmp_path, monkeypatch, *, models, session_init_sql=None) -> dict[str, Any]:
    monkeypatch.setenv("EXA_PASSWORD", "test")
    connector = SimpleNamespace(connect=lambda **_kwargs: _Connection(models))
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

    with_init = _validate(
        tmp_path,
        monkeypatch,
        models=[SALES],
        session_init_sql=[f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}"],
    )
    assert with_init["is_valid"] is True
    assert with_init["semantic_layer"]["models"][0]["preprocessor_active"] is True


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

    blocked = _semantic_layer_guidance({"status": "found", "models": [{**model, "preprocessor_active": False}]})
    assert blocked is not None
    assert f"ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {PREPROCESSOR}" in blocked["next_step"]
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
