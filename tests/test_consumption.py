from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import re
import sqlite3
import sys
from threading import Event
import time
from types import SimpleNamespace
from typing import Any

import pytest

from dash_server.auth import AuthContext, Principal
from dash_server.app_factory import create_app
from dash_server.consumption import consumption_contract_hash, normalize_consumption_contract
from dash_server.consumption.execution import DatasetStream, ExasolDatasetExecutor
from dash_server.exceptions import DashServerError


_APP_PY = """from dash import Dash, html


def create_dash_app(server, url_base_pathname, metadata):
    prefix = url_base_pathname.rstrip(\"/\") + \"/\"
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix=\"/\",
        requests_pathname_prefix=prefix,
        title=metadata.get(\"title\", \"Consumption Test\"),
    )
    app.layout = html.Div([html.H1(metadata.get(\"title\", \"Consumption Test\"))])
    return app
"""


def _call_tool(
    client,
    name: str,
    arguments: dict[str, Any],
    request_id: int = 1,
    *,
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _read_resource(client, uri: str, request_id: int = 2) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    return json.loads(response.get_json()["result"]["contents"][0]["text"])


def _consumption_contract(*, source_path: str = "queries/export.sql") -> dict[str, Any]:
    return {
        "outputs": [
            {
                "id": "monthly-close-detail",
                "title": "Monthly close detail",
                "description": "Posting detail for one accounting period.",
                "kind": "dataset",
                "source": {
                    "type": "exasol_sql",
                    "data_source": "primary",
                    "path": source_path,
                },
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "pattern": "^[0-9]{4}-[0-9]{2}$",
                        }
                    },
                    "required": ["period"],
                    "additionalProperties": False,
                },
                "formats": ["csv", "xlsx"],
                "classification": "confidential",
                "limits": {"max_rows": 5000, "max_bytes": 1000000},
                "allow_subscriptions": True,
                "allow_alerts": False,
            }
        ]
    }


def _create_output_app(client, *, name: str = "finance-outputs", include_sql: bool = True):
    files = [{"path": "app.py", "content": _APP_PY}]
    if include_sql:
        files.append(
            {
                "path": "queries/export.sql",
                "content": "SELECT {period!s} AS PERIOD FROM DUAL\n",
            }
        )
    response = _call_tool(
        client,
        "app_create_from_files",
        {
            "name": name,
            "title": "Finance Outputs",
            "data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}},
            "consumption": _consumption_contract(),
            "files": files,
        },
    )
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["isError"] is False
    return result["structuredContent"]


class _FakeDatasetExecutor:
    def __init__(self, rows: list[list[Any]] | None = None) -> None:
        self.rows = rows or [["2026-07", "=SUM(A1:A2)"], ["2026-07", "safe"]]
        self.preflight_calls = 0

    def preflight(self, revision, output) -> None:
        self.preflight_calls += 1

    def stream(self, revision, output, parameters, *, cancelled) -> DatasetStream:
        return DatasetStream(columns=["PERIOD", "VALUE"], batches=iter([[*self.rows]]))


