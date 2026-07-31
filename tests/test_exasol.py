from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _call_mcp(client, method: str, params: dict[str, Any], request_id: int = 1):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
    )


def _resource_json(client, uri: str, *, request_id: int) -> dict[str, Any]:
    response = _call_mcp(client, "resources/read", {"uri": uri}, request_id=request_id)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    return json.loads(payload["result"]["contents"][0]["text"])


def _dash_callback(
    client,
    mount_path: str,
    *,
    output: str,
    outputs: dict[str, str],
    inputs: list[dict[str, Any]],
    changed_prop_ids: list[str],
    state: list[dict[str, Any]] | None = None,
):
    return client.post(
        f"{mount_path}/_dash-update-component",
        json={
            "output": output,
            "outputs": outputs,
            "inputs": inputs,
            "changedPropIds": changed_prop_ids,
            "state": state or [],
        },
    )


def _dash_callback_output_key(
    client,
    mount_path: str,
    *,
    output_ids: list[str],
) -> str:
    response = client.get(f"{mount_path}/_dash-dependencies")
    assert response.status_code == 200
    dependencies = response.get_json()
    assert isinstance(dependencies, list)
    for dependency in dependencies:
        output_key = dependency.get("output")
        if not isinstance(output_key, str):
            continue
        if all(f"{output_id}." in output_key for output_id in output_ids):
            return output_key
    raise AssertionError(f"Callback output key not found for outputs {output_ids!r}")


class _FakeExasolStatement:
    description = [
        ("SNAPSHOT_DATE", None, None, None, None, None, None),
        ("CURRENT_USER", None, None, None, None, None, None),
        ("CURRENT_SCHEMA", None, None, None, None, None, None),
    ]

    def fetchall(self):
        return [("2026-03-30", "sys", "public")]


class _FakeExasolConnection:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.executed_sql: str | None = None

    def execute(self, sql_text: str, params: dict[str, Any] | None = None):
        self.executed_sql = sql_text
        return _FakeExasolStatement()

    def close(self) -> None:
        return None


class _FakePyExasolModule:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, Any]] = []

    def connect(self, **kwargs: Any) -> _FakeExasolConnection:
        self.connect_calls.append(kwargs)
        return _FakeExasolConnection(kwargs)


class _RoutingFakeExasolStatement:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.description = [(column, None, None, None, None, None, None) for column in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows


class _RoutingFakeExasolConnection:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.executed_sql: list[str] = []

    def execute(self, sql_text: str, params: dict[str, Any] | None = None):
        self.executed_sql.append(sql_text)
        if "FROM EXA_ALL_COLUMNS" in sql_text:
            return _RoutingFakeExasolStatement(
                ["COLUMN_SCHEMA", "COLUMN_TABLE", "COLUMN_NAME", "COLUMN_TYPE", "COLUMN_ORDINAL_POSITION"],
                [
                    ("SALES", "ORDERS", "ORDER_ID", "DECIMAL(18,0)", 1),
                    ("SALES", "ORDERS", "ORDER_DATE", "TIMESTAMP", 2),
                    ("SALES", "ORDERS", "CUSTOMER_SEGMENT", "VARCHAR(40)", 3),
                    ("SALES", "ORDERS", "NET_REVENUE", "DECIMAL(18,2)", 4),
                    ("SALES", "CUSTOMERS", "CUSTOMER_ID", "DECIMAL(18,0)", 1),
                    ("SALES", "CUSTOMERS", "CUSTOMER_SEGMENT", "VARCHAR(40)", 2),
                    ("SALES", "CUSTOMERS", "COUNTRY", "VARCHAR(40)", 3),
                ],
            )
        return _FakeExasolStatement()

    def close(self) -> None:
        return None


class _RoutingFakePyExasolModule:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, Any]] = []
        self.connections: list[_RoutingFakeExasolConnection] = []

    def connect(self, **kwargs: Any) -> _RoutingFakeExasolConnection:
        self.connect_calls.append(kwargs)
        connection = _RoutingFakeExasolConnection(kwargs)
        self.connections.append(connection)
        return connection


class _SqlSmokeFakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict[str, Any]]] = []

    def execute(self, sql_text: str, params: dict[str, Any] | None = None):
        bound_params = dict(params or {})
        self.executions.append((sql_text, bound_params))
        if "MISSING_LATENCY_MS" in sql_text:
            raise RuntimeError('object "MISSING_LATENCY_MS" not found')
        return _RoutingFakeExasolStatement(["OK"], [])

    def close(self) -> None:
        return None


class _SqlSmokeFakePyExasolModule:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, Any]] = []
        self.connections: list[_SqlSmokeFakeConnection] = []

    def connect(self, **kwargs: Any) -> _SqlSmokeFakeConnection:
        self.connect_calls.append(kwargs)
        connection = _SqlSmokeFakeConnection()
        self.connections.append(connection)
        return connection


@pytest.mark.slow
def test_exasol_profile_create_validate_and_resources(app, client) -> None:
    fake_module = _FakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module

    help_payload = _resource_json(client, "dash://exasol/help/connection-modes", request_id=1)
    assert help_payload["resource"] == "dash://exasol/help/connection-modes"
    assert "exasol_profile_create_local" in help_payload["recommended_workflow"][0]
    assert any("must not embed" in rule for rule in help_payload["security_rules"])

    agent_help = _resource_json(client, "dash://exasol/help/agent-workflow", request_id=2)
    assert agent_help["resource"] == "dash://exasol/help/agent-workflow"
    assert any("external Exasol MCP server" in step for step in agent_help["recommended_workflow"])
    assert any("discovery" in rule for rule in agent_help["rules"])
    # PS26-ENV-001: every persona in the multi-persona study hit an unreachable/failing
    # external Exasol MCP server (TLS verification against a self-signed cert) and had
    # no in-repo guidance pointing at a fallback. Name app_scaffold_from_schema's own
    # introspection as the documented fallback.
    fallback = agent_help["if_external_exasol_mcp_is_unreachable"]
    assert "app_scaffold_from_schema" in fallback["fallback"]
    assert "app_scaffold_from_schema" in fallback["related_tools"]

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
                "description": "Primary analytics database.",
            },
        },
        request_id=3,
    )
    assert create_response.status_code == 200
    create_payload = create_response.get_json()["result"]["structuredContent"]
    assert create_payload["profile"]["name"] == "analytics-prod"
    assert create_payload["profile"]["secret_ref"]["provider"] == "local_file"
    assert create_payload["profile"]["secret_ref"]["exposed_value"] is False

    repo_profile_path = Path(app.config["GITOPS_REPO_PATH"]) / "profiles" / "exasol" / "analytics-prod.json"
    assert repo_profile_path.exists()
    assert "super-secret" not in repo_profile_path.read_text()

    secret_path = Path(app.config["EXASOL_SECRETS_ROOT"]) / "analytics-prod.json"
    assert secret_path.exists()
    assert "super-secret" in secret_path.read_text()

    profiles_resource = _resource_json(client, "dash://exasol/profiles", request_id=4)
    assert profiles_resource["profiles"][0]["name"] == "analytics-prod"

    single_profile_resource = _resource_json(client, "dash://exasol/profiles/analytics-prod", request_id=5)
    assert single_profile_resource["profile"]["query_defaults"]["row_limit"] == 50000

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "exasol_profile_validate", "arguments": {"name": "analytics-prod"}},
        request_id=6,
    )
    assert validate_response.status_code == 200
    validation_payload = validate_response.get_json()["result"]["structuredContent"]
    assert validation_payload["is_valid"] is True
    assert validation_payload["connection_test"]["status"] == "succeeded"
    assert fake_module.connect_calls[0]["password"] == "super-secret"
    assert "dash://exasol/help/agent-workflow" in validation_payload["guidance"]["related_resources"]


