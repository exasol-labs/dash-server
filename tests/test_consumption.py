from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import pytest

from dash_server.auth import AuthContext, Principal
from dash_server.app_factory import create_app
from dash_server.consumption import consumption_contract_hash, normalize_consumption_contract
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
            "data_sources": {
                "primary": {"kind": "exasol", "profile": "analytics-prod"}
            },
            "consumption": _consumption_contract(),
            "files": files,
        },
    )
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["isError"] is False
    return result["structuredContent"]


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

    tool_domain_payload = {key: value for key, value in tool_payload.items() if key != "guidance"}
    assert tool_domain_payload == resource_payload
    assert tool_payload["output_count"] == 1
    assert tool_payload["outputs"][0]["id"] == "monthly-close-detail"
    assert tool_payload["outputs"][0]["policy"] == {
        "enabled": True,
        "effective_formats": ["csv", "xlsx"],
        "blocked_formats": [],
        "effective_limits": {"max_rows": 5000, "max_bytes": 1000000},
        "phase": "discovery_only",
        "executable": False,
        "reason": "phase_0_discovery_only",
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
            "data_sources": {
                "primary": {"kind": "exasol", "profile": "analytics-prod"}
            },
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


def test_phase0_registry_tables_are_initialized(app):
    db_path = app.config["REGISTRY_DB_PATH"]
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {
        "consumption_jobs",
        "consumption_artifacts",
        "consumption_subscriptions",
        "consumption_alerts",
        "consumption_delivery_attempts",
    } <= tables


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
            "data_sources": {
                "primary": {"kind": "exasol", "profile": "analytics-prod"}
            },
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