class _BlockingDatasetExecutor(_FakeDatasetExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def stream(self, revision, output, parameters, *, cancelled) -> DatasetStream:
        def batches():
            self.started.set()
            self.release.wait(timeout=5)
            yield self.rows

        return DatasetStream(columns=["PERIOD", "VALUE"], batches=batches())


def _enable_phase1(app, executor=None):
    service = app.extensions["consumption_service"]
    service.policy = replace(service.policy, exports_enabled=True)
    service.policy_version = service.policy.version
    service.executor = executor or _FakeDatasetExecutor()
    return service


def _wait_for_job(client, job_id: str, expected: set[str] | None = None):
    expected = expected or {"succeeded", "failed", "cancelled"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = _call_tool(client, "export_get", {"job_id": job_id})
        payload = response.get_json()["result"]["structuredContent"]
        if payload["job"]["status"] in expected:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"Export {job_id} did not reach {expected}")


def test_phase0_output_discovery_matches_mcp_resource_and_ui(app, client):
    _create_output_app(client)

    tool_response = _call_tool(
        client,
        "app_outputs_list",
        {"name": "finance-outputs"},
        request_id=3,
    )
    tool_payload = tool_response.get_json()["result"]["structuredContent"]
    resource_payload = _read_resource(
        client,
        "dash://apps/finance-outputs/outputs",
        request_id=4,
    )
    ui_response = client.get("/manage/apps/finance-outputs/consumption")
    catalog_response = client.get("/")
    hosted_layout = client.get("/apps/finance-outputs/_dash-layout")

    tool_domain_payload = {key: value for key, value in tool_payload.items() if key != "guidance"}
    assert tool_domain_payload == resource_payload
    assert tool_payload["output_count"] == 1
    assert tool_payload["outputs"][0]["id"] == "monthly-close-detail"
    assert tool_payload["outputs"][0]["policy"] == {
        "enabled": True,
        "effective_formats": ["csv", "xlsx"],
        "blocked_formats": [],
        "effective_limits": {"max_rows": 5000, "max_bytes": 1000000},
        "format_availability": {
            "csv": {"executable": False, "reason": "exports_disabled"},
            "xlsx": {"executable": False, "reason": "exports_disabled"},
        },
        "phase": "discovery_only",
        "executable": False,
        "reason": "no_executable_format",
    }
    revision = app.extensions["registry"].get_current_revision("finance-outputs")
    assert revision is not None
    declared_consumption = revision.manifest["consumption"]
    assert declared_consumption["outputs"][0]["id"] == "monthly-close-detail"
    assert "policy" not in declared_consumption["outputs"][0]
    assert consumption_contract_hash(declared_consumption) == tool_payload["contract_hash"]
    assert revision.manifest["consumption_contract_hash"] == tool_payload["contract_hash"]
    assert ui_response.status_code == 200
    assert b"Monthly close detail" in ui_response.data
    assert b"Output discovery is available now" in ui_response.data
    assert b"Outputs (1)" in catalog_response.data
    assert b"/manage/apps/finance-outputs/consumption" in catalog_response.data
    assert hosted_layout.status_code == 200
    hosted_layout_text = json.dumps(hosted_layout.get_json())
    assert "/manage/apps/finance-outputs/consumption" in hosted_layout_text
    assert "__dash-server-exports-link" in hosted_layout_text


def test_output_get_and_execution_context_are_revision_and_principal_bound(app, client):
    _create_output_app(client)
    response = _call_tool(
        client,
        "app_output_get",
        {"name": "finance-outputs", "output_id": "monthly-close-detail"},
    )
    payload = response.get_json()["result"]["structuredContent"]
    context = app.extensions["consumption_service"].execution_context(
        "finance-outputs",
        "monthly-close-detail",
        AuthContext.for_mode("local", auth_enabled=False),
    )

    assert payload["output"]["id"] == "monthly-close-detail"
    assert context.app_name == "finance-outputs"
    assert context.revision_number == payload["revision"]["revision_number"]
    assert context.output_contract_hash == payload["contract_hash"]
    assert context.principal_id == "local-admin"
    assert context.policy_version.startswith("consumption-v1:")


def test_workspace_validation_fails_when_declared_output_source_is_missing(client):
    response = _call_tool(
        client,
        "app_create_from_files",
        {
            "name": "missing-output-source",
            "title": "Missing Output Source",
            "data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}},
            "consumption": _consumption_contract(),
            "files": [{"path": "app.py", "content": _APP_PY}],
        },
    )
    result = response.get_json()["result"]
    validation = result["structuredContent"]["error"]["details"]["validation"]

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "workspace_validation_error"
    assert validation["is_valid"] is False
    assert validation["consumption"]["status"] == "failed"
    assert validation["consumption"]["issues"][0]["path"] == "queries/export.sql"


def test_contract_rejects_unsafe_source_and_unknown_parameter_schema_features():
    data_sources = {"primary": {"kind": "exasol", "profile": "analytics-prod"}}
    with pytest.raises(DashServerError) as unsafe_path:
        normalize_consumption_contract(
            _consumption_contract(source_path="../secret.sql"),
            data_sources=data_sources,
        )
    assert unsafe_path.value.category == "consumption_contract_validation_error"
    assert unsafe_path.value.details["field"].endswith("source.path")

    contract = _consumption_contract()
    contract["outputs"][0]["parameters"]["properties"]["period"]["oneOf"] = []
    with pytest.raises(DashServerError) as unknown_schema:
        normalize_consumption_contract(contract, data_sources=data_sources)
    assert unknown_schema.value.category == "consumption_contract_validation_error"
    assert "Unknown field" in unknown_schema.value.summary


def test_output_discovery_requires_app_export_authorization(app, client):
    _create_output_app(client)
    service = app.extensions["consumption_service"]
    registry = app.extensions["registry"]
    principal = Principal.authenticated_user(
        issuer="test",
        subject="viewer-1",
        email="viewer@example.test",
        roles=(),
        email_verified=True,
    )
    hosted_context = AuthContext(
        mode="hosted",
        auth_enabled=True,
        provider="trusted_proxy",
        principal=principal,
    )

    with pytest.raises(DashServerError) as denied:
        service.list_outputs("finance-outputs", hosted_context)
    assert denied.value.category == "consumption_authorization_denied"

    registry.grant_app_access(
        "finance-outputs",
        principal_type="user",
        principal_id=principal.principal_id,
        role="viewer",
        scope="all",
        created_by_principal_id="test-owner",
    )
    allowed = service.list_outputs("finance-outputs", hosted_context)
    assert allowed["authorization"]["allowed"] is True
    assert allowed["authorization"]["effective_role"] == "viewer"


def test_consumption_registry_tables_and_phase1_columns_are_initialized(app):
    db_path = app.config["REGISTRY_DB_PATH"]
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(consumption_jobs)").fetchall()}

    assert {
        "consumption_jobs",
        "consumption_artifacts",
        "consumption_subscriptions",
        "consumption_alerts",
        "consumption_delivery_attempts",
        "consumption_audit_events",
    } <= tables
    assert {
        "context_json",
        "output_json",
        "parameters_redacted_json",
        "effective_limits_json",
        "cancel_requested_at",
        "expires_at",
    } <= job_columns


def test_hosted_viewer_has_same_authorized_output_catalog_through_mcp_and_ui(tmp_path: Path):
    hosted_app = create_app(
        {
            "TESTING": True,
            "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
            "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
            "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
            "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state"),
            "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
            "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets"),
            "AUTO_INSTALL_DEPENDENCIES": False,
            "PYTHON_EXECUTABLE": sys.executable,
            "DASH_SERVER_MODE": "hosted",
            "SECRET_KEY": "test-secret-key",
            "SESSION_COOKIE_SECURE": True,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "DASH_SERVER_PUBLIC_BASE_URL": "https://dash.example.test",
            "DASH_SERVER_AUTH_PROVIDER": "trusted_proxy",
            "DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED": True,
            "DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS": ("127.0.0.1/32",),
            "DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS": ("trusted_proxy:admin-1",),
            "DASH_SERVER_ALLOW_UNSAFE_INPROCESS": True,
        }
    )
    hosted_client = hosted_app.test_client()
    admin_headers = {
        "X-Forwarded-User": "admin-1",
        "X-Forwarded-Email": "admin@example.test",
    }
    viewer_headers = {
        "X-Forwarded-User": "viewer-1",
        "X-Forwarded-Email": "viewer@example.test",
    }
    stranger_headers = {
        "X-Forwarded-User": "stranger-1",
        "X-Forwarded-Email": "stranger@example.test",
    }
    files = [
        {"path": "app.py", "content": _APP_PY},
        {"path": "queries/export.sql", "content": "SELECT {period!s} AS PERIOD FROM DUAL\n"},
    ]
    created = _call_tool(
        hosted_client,
        "app_create_from_files",
        {
            "name": "hosted-outputs",
            "data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}},
            "consumption": _consumption_contract(),
            "files": files,
        },
        headers=admin_headers,
    )
    assert created.get_json()["result"]["isError"] is False
    hosted_app.extensions["registry"].grant_app_access(
        "hosted-outputs",
        principal_type="user",
        principal_id="trusted_proxy:viewer-1",
        role="viewer",
        scope="all",
        created_by_principal_id="trusted_proxy:admin-1",
    )

    mcp_response = _call_tool(
        hosted_client,
        "app_outputs_list",
        {"name": "hosted-outputs"},
        headers=viewer_headers,
    )
    ui_response = hosted_client.get(
        "/manage/apps/hosted-outputs/consumption",
        headers=viewer_headers,
    )
    denied_mcp = _call_tool(
        hosted_client,
        "app_outputs_list",
        {"name": "hosted-outputs"},
        headers=stranger_headers,
    )
    denied_ui = hosted_client.get(
        "/manage/apps/hosted-outputs/consumption",
        headers=stranger_headers,
    )

    assert mcp_response.status_code == 200
    assert mcp_response.get_json()["result"]["structuredContent"]["output_count"] == 1
    assert ui_response.status_code == 200
    assert b"Monthly close detail" in ui_response.data
    assert denied_mcp.status_code == 403
    assert denied_mcp.get_json()["error"]["data"]["category"] == "mcp_authorization_denied"
    assert denied_ui.status_code == 403
    assert denied_ui.get_json()["error"]["data"]["category"] == "consumption_authorization_denied"

    _enable_phase1(hosted_app)
    export_created = _call_tool(
        hosted_client,
        "app_export_create",
        {
            "name": "hosted-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
        },
        headers=viewer_headers,
    ).get_json()["result"]["structuredContent"]
    export_job_id = export_created["job"]["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        export_status = _call_tool(
            hosted_client,
            "export_get",
            {"job_id": export_job_id},
            headers=viewer_headers,
        ).get_json()["result"]["structuredContent"]
        if export_status["job"]["status"] == "succeeded":
            break
        time.sleep(0.02)
    assert export_status["job"]["status"] == "succeeded"
    download_link = _call_tool(
        hosted_client,
        "export_download_link_create",
        {"job_id": export_job_id},
        headers=viewer_headers,
    ).get_json()["result"]["structuredContent"]["download_url"]
    hosted_app.extensions["registry"].revoke_app_access(
        "hosted-outputs",
        principal_type="user",
        principal_id="trusted_proxy:viewer-1",
    )
    revoked_download = hosted_client.get(download_link, headers=viewer_headers)
    assert revoked_download.status_code == 403
    assert revoked_download.get_json()["error"]["data"]["category"] == "consumption_authorization_denied"