@pytest.mark.slow
def test_app_create_exasol_dashboard_creates_live_querying_app(app, client) -> None:
    fake_module = _FakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=10,
    )

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_exasol_dashboard",
            "arguments": {
                "name": "sales-overview",
                "profile_name": "analytics-prod",
                "title": "Sales Overview",
            },
        },
        request_id=11,
    )
    assert create_response.status_code == 200
    create_payload = create_response.get_json()["result"]["structuredContent"]
    assert create_payload["app"]["name"] == "sales-overview"
    assert create_payload["app"]["route"] == "/apps/sales-overview"
    assert create_payload["app"]["browser_url"].endswith("/apps/sales-overview")
    assert create_payload["exasol_profile"]["name"] == "analytics-prod"
    assert "dash://exasol/help/agent-workflow" in create_payload["guidance"]["related_resources"]

    homepage = client.get("/apps/sales-overview")
    assert homepage.status_code == 200
    assert b"Sales Overview" in homepage.data

    manifest_resource = _resource_json(client, "dash://apps/sales-overview/manifest", request_id=12)
    assert manifest_resource["manifest"]["template"] == "exasol-analytics"
    assert manifest_resource["manifest"]["data_sources"]["primary"]["profile"] == "analytics-prod"

    files_resource = _resource_json(client, "dash://apps/sales-overview/files", request_id=13)
    assert "dash_server_exasol.py" in files_resource["draft"]["files"]
    assert "queries/system/meta.sql" in files_resource["draft"]["files"]
    assert "queries/system/sql_hist.sql" in files_resource["draft"]["files"]
    assert "queries/business/summary.sql" in files_resource["draft"]["files"]
    assert "queries/business/detail.sql" in files_resource["draft"]["files"]
    schema_notes_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "sales-overview", "path": "app.py"}},
        request_id=14,
    )
    app_py_text = schema_notes_response.get_json()["result"]["structuredContent"]["content"]
    assert "System Health" in app_py_text
    assert "Business Analytics" in app_py_text

    callback_output = _dash_callback_output_key(
        client,
        "/apps/sales-overview",
        output_ids=["health-summary", "business-table"],
    )
    callback_response = _dash_callback(
        client,
        "/apps/sales-overview",
        output=callback_output,
        outputs=[
            {"id": "health-caption", "property": "children"},
            {"id": "health-summary", "property": "children"},
            {"id": "monitor-chart", "property": "figure"},
            {"id": "usage-chart", "property": "figure"},
            {"id": "sql-history-table", "property": "children"},
            {"id": "business-summary", "property": "children"},
            {"id": "business-chart", "property": "figure"},
            {"id": "business-table", "property": "children"},
        ],
        inputs=[
            {"id": "refresh", "property": "n_intervals", "value": 1},
        ],
        changed_prop_ids=["refresh.n_intervals"],
    )
    assert callback_response.status_code == 200
    response_text = callback_response.get_data(as_text=True)
    assert "CURRENT_USER" in response_text
    assert "sys" in response_text
    assert "CURRENT_SCHEMA" in response_text

    profile_list_response = _call_mcp(
        client,
        "tools/call",
        {"name": "exasol_profiles_list", "arguments": {}},
        request_id=14,
    )
    assert profile_list_response.status_code == 200
    assert "analytics-prod" in profile_list_response.get_json()["result"]["structuredContent"]["profiles"][0]["name"]


@pytest.mark.slow
def test_exasol_authoring_guidance_and_validation_block_embedded_credentials(client) -> None:
    authoring_guide = _resource_json(client, "dash://meta/app-authoring-guide", request_id=30)
    assert any(
        "Do not embed database credentials" in rule for rule in authoring_guide["required_rules"]
    )
    assert any("pyexasol.connect" in rule for rule in authoring_guide["required_rules"])

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "unsafe-exasol",
                "title": "Unsafe Exasol",
                "files": [
                    {
                        "path": "dash-app.json",
                        "content": json.dumps(
                            {
                                "name": "unsafe-exasol",
                                "title": "Unsafe Exasol",
                                "route": "/apps/unsafe-exasol",
                                "description": "Unsafe Exasol app.",
                                "template": "exasol-analytics",
                                "data_sources": {
                                    "primary": {
                                        "kind": "exasol",
                                        "profile": "analytics-prod",
                                        "auth_mode": "local_direct",
                                    }
                                },
                            },
                            indent=2,
                        )
                        + "\n",
                    },
                    {
                        "path": "app.py",
                        "content": (
                            "import os\n"
                            "import pyexasol\n"
                            "from dash import Dash, html\n\n"
                            "def create_dash_app(server, url_base_pathname, metadata):\n"
                            "    password = os.environ.get('EXASOL_PASS', 'exasol')\n"
                            "    pyexasol.connect(dsn='127.0.0.1:8563', user='sys', password=password)\n"
                            "    app = Dash(__name__, server=server, routes_pathname_prefix='/', requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                            "    app.layout = html.Div('unsafe')\n"
                            "    return app\n"
                        ),
                    },
                    {"path": "requirements.txt", "content": "dash>=4.0,<5.0\npyexasol>=2.2.2,<3.0\n"},
                ],
            },
        },
        request_id=31,
    )
    assert create_response.status_code == 200
    create_result = create_response.get_json()["result"]
    assert create_result["isError"] is True
    create_payload = create_result["structuredContent"]
    validation_payload = create_payload["error"]["details"]["validation"]
    assert validation_payload["is_valid"] is False
    assert validation_payload["credential_safety"]["status"] == "failed"
    messages = [finding["message"] for finding in validation_payload["credential_safety"]["findings"]]
    assert any("pyexasol.connect" in message for message in messages)
    assert any("EXA_/EXASOL_" in message for message in messages)


@pytest.mark.slow
def test_exasol_patterns_help_and_kpi_trend_scaffold(app, client) -> None:
    fake_module = _FakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module

    patterns = _resource_json(client, "dash://exasol/help/dashboard-patterns", request_id=40)
    assert patterns["resource"] == "dash://exasol/help/dashboard-patterns"
    assert any(item["name"] == "analytics-hub" for item in patterns["patterns"])
    assert any(item["name"] == "kpi-trend" for item in patterns["patterns"])

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=41,
    )

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_exasol_dashboard",
            "arguments": {
                "name": "revenue-pulse",
                "profile_name": "analytics-prod",
                "pattern": "kpi-trend",
                "title": "Revenue Pulse",
            },
        },
        request_id=42,
    )
    assert create_response.status_code == 200
    create_payload = create_response.get_json()["result"]["structuredContent"]
    assert create_payload["app"]["name"] == "revenue-pulse"

    files_resource = _resource_json(client, "dash://apps/revenue-pulse/files", request_id=43)
    assert "queries/summary.sql" in files_resource["draft"]["files"]
    assert "queries/trend.sql" in files_resource["draft"]["files"]
    assert "queries/detail.sql" in files_resource["draft"]["files"]

    app_py_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "revenue-pulse", "path": "app.py"}},
        request_id=44,
    )
    assert app_py_response.status_code == 200
    app_py_text = app_py_response.get_json()["result"]["structuredContent"]["content"]
    assert "queries/trend.sql" in app_py_text
    assert "load_row" in app_py_text
    assert "load_rows" in app_py_text

    requirements_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "revenue-pulse", "path": "requirements.txt"}},
        request_id=45,
    )
    assert requirements_response.status_code == 200
    requirements_text = requirements_response.get_json()["result"]["structuredContent"]["content"]
    assert "pyexasol>=2.2.2,<3.0" in requirements_text
    assert "pyexasol>=1.0,<2.0" not in requirements_text


@pytest.mark.slow
def test_app_scaffold_from_schema_generates_schema_specific_bundle(app, client) -> None:
    fake_module = _RoutingFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=60,
    )

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_scaffold_from_schema",
            "arguments": {
                "name": "sales-schema",
                "profile_name": "analytics-prod",
                "schema_name": "SALES",
                "title": "Sales Schema",
            },
        },
        request_id=61,
    )
    assert create_response.status_code == 200
    payload = create_response.get_json()["result"]["structuredContent"]
    assert payload["schema_blueprint"]["schema_name"] == "SALES"
    assert payload["schema_blueprint"]["table_name"] == "ORDERS"
    assert payload["schema_blueprint"]["primary_measure"] == "NET_REVENUE"
    assert payload["schema_blueprint"]["time_column"] == "ORDER_DATE"

    files_resource = _resource_json(client, "dash://apps/sales-schema/files", request_id=62)
    assert "SCHEMA_NOTES.md" in files_resource["draft"]["files"]
    assert "queries/business/summary.sql" in files_resource["draft"]["files"]
    assert "queries/business/trend.sql" in files_resource["draft"]["files"]

    summary_sql_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "sales-schema", "path": "queries/business/summary.sql"}},
        request_id=63,
    )
    summary_sql = summary_sql_response.get_json()["result"]["structuredContent"]["content"]
    assert '"SALES"."ORDERS"' in summary_sql
    assert '"NET_REVENUE"' in summary_sql

    notes_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "sales-schema", "path": "SCHEMA_NOTES.md"}},
        request_id=64,
    )
    notes_text = notes_response.get_json()["result"]["structuredContent"]["content"]
    assert "SALES.ORDERS" in notes_text
    assert "NET_REVENUE" in notes_text

    requirements_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_read_file", "arguments": {"name": "sales-schema", "path": "requirements.txt"}},
        request_id=65,
    )
    assert requirements_response.status_code == 200
    requirements_text = requirements_response.get_json()["result"]["structuredContent"]["content"]
    assert "pyexasol>=2.2.2,<3.0" in requirements_text
    assert "pyexasol>=1.0,<2.0" not in requirements_text


