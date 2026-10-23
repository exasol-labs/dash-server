from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dash_server.exceptions import DashServerError
from dash_server.exasol.connection_manager import ExasolConnectionManager
from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.exasol.secrets import ExasolSecretStore
from dash_server.exasol.service import ExasolDashboardService
from dash_server.exasol.session_init import normalize_session_init_sql, split_sql_statements

ENABLE_SEMANTIC_SQL = "ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"


class _RecordingConnection:
    def __init__(self, fail_on: str | None = None) -> None:
        self.executed: list[str] = []
        self.closed = False
        self._fail_on = fail_on

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql == self._fail_on:
            raise RuntimeError("insufficient privileges for calling script")
        self.executed.append(sql)
        return SimpleNamespace(result_type="rowCount", rowcount=lambda: 0)

    def close(self) -> None:
        self.closed = True


class _RecordingConnector:
    def __init__(self, fail_on: str | None = None) -> None:
        self.connections: list[_RecordingConnection] = []
        self._fail_on = fail_on

    def connect(self, **_kwargs: Any) -> _RecordingConnection:
        connection = _RecordingConnection(self._fail_on)
        self.connections.append(connection)
        return connection


def _profile(session_init_sql: list[str] | None = None) -> ExasolProfile:
    query_defaults: dict[str, Any] = {"statement_timeout_seconds": 30, "row_limit": 50000}
    if session_init_sql is not None:
        query_defaults["session_init_sql"] = session_init_sql
    return ExasolProfile(
        name="semantic",
        backend="onprem",
        deployment_mode="local_direct",
        credential_mode="password",
        user="sys",
        dsn="localhost:8563",
        description="test profile",
        tls_verify=False,
        secret_ref=ExasolSecretRef(provider="env", key="EXA_PASSWORD"),
        query_defaults=query_defaults,
    )


def _manager(tmp_path, monkeypatch, connector: _RecordingConnector) -> ExasolConnectionManager:
    monkeypatch.setenv("EXA_PASSWORD", "test")
    return ExasolConnectionManager(ExasolSecretStore(str(tmp_path)), connector_loader=lambda: connector)


# --- parsing and validation --------------------------------------------------------------