def test_phase1_mcp_csv_export_is_pinned_encrypted_and_downloadable(app, client):
    _create_output_app(client)
    executor = _FakeDatasetExecutor()
    service = _enable_phase1(app, executor)

    created = _call_tool(
        client,
        "app_export_create",
        {
            "name": "finance-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
            "idempotency_key": "mcp-export-1",
        },
    ).get_json()["result"]["structuredContent"]
    job_id = created["job"]["id"]
    completed = _wait_for_job(client, job_id)
    resource_payload = _read_resource(client, f"dash://exports/{job_id}")

    assert completed["job"]["status"] == "succeeded"
    assert completed["job"]["revision_number"] == 1
    assert completed["job"]["output_contract_hash"]
    assert completed["job"]["policy_version"] == service.policy_version
    assert "parameters" not in completed["job"]
    assert completed["artifact"]["row_count"] == 2
    assert resource_payload == {
        key: value for key, value in completed.items() if key != "guidance"
    }
    assert executor.preflight_calls == 1

    with sqlite3.connect(app.config["REGISTRY_DB_PATH"]) as connection:
        encoded = connection.execute("SELECT parameters_json FROM consumption_jobs WHERE id = ?", (job_id,)).fetchone()[
            0
        ]
    assert "2026-07" not in encoded
    assert service.parameter_codec.decode(encoded) == {"period": "2026-07"}

    link = _call_tool(client, "export_download_link_create", {"job_id": job_id}).get_json()["result"][
        "structuredContent"
    ]
    downloaded = client.get(link["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["Cache-Control"] == "private, no-store"
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    assert downloaded.data.startswith(b"PERIOD,VALUE\n")
    assert b"'=SUM(A1:A2)" in downloaded.data

    artifact_path = service.artifact_store.resolve(completed["artifact"]["storage_key"])
    with sqlite3.connect(app.config["REGISTRY_DB_PATH"]) as connection:
        connection.execute(
            "UPDATE consumption_artifacts SET expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", job_id),
        )
        connection.commit()
    assert service.cleanup_expired_artifacts() == 1
    assert artifact_path.exists() is False
    expired = _call_tool(client, "export_get", {"job_id": job_id})
    assert expired.get_json()["result"]["structuredContent"]["job"]["status"] == "expired"
    with sqlite3.connect(app.config["REGISTRY_DB_PATH"]) as connection:
        audit_events = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM consumption_audit_events WHERE job_id = ?", (job_id,)
            ).fetchall()
        }
    assert {
        "export.created",
        "export.succeeded",
        "artifact.downloaded",
        "artifact.expired",
    } <= audit_events