class _HeuristicFakeExasolStatement:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.description = [(column, None, None, None, None, None, None) for column in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows


class _HeuristicFakeExasolConnection:
    def __init__(self) -> None:
        self.executed_sql: list[str] = []

    def execute(self, sql_text: str, params: dict[str, Any] | None = None):
        self.executed_sql.append(sql_text)
        # PS26-BUG-008/009 fixture: a platform-internal decoy schema/table named to
        # score well on an "agent" keyword scorer, plus the real target schema with a
        # LAT/LON pair alongside a genuine measure column, plus a second real table
        # sharing a key column with the target (for relationship_hints).
        return _HeuristicFakeExasolStatement(
            ["COLUMN_SCHEMA", "COLUMN_TABLE", "COLUMN_NAME", "COLUMN_TYPE", "COLUMN_ORDINAL_POSITION"],
            [
                ("SEMANTIC_AGENT", "INSTRUCTIONS_FOR_AGENT", "AGENT_ID", "DECIMAL(18,0)", 1),
                ("SEMANTIC_AGENT", "INSTRUCTIONS_FOR_AGENT", "VERSION_NUMBER", "DECIMAL(18,0)", 2),
                ("SEMANTIC_AGENT", "INSTRUCTIONS_FOR_AGENT", "CREATED_AT", "TIMESTAMP", 3),
                ("SENSORS", "READINGS", "READING_ID", "DECIMAL(18,0)", 1),
                ("SENSORS", "READINGS", "SENSOR_ID", "DECIMAL(18,0)", 2),
                ("SENSORS", "READINGS", "READING_TS", "TIMESTAMP", 3),
                ("SENSORS", "READINGS", "LAT", "DECIMAL(9,6)", 4),
                ("SENSORS", "READINGS", "LON", "DECIMAL(9,6)", 5),
                ("SENSORS", "READINGS", "TEMPERATURE_C", "DECIMAL(6,2)", 6),
                ("SENSORS", "STATIONS", "SENSOR_ID", "DECIMAL(18,0)", 1),
                ("SENSORS", "STATIONS", "STATION_NAME", "VARCHAR(40)", 2),
            ],
        )

    def close(self) -> None:
        return None


class _HeuristicFakePyExasolModule:
    def connect(self, **kwargs: Any) -> _HeuristicFakeExasolConnection:
        return _HeuristicFakeExasolConnection()


@pytest.mark.slow
def test_ps26_bug008_blind_auto_pick_ignores_platform_internal_schemas(app, client) -> None:
    """PS26-BUG-008 regression: `app_scaffold_from_schema` with no explicit
    `schema_name`/`table_name` used to be able to select a `SEMANTIC_*` platform
    schema over the real target purely because its table name matched an
    "agent"-shaped keyword scorer, with every `table_candidates` entry drawn from
    that same platform schema. Blind auto-pick must exclude platform-internal
    schemas from candidacy entirely.
    """

    fake_module = _HeuristicFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=70,
    )

    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_scaffold_from_schema",
            "arguments": {"name": "blind-pick", "profile_name": "analytics-prod", "title": "Blind Pick"},
        },
        request_id=71,
    )
    assert response.get_json()["result"]["isError"] is False
    blueprint = response.get_json()["result"]["structuredContent"]["schema_blueprint"]

    assert blueprint["schema_name"] != "SEMANTIC_AGENT"
    assert blueprint["schema_name"] == "SENSORS"
    assert blueprint["table_name"] == "READINGS"
    assert all(
        candidate["schema_name"] != "SEMANTIC_AGENT" for candidate in blueprint["table_candidates"]
    )


@pytest.mark.slow
def test_ps26_bug009_measure_scoring_excludes_lat_lon_and_relationship_hints_skip_self(app, client) -> None:
    """PS26-BUG-009 regression, two facets in one fixture:
    1. `primary_measure` must not pick a LAT/LON coordinate column even when it's the
       first numeric, non-key column encountered (a real measure, TEMPERATURE_C, exists
       on the same table).
    2. `relationship_hints` must exclude the *selected* table itself, not just whichever
       table happened to rank first - triggered here via an explicit `table_name`
       override that does not match the top-ranked candidate.
    """

    fake_module = _HeuristicFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=72,
    )

    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_scaffold_from_schema",
            "arguments": {
                "name": "measure-pick",
                "profile_name": "analytics-prod",
                "schema_name": "SENSORS",
                "table_name": "READINGS",
                "title": "Measure Pick",
            },
        },
        request_id=73,
    )
    assert response.get_json()["result"]["isError"] is False
    blueprint = response.get_json()["result"]["structuredContent"]["schema_blueprint"]

    assert blueprint["primary_measure"] == "TEMPERATURE_C"
    assert "LAT" not in blueprint["measure_columns"]
    assert "LON" not in blueprint["measure_columns"]

    hints = blueprint["relationship_hints"]
    assert not any(hint["other_table"] == "READINGS" for hint in hints)
    assert any(hint["other_table"] == "STATIONS" for hint in hints)


def test_ps26_bug012_analytics_hub_surfaces_real_query_errors_instead_of_generic_empty_message() -> None:
    """PS26-BUG-012 regression: the generated analytics-hub `app.py` used to check
    ``"_error" not in monitor_rows[0]`` and, on failure, collapse straight to a generic
    "No monitor/usage data available." message - indistinguishable from the query
    legitimately returning zero rows. An operator watching System Health would never
    learn Exasol actually failed. The fix routes every check through the documented
    `has_error()` contract helper and surfaces the real `_error` text for monitor.sql
    and usage.sql failures, matching the three-way handling `business_trend` already had.
    """

    from dash_server.exasol import scaffold

    bundle = scaffold.build_exasol_dashboard_bundle(
        app_name="bug012-check",
        title="Bug012 Check",
        route="/bug012-check",
        description="Regression fixture for PS26-BUG-012.",
        profile_name="golden-profile",
        pattern="analytics-hub",
    )
    app_py = next(f["content"] for f in bundle["files"] if f["path"] == "app.py")

    compile(app_py, "app.py", "exec")

    assert "has_error = _HELPER_MODULE.has_error" in app_py

    # The old buggy inline checks must be gone - they silently swallowed the real error.
    assert '"_error" not in monitor_rows[0]' not in app_py
    assert '"_error" not in usage_rows[0]' not in app_py
    assert '"_error" in business_summary' not in app_py

    # Every branch now goes through the documented has_error() helper...
    assert "has_error(monitor_rows)" in app_py
    assert "has_error(usage_rows)" in app_py
    assert "has_error(business_trend)" in app_py
    assert "has_error(sql_rows)" in app_py
    assert "has_error(business_summary)" in app_py

    # ...and a real query failure now surfaces its own error text, not a generic message.
    assert "Exasol query failed for queries/system/monitor.sql: {monitor_rows[0]['_error']}" in app_py
    assert "Exasol query failed for queries/system/usage.sql: {usage_rows[0]['_error']}" in app_py