def test_normalize_accepts_session_statements_and_strips_trailing_semicolons() -> None:
    assert normalize_session_init_sql(
        [f"  {ENABLE_SEMANTIC_SQL};  ", "execute script SEMANTIC_ADMIN.ENABLE_SEMANTIC_SQL()", "OPEN SCHEMA MART"]
    ) == [ENABLE_SEMANTIC_SQL, "execute script SEMANTIC_ADMIN.ENABLE_SEMANTIC_SQL()", "OPEN SCHEMA MART"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("ALTER SESSION SET X = 1", "must be a list"),
        ([""], "statement 1 is empty"),
        (["SELECT 1"], "statement 1 must be a session statement"),
        (["OPEN SCHEMA A", "DELETE FROM T"], "statement 2 must be a session statement"),
        (["OPEN SCHEMA A; OPEN SCHEMA B"], "more than one statement"),
        (["OPEN SCHEMA A"] * 11, "at most 10"),
    ],
)
def test_normalize_rejects_invalid_values(value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_session_init_sql(value)


def test_split_ignores_semicolons_inside_quotes() -> None:
    assert split_sql_statements("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY;MM'; OPEN SCHEMA \"A;B\";") == [
        "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY;MM'",
        'OPEN SCHEMA "A;B"',
    ]


# --- connections ---------------------------------------------------------------------------


def test_every_new_connection_runs_session_init_in_order(tmp_path, monkeypatch) -> None:
    connector = _RecordingConnector()
    manager = _manager(tmp_path, monkeypatch, connector)
    profile = _profile([ENABLE_SEMANTIC_SQL, "OPEN SCHEMA SEMANTIC_SALES"])

    cached = manager.connect(profile)
    assert manager.connect(profile) is cached  # cached: init is not repeated
    manager.connect_uncached(profile)  # SQL smoke and exports open their own

    assert [c.executed for c in connector.connections] == [[ENABLE_SEMANTIC_SQL, "OPEN SCHEMA SEMANTIC_SALES"]] * 2


def test_rewritten_profile_replaces_the_cached_connection(tmp_path, monkeypatch) -> None:
    connector = _RecordingConnector()
    manager = _manager(tmp_path, monkeypatch, connector)

    first = manager.connect(_profile([ENABLE_SEMANTIC_SQL]))
    second = manager.connect(_profile())  # same name, session_init_sql removed

    assert second is not first
    assert first.closed is True
    assert second.executed == []


def test_profile_without_session_init_runs_nothing(tmp_path, monkeypatch) -> None:
    connector = _RecordingConnector()
    _manager(tmp_path, monkeypatch, connector).connect_uncached(_profile())
    assert connector.connections[0].executed == []


def test_failed_session_init_closes_the_connection_and_names_the_statement(tmp_path, monkeypatch) -> None:
    connector = _RecordingConnector(fail_on="OPEN SCHEMA SEMANTIC_SALES")
    manager = _manager(tmp_path, monkeypatch, connector)

    with pytest.raises(DashServerError) as excinfo:
        manager.connect(_profile([ENABLE_SEMANTIC_SQL, "OPEN SCHEMA SEMANTIC_SALES"]))

    assert excinfo.value.category == "exasol_session_init_failed"
    assert excinfo.value.details["statement_index"] == 2
    assert excinfo.value.details["statement"] == "OPEN SCHEMA SEMANTIC_SALES"
    assert connector.connections[0].closed is True
    assert manager._thread_cache() == {}


@pytest.mark.parametrize(
    ("statements", "fail_on", "expected_status", "expected_valid"),
    [
        (None, None, "not_configured", True),
        ([ENABLE_SEMANTIC_SQL], None, "applied", True),
        ([ENABLE_SEMANTIC_SQL], ENABLE_SEMANTIC_SQL, "failed", False),
    ],
)
def test_validate_profile_reports_session_init(
    tmp_path, monkeypatch, statements, fail_on, expected_status, expected_valid
) -> None:
    manager = _manager(tmp_path, monkeypatch, _RecordingConnector(fail_on=fail_on))

    result = manager.validate_profile(_profile(statements))

    assert result["connection_test"]["status"] == "succeeded"
    assert result["session_init"]["status"] == expected_status
    assert result["is_valid"] is expected_valid
    if expected_status == "failed":
        assert result["session_init"]["statement_index"] == 1
        assert "insufficient privileges" in result["session_init"]["error"]


# --- statements without a result set -------------------------------------------------------


def test_query_without_result_set_returns_no_rows_and_drops_the_cached_connection(tmp_path, monkeypatch) -> None:
    connector = _RecordingConnector()
    service = ExasolDashboardService.__new__(ExasolDashboardService)
    service.profile_store = SimpleNamespace(get_profile=lambda _name: _profile())
    service.connection_manager = _manager(tmp_path, monkeypatch, connector)

    result = service.execute_profile_query("semantic", "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD'")

    assert result["status"] == "ok"
    assert (result["rows"], result["columns"], result["affected_rows"]) == ([], [], 0)
    assert "session_init_sql" in result["summary"]
    # Session changes from one app's query file must not leak to others on the profile.
    assert service.connection_manager._thread_cache() == {}
    assert connector.connections[0].closed is True


# --- MCP tool ------------------------------------------------------------------------------


def _create_profile(client, **arguments: Any) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "exasol_profile_create_local",
                "arguments": {
                    "name": "semantic",
                    "backend": "onprem",
                    "credential_mode": "password",
                    "dsn": "localhost:8563",
                    "user": "sys",
                    "secret_env_var": "EXA_PASSWORD",
                    **arguments,
                },
            },
        },
    )
    return response.get_json()


def test_profile_create_stores_session_init_sql(client) -> None:
    payload = _create_profile(client, session_init_sql=[f"{ENABLE_SEMANTIC_SQL};"])
    profile = payload["result"]["structuredContent"]["profile"]
    assert profile["query_defaults"]["session_init_sql"] == [ENABLE_SEMANTIC_SQL]


def test_profile_create_without_session_init_sql_keeps_the_profile_shape(client) -> None:
    payload = _create_profile(client)
    assert "session_init_sql" not in payload["result"]["structuredContent"]["profile"]["query_defaults"]


def test_profile_create_rejects_non_session_statements(client) -> None:
    payload = _create_profile(client, session_init_sql=["DROP TABLE MART.ORDERS"])
    error = payload["result"]["structuredContent"]["error"]
    assert error["category"] == "exasol_profile_validation_error"
    assert error["details"]["field"] == "session_init_sql"