def test_phase1_idempotency_and_parameter_validation(app, client):
    _create_output_app(client)
    _enable_phase1(app)
    request = {
        "name": "finance-outputs",
        "output_id": "monthly-close-detail",
        "format": "csv",
        "parameters": {"period": "2026-07"},
        "idempotency_key": "same-request",
    }
    first = _call_tool(client, "app_export_create", request).get_json()["result"]
    second = _call_tool(client, "app_export_create", request).get_json()["result"]
    assert first["structuredContent"]["job"]["id"] == second["structuredContent"]["job"]["id"]

    conflict = _call_tool(
        client,
        "app_export_create",
        {**request, "parameters": {"period": "2026-08"}},
    ).get_json()["result"]
    assert conflict["isError"] is True
    assert conflict["structuredContent"]["error"]["category"] == "consumption_idempotency_conflict"

    invalid = _call_tool(
        client,
        "app_export_create",
        {**request, "idempotency_key": "invalid-params", "parameters": {"period": "July"}},
    ).get_json()["result"]
    assert invalid["isError"] is True
    assert invalid["structuredContent"]["error"]["category"] == "consumption_parameter_validation_error"


def test_phase1_ui_uses_csrf_and_same_job_service(app, client):
    _create_output_app(client)
    _enable_phase1(app)
    page = client.get("/manage/apps/finance-outputs/consumption")
    assert page.status_code == 200
    csrf_match = re.search(rb'name="_csrf" value="([^"]+)"', page.data)
    idempotency_match = re.search(rb'name="idempotency_key" value="([^"]+)"', page.data)
    assert csrf_match and idempotency_match

    denied = client.post(
        "/manage/apps/finance-outputs/exports",
        data={
            "output_id": "monthly-close-detail",
            "format": "csv",
            "param__period": "2026-07",
            "type__period": "string",
            "idempotency_key": "ui-denied",
        },
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"]["data"]["category"] == "consumption_token_invalid"

    created = client.post(
        "/manage/apps/finance-outputs/exports",
        data={
            "_csrf": csrf_match.group(1).decode(),
            "output_id": "monthly-close-detail",
            "format": "csv",
            "param__period": "2026-07",
            "type__period": "string",
            "idempotency_key": idempotency_match.group(1).decode(),
        },
    )
    assert created.status_code == 303
    job_id = created.headers["Location"].rsplit("/", 1)[-1]
    completed = _wait_for_job(client, job_id)
    assert completed["job"]["status"] == "succeeded"
    mcp_same = _call_tool(
        client,
        "app_export_create",
        {
            "name": "finance-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
            "idempotency_key": idempotency_match.group(1).decode(),
        },
    ).get_json()["result"]["structuredContent"]
    assert mcp_same["job"]["id"] == job_id
    detail = client.get(created.headers["Location"])
    assert detail.status_code == 200
    assert b"Download CSV" in detail.data


def test_phase1_cancellation_publishes_no_partial_artifact(app, client):
    _create_output_app(client)
    executor = _BlockingDatasetExecutor()
    service = _enable_phase1(app, executor)
    created = _call_tool(
        client,
        "app_export_create",
        {
            "name": "finance-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
        },
    ).get_json()["result"]["structuredContent"]
    job_id = created["job"]["id"]
    assert executor.started.wait(timeout=2)
    cancelled = _call_tool(client, "export_cancel", {"job_id": job_id})
    assert cancelled.status_code == 200
    executor.release.set()
    terminal = _wait_for_job(client, job_id)
    assert terminal["job"]["status"] == "cancelled"
    assert terminal["artifact"] is None
    assert not list((service.artifact_store.root / job_id).glob("*.csv"))


def test_phase1_row_limit_fails_without_artifact(app, client):
    _create_output_app(client)
    service = _enable_phase1(app, _FakeDatasetExecutor())
    service.policy = replace(service.policy, max_rows=1)
    service.policy_version = service.policy.version
    created = _call_tool(
        client,
        "app_export_create",
        {
            "name": "finance-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
        },
    ).get_json()["result"]["structuredContent"]
    terminal = _wait_for_job(client, created["job"]["id"])
    assert terminal["job"]["status"] == "failed"
    assert terminal["job"]["error"]["category"] == "consumption_export_limit_exceeded"
    assert terminal["artifact"] is None


def test_exasol_export_executor_uses_bounded_fetch_and_closes_resources(tmp_path: Path):
    artifact_root = tmp_path / "artifact"
    (artifact_root / "queries").mkdir(parents=True)
    (artifact_root / "queries" / "export.sql").write_text("SELECT {period!s} AS PERIOD FROM DUAL", encoding="utf-8")

    class Statement:
        def __init__(self) -> None:
            self.batches = [[("2026-07",), ("2026-08",)], []]
            self.fetch_sizes: list[int] = []
            self.closed = False

        def column_names(self):
            return ["PERIOD"]

        def fetchmany(self, size):
            self.fetch_sizes.append(size)
            return self.batches.pop(0)

        def fetchall(self):
            raise AssertionError("export execution must not call fetchall")

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self, statement) -> None:
            self.statement = statement
            self.closed = False

        def execute(self, sql, parameters):
            assert parameters == {"period": "2026-07"}
            return self.statement

        def close(self):
            self.closed = True

    statement = Statement()
    connection = Connection(statement)
    profile = SimpleNamespace(name="analytics-prod")
    connection_options = {}

    def connect_uncached(_profile, **kwargs):
        connection_options.update(kwargs)
        return connection

    service = SimpleNamespace(
        profile_store=SimpleNamespace(get_profile=lambda _name: profile),
        connection_manager=SimpleNamespace(connect_uncached=connect_uncached),
    )
    executor = ExasolDatasetExecutor(service, batch_size=2, max_runtime_seconds=30)
    revision = SimpleNamespace(
        app_name="finance-outputs",
        revision_number=1,
        artifact_path=str(artifact_root),
        manifest={"data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}}},
    )
    output = {
        "id": "monthly-close-detail",
        "source": {
            "type": "exasol_sql",
            "data_source": "primary",
            "path": "queries/export.sql",
        },
    }

    stream = executor.stream(
        revision,
        output,
        {"period": "2026-07"},
        cancelled=lambda: False,
    )
    assert stream.columns == ["PERIOD"]
    assert list(stream.batches) == [[["2026-07"], ["2026-08"]]]
    assert statement.fetch_sizes == [2, 2]
    assert connection_options == {"query_timeout_seconds": 30}
    assert statement.closed is True
    assert connection.closed is True