@pytest.mark.slow
def test_exasol_validation_flags_import_time_queries_and_risky_sql(client) -> None:
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "risky-exasol",
                "title": "Risky Exasol",
                "files": [
                    {
                        "path": "dash-app.json",
                        "content": json.dumps(
                            {
                                "name": "risky-exasol",
                                "title": "Risky Exasol",
                                "route": "/apps/risky-exasol",
                                "description": "Risky Exasol app.",
                                "template": "exasol-analytics",
                                "data_sources": {
                                    "primary": {
                                        "kind": "exasol",
                                        "profile": "analytics-prod",
                                        "auth_mode": "local_direct",
                                    }
                                },
                            },
                            indent=2,
                        )
                        + "\n",
                    },
                    {
                        "path": "app.py",
                        "content": (
                            "from dash import Dash, html\n"
                            "from dash_server.exasol.runtime import query_rows\n\n"
                            "BOOT = query_rows(None, {'data_sources': {'primary': {'profile': 'analytics-prod'}}}, base_dir='.', sql_relative_path='queries/detail.sql')\n\n"
                            "def create_dash_app(server, url_base_pathname, metadata):\n"
                            "    app = Dash(__name__, server=server, routes_pathname_prefix='/', requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                            "    app.layout = html.Div('risky')\n"
                            "    return app\n"
                        ),
                    },
                    {"path": "queries/detail.sql", "content": "SELECT * FROM DUAL\n"},
                    {"path": "requirements.txt", "content": "dash>=4.0,<5.0\n"},
                ],
            },
        },
        request_id=50,
    )
    assert create_response.status_code == 200
    create_result = create_response.get_json()["result"]
    assert create_result["isError"] is True
    validation_payload = create_result["structuredContent"]["error"]["details"]["validation"]
    assert validation_payload["exasol"]["status"] == "failed"
    exasol_messages = [issue["message"] for issue in validation_payload["exasol"]["issues"]]
    assert any("import time" in message for message in exasol_messages)
    assert any("SELECT *" in message for message in exasol_messages)


def test_ps26_bug010_query_directly_in_create_dash_app_body_gets_an_actionable_error(client) -> None:
    """PS26-BUG-010 regression: a query run directly in `create_dash_app()`'s body
    (not inside an `@app.callback`, and not a top-level module statement either, so
    the `exasol` lint's import-time-query check doesn't catch it) used to fail
    `app_validate` with a generic `import_error` reading
    'Exasol dashboard service is not registered on the Flask server' - indistinguishable
    from a genuinely broken/unbound profile. It must now name the real fix.
    """

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "eager-exasol",
                "title": "Eager Exasol",
                "files": [
                    {
                        "path": "dash-app.json",
                        "content": json.dumps(
                            {
                                "name": "eager-exasol",
                                "title": "Eager Exasol",
                                "route": "/apps/eager-exasol",
                                "description": "Eager Exasol app.",
                                "template": "exasol-analytics",
                                "data_sources": {
                                    "primary": {
                                        "kind": "exasol",
                                        "profile": "analytics-prod",
                                        "auth_mode": "local_direct",
                                    }
                                },
                            },
                            indent=2,
                        )
                        + "\n",
                    },
                    {
                        "path": "app.py",
                        "content": (
                            "from dash import Dash, html\n"
                            "from dash_server.exasol.runtime import query_rows\n\n"
                            "def create_dash_app(server, url_base_pathname, metadata):\n"
                            "    rows = query_rows(server, metadata, base_dir='.', "
                            "sql_relative_path='queries/detail.sql')\n"
                            "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
                            "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                            "    app.layout = html.Div(str(rows))\n"
                            "    return app\n"
                        ),
                    },
                    {"path": "queries/detail.sql", "content": "SELECT * FROM DUAL\n"},
                    {"path": "requirements.txt", "content": "dash>=4.0,<5.0\n"},
                ],
            },
        },
        request_id=51,
    )
    assert create_response.status_code == 200
    create_result = create_response.get_json()["result"]
    assert create_result["isError"] is True
    validation_payload = create_result["structuredContent"]["error"]["details"]["validation"]
    # This call site isn't a top-level module statement, so the `exasol` lint's
    # static import-time check doesn't fire - the failure must come from `imports`.
    assert validation_payload["exasol"]["status"] != "failed"
    assert validation_payload["imports"]["status"] == "failed"
    assert validation_payload["imports"]["category"] == "exasol_query_outside_callback"
    error_text = validation_payload["imports"]["error"]
    assert "not registered on the Flask server" in error_text
    assert "not a broken Exasol profile" in error_text
    assert "@app.callback" in error_text


# --- Regression tests for the persona study (BUG-001, BUG-002, BUG-003) ---

import ssl

from dash_server.exasol.connection_manager import (
    ExasolConnectionManager,
    _classify_connection_error,
)
from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.exasol.secrets import ExasolSecretStore


class _ConnectKwargsCapture:
    """Pretend connector that records the kwargs pyexasol.connect would receive."""

    def __init__(self) -> None:
        self.connect_calls: list[dict[str, Any]] = []

    def connect(self, **kwargs: Any):
        self.connect_calls.append(kwargs)

        class _Conn:
            def close(self) -> None:
                return None

        return _Conn()


def _profile(*, tls_verify: bool, query_defaults: dict[str, Any] | None = None) -> ExasolProfile:
    return ExasolProfile(
        name="local-test",
        backend="onprem",
        deployment_mode="local_direct",
        credential_mode="password",
        user="sys",
        dsn="localhost:8563",
        description="study test profile",
        tls_verify=tls_verify,
        secret_ref=ExasolSecretRef(provider="env", key="EXA_PASSWORD"),
        query_defaults=query_defaults,
    )


class _CacheProbeStatement:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _CacheProbeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.statements: list[_CacheProbeStatement] = []

    def execute(self, sql_text: str, params: dict[str, Any] | None = None) -> _CacheProbeStatement:
        if self.closed:
            raise RuntimeError("Exasol connection was closed")
        self.executed.append((sql_text, dict(params or {})))
        statement = _CacheProbeStatement()
        self.statements.append(statement)
        return statement

    def close(self) -> None:
        self.closed = True


class _CacheProbeConnector:
    def __init__(self) -> None:
        self.connections: list[_CacheProbeConnection] = []

    def connect(self, **_kwargs: Any) -> _CacheProbeConnection:
        connection = _CacheProbeConnection()
        self.connections.append(connection)
        return connection


def test_study_bug003_sql_smoke_does_not_close_cached_runtime_connection(tmp_path, monkeypatch) -> None:
    from dash_server.exasol.sql_smoke import run_sql_smoke

    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _CacheProbeConnector()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False)

    cached = cm.connect(profile)
    report = run_sql_smoke(
        profile=profile,
        sql_files=[("queries/summary.sql", "SELECT 1 AS OK FROM DUAL")],
        connection_manager=cm,
    )

    assert report.overall_status == "passed"
    assert len(fake.connections) == 2
    smoke_connection = fake.connections[1]
    assert smoke_connection.closed is True
    assert cached.closed is False
    assert cm.connect(profile) is cached
    cached.execute("SELECT 1 AS STILL_OPEN FROM DUAL")


def test_bug001_tls_verify_false_keeps_encryption_on_and_disables_cert_check(tmp_path, monkeypatch) -> None:
    """BUG-001 regression: tls_verify=false must still negotiate TLS, only skip cert verify."""
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False)

    result = cm.validate_profile(profile)
    assert result["is_valid"] is True
    assert len(fake.connect_calls) == 1
    kwargs = fake.connect_calls[0]
    assert kwargs["encryption"] is True, "encryption must be True even when tls_verify=False"
    assert kwargs["websocket_sslopt"] == {"cert_reqs": ssl.CERT_NONE}


def test_bug001_tls_verify_true_requires_cert(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )

    result = cm.validate_profile(_profile(tls_verify=True))
    assert result["is_valid"] is True
    assert fake.connect_calls[0]["encryption"] is True
    assert fake.connect_calls[0]["websocket_sslopt"] == {"cert_reqs": ssl.CERT_REQUIRED}


def test_bug001_only_tls_error_gets_friendly_hint() -> None:
    classified = _classify_connection_error(
        "(\n    message     =>  Connection exception - Only TLS connections are allowed.\n    code        =>  08004\n)"
    )
    assert classified is not None
    assert classified["kind"] == "tls_required"
    assert "encryption=True" in classified["hint"] or "encryption" in classified["hint"].lower()


def test_bug001_self_signed_cert_error_gets_friendly_hint() -> None:
    classified = _classify_connection_error(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    assert classified is not None
    assert classified["kind"] == "tls_cert_verify_failed"
    assert "tls_verify=false" in classified["hint"]


def test_ps26_bug001_cached_connect_passes_profile_statement_timeout_to_pyexasol(
    tmp_path, monkeypatch
) -> None:
    """PS26-BUG-001 regression: `query_defaults.statement_timeout_seconds` used to be
    stored and displayed but never reached `pyexasol.connect(...)` for the runtime/
    callback path (`ExasolConnectionManager.connect`) — only one-shot uncached probe
    callers that explicitly passed `query_timeout_seconds` got it. A profile with
    `statement_timeout_seconds` configured must now have every *cached* connection
    (the one every live Dash callback query reuses) opened with pyexasol's
    `query_timeout` set to that value.
    """
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False, query_defaults={"statement_timeout_seconds": 12, "row_limit": 50000})

    connection = cm.connect(profile)
    assert connection is cm.connect(profile), "second connect() must reuse the cached connection"
    assert len(fake.connect_calls) == 1, "the cache must prevent a second pyexasol.connect(...) call"
    assert fake.connect_calls[0]["query_timeout"] == 12


def test_ps26_bug001_cached_connect_omits_query_timeout_when_profile_has_none(tmp_path, monkeypatch) -> None:
    """A profile with no `statement_timeout_seconds` must not invent one — pyexasol's
    own default (no timeout) applies, matching pre-fix behavior for profiles that
    never configured this.
    """
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False, query_defaults=None)

    cm.connect(profile)
    assert "query_timeout" not in fake.connect_calls[0]


def test_ps26_bug001_statement_timeout_seconds_coerces_and_tolerates_bad_values() -> None:
    good = _profile(tls_verify=False, query_defaults={"statement_timeout_seconds": "45"})
    assert ExasolConnectionManager.statement_timeout_seconds(good) == 45

    missing = _profile(tls_verify=False, query_defaults={})
    assert ExasolConnectionManager.statement_timeout_seconds(missing) is None

    bogus = _profile(tls_verify=False, query_defaults={"statement_timeout_seconds": "not-a-number"})
    assert ExasolConnectionManager.statement_timeout_seconds(bogus) is None

    none_profile = _profile(tls_verify=False, query_defaults=None)
    assert ExasolConnectionManager.statement_timeout_seconds(none_profile) is None


def test_ps27_bug001_connection_idle_timeout_seconds_coerces_and_tolerates_bad_values() -> None:
    good = _profile(tls_verify=False, query_defaults={"connection_idle_timeout_seconds": 45})
    assert ExasolConnectionManager.connection_idle_timeout_seconds(good) == 45

    missing = _profile(tls_verify=False, query_defaults={})
    assert ExasolConnectionManager.connection_idle_timeout_seconds(missing) == 300

    bogus = _profile(tls_verify=False, query_defaults={"connection_idle_timeout_seconds": "not-a-number"})
    assert ExasolConnectionManager.connection_idle_timeout_seconds(bogus) == 300

    none_profile = _profile(tls_verify=False, query_defaults=None)
    assert ExasolConnectionManager.connection_idle_timeout_seconds(none_profile) == 300


def test_ps27_bug001_connect_evicts_a_connection_idle_past_the_timeout(tmp_path, monkeypatch) -> None:
    """PS27-BUG-001 regression: `ExasolConnectionManager.connect()` used to cache one
    connection per (profile, thread) forever, releasing it only via incidental
    error-triggered invalidation - never proactively. Under real concurrent load this
    exhausted Exasol Personal's connection license for 25-30 minutes at a stretch. A
    cached connection idle longer than `connection_idle_timeout_seconds` must now be
    closed and replaced by the next `connect()` call rather than reused.
    """
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False, query_defaults={"connection_idle_timeout_seconds": 5})

    fake_now = [1000.0]
    monkeypatch.setattr("dash_server.exasol.connection_manager.time.monotonic", lambda: fake_now[0])

    first = cm.connect(profile)
    assert len(fake.connect_calls) == 1

    fake_now[0] += 2  # well within the 5s idle timeout
    assert cm.connect(profile) is first
    assert len(fake.connect_calls) == 1, "still-fresh connection must be reused, not reopened"

    fake_now[0] += 10  # now past the 5s idle timeout
    second = cm.connect(profile)
    assert second is not first, "an idle-past-timeout connection must be evicted and replaced"
    assert len(fake.connect_calls) == 2


def test_ps27_bug001_connect_refreshes_last_used_on_every_reuse(tmp_path, monkeypatch) -> None:
    """A connection must not be evicted just because it was opened long ago - only
    because it has been *unused* longer than the idle timeout. Touching it via
    `connect()` resets the idle clock.
    """
    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False, query_defaults={"connection_idle_timeout_seconds": 5})

    fake_now = [1000.0]
    monkeypatch.setattr("dash_server.exasol.connection_manager.time.monotonic", lambda: fake_now[0])

    first = cm.connect(profile)
    for _ in range(4):
        fake_now[0] += 3  # each step is under the timeout, but 4 steps sum to more than it
        assert cm.connect(profile) is first
    assert len(fake.connect_calls) == 1, "repeated use within the timeout must never evict"


def test_ps26_bug001_sql_smoke_passes_profile_statement_timeout_to_uncached_connect(tmp_path, monkeypatch) -> None:
    """The SQL-smoke preflight's one-shot probe connection should honor the same
    configured timeout as the runtime path, so a query that would hang forever in a
    live callback doesn't also hang forever during preflight.
    """
    from dash_server.exasol.sql_smoke import run_sql_smoke

    monkeypatch.setenv("EXA_PASSWORD", "test")
    fake = _ConnectKwargsCapture()
    cm = ExasolConnectionManager(
        ExasolSecretStore(str(tmp_path)),
        connector_loader=lambda: fake,
    )
    profile = _profile(tls_verify=False, query_defaults={"statement_timeout_seconds": 9})

    run_sql_smoke(
        profile=profile,
        sql_files=[("queries/summary.sql", "SELECT 1 AS OK FROM DUAL")],
        connection_manager=cm,
    )

    assert len(fake.connect_calls) == 1
    assert fake.connect_calls[0]["query_timeout"] == 9


class _PyexasolStyleStatement:
    """Mimic pyexasol.ExaStatement: column_names() and columns() are methods, description is None."""

    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self._columns = columns
        self._rows = rows
        self.description = None

    def column_names(self) -> list[str]:
        return list(self._columns)

    def columns(self) -> dict[str, dict[str, Any]]:
        return {c: {"type": "VARCHAR"} for c in self._columns}

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class _FetchManyStatement:
    """Statement double that supports `fetchmany` and asserts `fetchall` is never
    called when it doesn't have to be — the whole point of PS26-BUG-004's fix.
    """

    def __init__(self, rows: list[list[Any]]) -> None:
        self._rows = rows
        self.fetchmany_calls: list[int] = []

    def fetchmany(self, size: int) -> list[list[Any]]:
        self.fetchmany_calls.append(size)
        return self._rows[:size]

    def fetchall(self) -> list[list[Any]]:
        raise AssertionError(
            "fetchall() must not be called when fetchmany() is available (PS26-BUG-004)"
        )

    def close(self) -> None:
        return None


def test_ps26_bug004_fetch_bounded_rows_stops_at_row_limit_plus_one_via_fetchmany() -> None:
    """PS26-BUG-004 regression: the previous implementation always called
    `statement.fetchall()`, pulling an unbounded result set into Python before
    slicing to `row_limit` — meaning a query with no SQL-level LIMIT (e.g. an
    accidental cross join) cost dash-server the full result size regardless of
    `row_limit`. `_fetch_bounded_rows` must instead request only `row_limit + 1`
    rows via `fetchmany` (the `+1` is exactly enough to detect truncation) and
    never fall back to `fetchall()` when `fetchmany` exists.
    """
    from dash_server.exasol.service import ExasolDashboardService

    svc = ExasolDashboardService.__new__(ExasolDashboardService)
    # 10,000 "rows" available — a stand-in for an unbounded cross-join result.
    # If the fix regresses to fetchall()-then-slice, this statement's fetchall()
    # will raise and fail the test outright rather than just returning a wrong count.
    statement = _FetchManyStatement([[i] for i in range(10_000)])

    rows, truncated = svc._fetch_bounded_rows(statement, row_limit=50)

    assert statement.fetchmany_calls == [51], "must request exactly row_limit + 1 rows, not the full result"
    assert len(rows) == 50
    assert truncated is True


def test_ps26_bug004_fetch_bounded_rows_not_truncated_when_result_fits_under_limit() -> None:
    from dash_server.exasol.service import ExasolDashboardService

    svc = ExasolDashboardService.__new__(ExasolDashboardService)
    statement = _FetchManyStatement([[i] for i in range(5)])

    rows, truncated = svc._fetch_bounded_rows(statement, row_limit=50)

    assert statement.fetchmany_calls == [51]
    assert len(rows) == 5
    assert truncated is False


def test_ps26_bug004_fetch_bounded_rows_falls_back_to_fetchall_without_fetchmany() -> None:
    """Statement doubles (and, per the module docstring, potentially older/other
    driver shapes) that only implement `fetchall` must keep working exactly as
    before the fix - this is the explicit fallback path, not an oversight.
    """
    from dash_server.exasol.service import ExasolDashboardService

    svc = ExasolDashboardService.__new__(ExasolDashboardService)
    statement = _FakeExasolStatement()  # only fetchall(); one row

    rows, truncated = svc._fetch_bounded_rows(statement, row_limit=50)

    assert rows == [["2026-03-30", "sys", "public"]]
    assert truncated is False


def test_bug002_extract_columns_handles_pyexasol_method_api() -> None:
    from dash_server.exasol.service import ExasolDashboardService

    svc = ExasolDashboardService.__new__(ExasolDashboardService)
    statement = _PyexasolStyleStatement(
        columns=["PARAM_NAME", "PARAM_VALUE"],
        rows=[("databaseProductVersion", "2026.1.0"), ("maxConnections", "20")],
    )
    columns = svc._extract_columns(statement)
    assert columns == ["PARAM_NAME", "PARAM_VALUE"], (
        "BUG-002: pyexasol exposes column_names() as a method, not as a list"
    )


def test_bug002_records_from_result_zips_columns_and_rows() -> None:
    from dash_server.exasol.runtime import _records_from_result

    result = {
        "status": "ok",
        "columns": ["REGION", "REV"],
        "rows": [["EMEA", 1075354], ["NA", 1043420]],
    }
    records = _records_from_result(result)
    assert records == [
        {"REGION": "EMEA", "REV": 1075354},
        {"REGION": "NA", "REV": 1043420},
    ]


def test_bug003_introspection_sql_uses_pyexasol_native_placeholders() -> None:
    """BUG-003: schema introspection must not use :name (Exasol rejects it as host parameter)."""
    import inspect
    from dash_server.exasol import service as svc_module

    source = inspect.getsource(svc_module.ExasolDashboardService._discover_schema_blueprint)
    assert ":schema_name" not in source, (
        "BUG-003: introspection SQL used :name placeholder; switch to pyexasol's {name!s} syntax"
    )
    assert "{schema_name!s}" in source or "{schema_name!q}" in source


# --- Phase 3 regression tests (BUG-007, BUG-008, BUG-009) ---


def test_bug007_preview_path_accepts_both_bare_and_r000_form() -> None:
    from dash_server.runtime.dispatcher import _PREVIEW_REVISION_ALIAS_RE

    # Bare numbers are not rewritten (no `r` prefix).
    assert _PREVIEW_REVISION_ALIAS_RE.match("/preview/exec-sales/7") is None
    assert _PREVIEW_REVISION_ALIAS_RE.match("/preview/exec-sales/7/_dash-layout") is None

    # r000NNN form is rewritten back to bare number form.
    match = _PREVIEW_REVISION_ALIAS_RE.match("/preview/exec-sales/r000007/_dash-layout")
    assert match is not None
    assert match.group(1) == "/preview/exec-sales/"
    assert match.group(2) == "7"
    assert match.group(3) == "/_dash-layout"

    match = _PREVIEW_REVISION_ALIAS_RE.match("/preview/agent-watch/r000042")
    assert match is not None and match.group(2) == "42"


@pytest.mark.slow
def test_bug008_unknown_tool_argument_returns_invalid_arguments_error(app, client) -> None:
    """BUG-008 regression: unknown arguments to tools/call must produce -32602, not be silently dropped."""
    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "demo", "target": "preview"},  # `target` is wrong; real arg is deployment_target
        },
        request_id=900,
    )
    payload = response.get_json()
    assert payload["result"]["isError"] is True
    error = payload["result"]["structuredContent"]["error"]
    # The dispatcher rejects with -32602 invalid_arguments.
    assert error["category"] in {"tool_validation_error", "invalid_arguments"}
    assert "target" in error["summary"] or "deployment_target" in error["summary"]


@pytest.mark.slow
def test_bug008_camel_case_force_clean_is_rejected(app, client) -> None:
    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_build",
            "arguments": {"name": "demo", "forceClean": True},
        },
        request_id=901,
    )
    payload = response.get_json()
    assert payload["result"]["isError"] is True
    error = payload["result"]["structuredContent"]["error"]
    assert error["category"] in {"tool_validation_error", "invalid_arguments"}
    assert "forceClean" in error["summary"] or "force_clean" in error["summary"].lower()


def test_bug009_app_scaffold_from_schema_schema_includes_table_name() -> None:
    """BUG-009 regression: scaffold-from-schema accepts a table_name argument."""
    from dash_server.mcp.server import MCPServer

    server = MCPServer.__new__(MCPServer)
    schema = server._app_scaffold_from_schema_schema()
    properties = schema.get("properties", {})
    assert "table_name" in properties
    assert properties["table_name"]["type"] == "string"


def test_data_layer_error_recording_with_rate_limit(tmp_path) -> None:
    """When the runtime hits a SQL error, diagnostics.record_data_layer_error captures it (rate-limited)."""
    from dash_server.diagnostics.service import DiagnosticsService

    svc = DiagnosticsService(str(tmp_path))
    first = svc.record_data_layer_error(
        "exec-sales",
        sql_file="queries/business/trend.sql",
        profile_name="analytics-prod",
        error_text="object REVENUEZ not found",
    )
    assert first is not None
    assert first["source"] == "data_layer"
    assert first["category"] == "exasol_query_error"
    # Within the rate-limit window, the same error is dropped.
    second = svc.record_data_layer_error(
        "exec-sales",
        sql_file="queries/business/trend.sql",
        profile_name="analytics-prod",
        error_text="object REVENUEZ not found",
    )
    assert second is None
    # A different error_text gets through.
    third = svc.record_data_layer_error(
        "exec-sales",
        sql_file="queries/business/trend.sql",
        profile_name="analytics-prod",
        error_text="syntax error, unexpected DAY_",
    )
    assert third is not None


@pytest.mark.slow
def test_data_layer_probe_marks_health_degraded_when_errors_present(tmp_path, monkeypatch) -> None:
    """BUG-002 + diagnostics cross-cutting: app_run_healthcheck reports degraded when data_layer
    errors exist."""
    from dash_server.diagnostics.service import DiagnosticsService
    from dash_server.runtime.service import AppRuntimeService

    svc = DiagnosticsService(str(tmp_path / "diagnostics"))
    svc.record_data_layer_error(
        "exec-sales",
        sql_file="queries/business/trend.sql",
        profile_name="analytics-prod",
        error_text="object REVENUEZ not found",
    )

    # We only need the data_layer probe helper, not the full runtime; instantiate via __new__.
    runtime = AppRuntimeService.__new__(AppRuntimeService)
    runtime.diagnostics_service = svc
    probe = runtime._data_layer_probe("exec-sales", revision_number=2)
    assert probe["status"] == "failed"
    assert probe["details"]["sql_file"] == "queries/business/trend.sql"
    assert "REVENUEZ" in (probe["details"].get("latest_error") or "")


@pytest.mark.slow
def test_sql_placeholders_help_resource_is_reachable(app, client) -> None:
    payload = _resource_json(client, "dash://exasol/help/sql-placeholders", request_id=910)
    assert payload["resource"] == "dash://exasol/help/sql-placeholders"
    syntaxes = {p["syntax"] for p in payload["placeholders"]}
    assert "{name!d}" in syntaxes
    assert "{name!s}" in syntaxes or "{name} or {name!s}" in syntaxes
    assert any("Feature not supported: host parameter" in rule for rule in payload["rules"])
    assert any("queries/sql_smoke.json" in rule for rule in payload["rules"])

    # PS27 round-2 synthesis recommendation R1: warn against hardcoding an "all known
    # values" fallback list for an empty-selection IN (...) filter, since nothing in
    # app_validate/app_build/app_run_healthcheck checks whether it still matches live
    # data - confirmed to silently undercount a real dashboard's KPI by 32%.
    assert any("SELECT DISTINCT" in rule and "stale" in rule for rule in payload["rules"])
    assert any("SELECT DISTINCT" in mistake for mistake in payload["common_mistakes"])

    # R2: nothing in the pipeline checks a window function's semantic plausibility, so
    # the guide should at least prompt an author to spot-check one manually.
    assert any("PARTITION BY" in rule for rule in payload["rules"])


def test_ps27_r1_app_authoring_guide_documents_the_hardcoded_fallback_list_pitfall(app, client) -> None:
    guide = _resource_json(client, "dash://meta/app-authoring-guide", request_id=911)
    entry = next(
        (item for item in guide["common_failures"] if "empty-selection" in item.get("cause", "")),
        None,
    )
    assert entry is not None, "app-authoring-guide must document the hardcoded-fallback-list pitfall"
    assert "SELECT DISTINCT" in entry["fix"]


def _create_sql_smoke_profile(client, *, request_id: int) -> None:
    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "exasol_profile_create_local",
            "arguments": {
                "name": "analytics-prod",
                "backend": "onprem",
                "credential_mode": "password",
                "dsn": "demodb.exasol.com:8563",
                "user": "sys",
                "secret_value": "super-secret",
            },
        },
        request_id=request_id,
    )
    assert response.status_code == 200


def _create_parameterized_sql_app(client, *, name: str, include_smoke_params: bool, request_id: int) -> None:
    manifest = {
        "name": name,
        "title": name.replace("-", " ").title(),
        "route": f"/apps/{name}",
        "template": "exasol-analytics",
        "data_sources": {
            "primary": {
                "kind": "exasol",
                "profile": "analytics-prod",
                "auth_mode": "local_direct",
            }
        },
    }
    files = [
        {"path": "dash-app.json", "content": json.dumps(manifest) + "\n"},
        {
            "path": "app.py",
            "content": (
                "from dash import Dash, html\n\n"
                "def create_dash_app(server, url_base_pathname, metadata):\n"
                "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
                "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                "    app.layout = html.Div('parameterized sql smoke')\n"
                "    return app\n"
            ),
        },
        {
            "path": "queries/agent_latency.sql",
            "content": (
                "SELECT AGENT_ID, MISSING_LATENCY_MS\n"
                "FROM AGENT_EVENTS\n"
                "WHERE AGENT_ID = {agent_id!s}\n"
            ),
        },
        {"path": "requirements.txt", "content": "dash>=4.0,<5.0\npyexasol>=2.2.2,<3.0\n"},
    ]
    if include_smoke_params:
        files.append(
            {
                "path": "queries/sql_smoke.json",
                "content": json.dumps({"queries/agent_latency.sql": {"agent_id": "agent-001"}}) + "\n",
            }
        )
    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": name,
                "title": manifest["title"],
                "template": "exasol-analytics",
                "start_immediately": False,
                "files": files,
            },
        },
        request_id=request_id,
    )
    assert response.status_code == 200
    # PS26-BUG-005: this fixture's `queries/agent_latency.sql` always references the
    # nonexistent MISSING_LATENCY_MS column, so revision 1's own preflight can now
    # legitimately fail at creation time too (previously only a later app_build/
    # app_deploy_draft call ever ran sql_smoke against it, since creation skipped it
    # entirely). Both current call sites intentionally create a broken revision 1 to
    # then test what a *later* `app_build`/`app_deploy_draft` call reveals about
    # revision 2, so they don't assert anything about revision 1's own isError here.


def _sql_smoke_probe(preflight: dict[str, Any]) -> dict[str, Any]:
    return next(probe for probe in preflight["probes"] if probe.get("name") == "sql_smoke")


@pytest.mark.slow
def test_parameterized_sql_without_smoke_params_blocks_live_deploy(app, client) -> None:
    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=930)
    _create_parameterized_sql_app(
        client,
        name="param-sql-needs-smoke",
        include_smoke_params=False,
        request_id=931,
    )

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "param-sql-needs-smoke"}},
        request_id=932,
    )
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is True
    preflight = deploy_result["structuredContent"]["build"]["preflight"]
    probe = _sql_smoke_probe(preflight)
    assert probe["status"] == "failed"
    assert probe["details"]["first_failed_file"] == "queries/agent_latency.sql"
    assert "Missing values for: agent_id" in probe["details"]["latest_error"]
    assert "queries/sql_smoke.json" in probe["details"]["latest_error"]
    assert not any(connection.executions for connection in fake_module.connections)


@pytest.mark.slow
def test_parameterized_sql_smoke_params_exercise_query_and_catch_bad_column(app, client) -> None:
    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=940)
    _create_parameterized_sql_app(
        client,
        name="param-sql-bad-column",
        include_smoke_params=True,
        request_id=941,
    )

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "param-sql-bad-column"}},
        request_id=942,
    )
    deploy_result = deploy_response.get_json()["result"]
    assert deploy_result["isError"] is True
    preflight = deploy_result["structuredContent"]["build"]["preflight"]
    probe = _sql_smoke_probe(preflight)
    assert probe["status"] == "failed"
    assert probe["details"]["first_failed_file"] == "queries/agent_latency.sql"
    assert "MISSING_LATENCY_MS" in probe["details"]["latest_error"]
    executions = [execution for connection in fake_module.connections for execution in connection.executions]
    assert executions
    assert executions[0][1] == {"agent_id": "agent-001"}


def _sql_smoke_app_files(*, name: str, bad_column: bool) -> list[dict[str, Any]]:
    manifest = {
        "name": name,
        "title": name.replace("-", " ").title(),
        "route": f"/apps/{name}",
        "template": "exasol-analytics",
        "data_sources": {
            "primary": {"kind": "exasol", "profile": "analytics-prod", "auth_mode": "local_direct"}
        },
    }
    column = "MISSING_LATENCY_MS" if bad_column else "LATENCY_MS"
    return [
        {"path": "dash-app.json", "content": json.dumps(manifest) + "\n"},
        {
            "path": "app.py",
            "content": (
                "from dash import Dash, html\n\n"
                "def create_dash_app(server, url_base_pathname, metadata):\n"
                "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
                "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                "    app.layout = html.Div('sql smoke revision history')\n"
                "    return app\n"
            ),
        },
        {"path": "queries/agent_latency.sql", "content": f"SELECT AGENT_ID, {column}\nFROM AGENT_EVENTS\n"},
        {"path": "requirements.txt", "content": "dash>=4.0,<5.0\npyexasol>=2.2.2,<3.0\n"},
    ]


@pytest.mark.slow
def test_ps26_bug011_last_known_good_revision_skips_a_broken_rollback_target(app, client) -> None:
    """PS26-BUG-011 regression: `last_known_good_revision` used to just mirror
    `get_rollback_revision`, which is "whatever was live before the current revision" -
    not necessarily one that ever passed a health check.

    Builds a 4-revision history entirely through explicit `app_build` calls (never
    relying on `app_create_from_files`'s own un-preflighted first revision, so this
    stays valid regardless of PS26-BUG-005's status): r1 good, r2 bad-and-promoted,
    r3 good-and-promoted-over-r2 (so the rollback pointer now points at bad r2), r4
    bad-and-only-in-preview. The diagnostics field must recommend r3 (current, live,
    good) - not the broken rollback target r2, and not the broken preview head r4.
    """

    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=960)

    def _put_and_build(*, bad: bool, request_id: int) -> Any:
        _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_put_files",
                "arguments": {
                    "name": "last-known-good",
                    "files": [
                        f
                        for f in _sql_smoke_app_files(name="last-known-good", bad_column=bad)
                        if f["path"] != "dash-app.json"
                    ],
                },
            },
            request_id=request_id,
        )
        return _call_mcp(
            client, "tools/call", {"name": "app_build", "arguments": {"name": "last-known-good"}}, request_id=request_id + 1
        )

    # r1: good SQL, created via app_create_from_files (start_immediately=False, so
    # nothing is live/published yet) and promoted live.
    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "last-known-good",
                "title": "Last Known Good",
                "template": "exasol-analytics",
                "start_immediately": False,
                "files": _sql_smoke_app_files(name="last-known-good", bad_column=False),
            },
        },
        request_id=961,
    )
    assert create_response.get_json()["result"]["isError"] is False
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "last-known-good", "revision_number": 1}},
        request_id=962,
    )

    # r2: bad SQL, explicit app_build (so the preflight failure is properly recorded),
    # promoted live anyway (no promote health gate blocks this by default).
    build_2 = _put_and_build(bad=True, request_id=963)
    assert build_2.get_json()["result"]["isError"] is True
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "last-known-good", "revision_number": 2}},
        request_id=965,
    )

    # r3: good SQL, promoted over r2 - this makes bad r2 the rollback target.
    build_3 = _put_and_build(bad=False, request_id=966)
    assert build_3.get_json()["result"]["isError"] is False
    promote_3 = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "last-known-good", "revision_number": 3}},
        request_id=968,
    )
    assert promote_3.get_json()["result"]["structuredContent"]["app"]["rollback_revision_number"] == 2

    # r4: bad SQL again, built and left in preview only (never promoted).
    build_4 = _put_and_build(bad=True, request_id=969)
    assert build_4.get_json()["result"]["isError"] is True
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "last-known-good", "revision_number": 4}},
        request_id=971,
    )

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "last-known-good"}},
        request_id=972,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["last_known_good_revision"]["revision_number"] == 3


@pytest.mark.slow
def test_ps26_bug005_app_create_from_files_runs_preflight_on_the_first_revision(app, client) -> None:
    """PS26-BUG-005 regression: `app_create_from_files` (and every other tool that
    creates an app's first revision) used to skip the same `sql_smoke` preflight probe
    that `app_build` runs on every later revision - a bad query went straight to
    `published: true, status: "running"` with no error, only surfacing via a
    separately-called `app_run_healthcheck`. The very first `app_create_from_files`
    call for a bad query must now itself report the failure.
    """

    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=990)

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "preflight-on-create",
                "title": "Preflight On Create",
                "template": "exasol-analytics",
                "files": _sql_smoke_app_files(name="preflight-on-create", bad_column=True),
            },
        },
        request_id=991,
    )
    result = create_response.get_json()["result"]
    assert result["isError"] is True
    structured = result["structuredContent"]
    assert structured["error"]["category"] == "artifact_preflight_failed"
    assert structured["error"]["details"]["revision_number"] == 1
    preflight = structured["preflight"]
    assert preflight["status"] == "failed"
    probe = _sql_smoke_probe(preflight)
    assert probe["status"] == "failed"
    assert "MISSING_LATENCY_MS" in probe["details"]["latest_error"]

    # The app still exists (creation isn't blocked outright, matching app_build's
    # "revision exists, but preflight failed" contract) - a healthcheck independently
    # confirms the same failure, so the two surfaces agree.
    health = _call_mcp(
        client,
        "tools/call",
        {"name": "app_run_healthcheck", "arguments": {"name": "preflight-on-create"}},
        request_id=992,
    ).get_json()["result"]["structuredContent"]
    assert health["health"]["status"] == "degraded"


@pytest.mark.slow
def test_ps26_bug005_app_create_from_files_with_good_sql_still_succeeds_cleanly(app, client) -> None:
    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=993)

    create_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "preflight-on-create-good",
                "title": "Preflight On Create Good",
                "template": "exasol-analytics",
                "files": _sql_smoke_app_files(name="preflight-on-create-good", bad_column=False),
            },
        },
        request_id=994,
    )
    result = create_response.get_json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["preflight"]["status"] == "passed"


@pytest.mark.slow
def test_ps26_bug006_promote_revision_require_healthy_blocks_a_known_bad_revision(app, client) -> None:
    """PS26-BUG-006 regression: `app_promote_revision` had no health gate at all - it
    would promote a revision whose own preview healthcheck reported `degraded` with no
    warning. `require_healthy=true` must refuse that; the default (`require_healthy`
    omitted, i.e. false) must keep promoting unconditionally, matching the documented
    "opt-in, not a behavior change" fix.
    """

    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=980)

    # r1 (created via app_create_from_files) is intentionally left good/unused here -
    # PS26-BUG-005 (app_create_from_files' first revision skips sql_smoke) means a bad
    # r1 wouldn't have a recorded failure to gate on. Use an explicit app_build (r2) for
    # the bad revision instead, so this test is valid regardless of BUG-005's status.
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "gated-promote",
                "title": "Gated Promote",
                "template": "exasol-analytics",
                "start_immediately": False,
                "files": _sql_smoke_app_files(name="gated-promote", bad_column=False),
            },
        },
        request_id=981,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "gated-promote",
                "files": [
                    f
                    for f in _sql_smoke_app_files(name="gated-promote", bad_column=True)
                    if f["path"] != "dash-app.json"
                ],
            },
        },
        request_id=982,
    )
    build_response = _call_mcp(
        client, "tools/call", {"name": "app_build", "arguments": {"name": "gated-promote"}}, request_id=983
    )
    assert build_response.get_json()["result"]["isError"] is True  # preflight fails; revision 2 still exists

    blocked = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_promote_revision",
            "arguments": {"name": "gated-promote", "revision_number": 2, "require_healthy": True},
        },
        request_id=984,
    )
    blocked_result = blocked.get_json()["result"]
    assert blocked_result["isError"] is True
    assert blocked_result["structuredContent"]["error"]["category"] == "deployment_healthcheck_failed"

    # Confirm it genuinely didn't promote: current revision is still r1.
    status = _call_mcp(
        client, "tools/call", {"name": "app_get_status", "arguments": {"name": "gated-promote"}}, request_id=985
    ).get_json()["result"]["structuredContent"]
    assert status["current_revision"]["revision_number"] == 1

    # Default behavior (require_healthy omitted) is unchanged: promotes anyway.
    allowed = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "gated-promote", "revision_number": 2}},
        request_id=986,
    )
    assert allowed.get_json()["result"]["isError"] is False


@pytest.mark.slow
def test_exasol_helper_auto_seeded_for_exasol_analytics_template(app, client) -> None:
    """BUG-004 regression: app_create_from_files with template=exasol-analytics auto-injects dash_server_exasol.py.

    PS26-BUG-005 note: this app has no `queries/*.sql` files, so `sql_smoke` is
    `not_applicable` regardless of the bound profile - but since PS26-BUG-005's fix
    makes `app_create_from_files` preflight the first revision too, the profile
    referenced in `dash-app.json` must now actually resolve (previously a placeholder
    name like "doesnt-matter" worked because nothing validated it at creation time).
    """
    fake_module = _SqlSmokeFakePyExasolModule()
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = lambda: fake_module
    _create_sql_smoke_profile(client, request_id=919)

    response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create_from_files",
            "arguments": {
                "name": "auto-helper-test",
                "title": "Auto Helper Test",
                "template": "exasol-analytics",
                "files": [
                    {
                        "path": "app.py",
                        "content": (
                            "from dash import Dash, html\n\n"
                            "def create_dash_app(server, url_base_pathname, metadata):\n"
                            "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
                            "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
                            "    app.layout = html.Div(metadata.get('title', 'auto-helper'))\n"
                            "    return app\n"
                        ),
                    },
                    {
                        "path": "dash-app.json",
                        "content": (
                            '{"name": "auto-helper-test", "title": "Auto Helper Test", '
                            '"route": "/apps/auto-helper-test", "template": "exasol-analytics", '
                            '"data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod", "auth_mode": "local_direct"}}}'
                        ),
                    },
                    {"path": "requirements.txt", "content": "dash>=4.0,<5.0\n"},
                ],
            },
        },
        request_id=920,
    )
    payload = response.get_json()["result"]
    assert payload.get("isError") is False, payload
    structured = payload["structuredContent"]
    files_in_draft = structured["draft"]["files"]
    assert "dash_server_exasol.py" in files_in_draft, (
        "BUG-004: dash_server_exasol.py should be auto-seeded for template=exasol-analytics"
    )
    notes = structured.get("notes", [])
    assert any("dash_server_exasol.py" in n for n in notes), notes
