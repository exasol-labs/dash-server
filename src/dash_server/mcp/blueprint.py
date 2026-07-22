"""Flask blueprint exposing the `/mcp` control-plane endpoint."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from dash_server.errors import JSONRPC_PARSE_ERROR, codes_for
from dash_server.auth import current_auth_context
from dash_server.auth.capabilities import (
    DASHBOARD_EXPORT,
    MCP_USE_CONTROL_PLANE,
    roles_with_capability,
)
from dash_server.mcp.resources import APP_OUTPUTS_RESOURCE_RE, EXPORT_RESOURCE_RE
from dash_server.mcp.tool_specs import APP_SCOPED_TOOL_CAPABILITIES, JOB_SCOPED_TOOLS


# Derived from the single ToolSpec table so the transport gate cannot disagree
# with the server's own handler dispatch. Names kept for the drift-guard test.
_APP_SCOPED_TOOL_CAPABILITIES = APP_SCOPED_TOOL_CAPABILITIES
_JOB_SCOPED_TOOLS = JOB_SCOPED_TOOLS


def create_mcp_blueprint() -> Blueprint:
    """Create the `/mcp` blueprint."""

    blueprint = Blueprint("mcp", __name__)

    @blueprint.get("/mcp")
    def mcp_stream() -> Response:
        denial = _mcp_authorization_denial()
        if denial is not None:
            return denial
        server = current_app.extensions["mcp_server"]

        def event_stream():
            yield server.sse_ready_event()

        return Response(event_stream(), mimetype="text/event-stream")

    @blueprint.post("/mcp")
    def mcp_rpc():
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": JSONRPC_PARSE_ERROR, "message": "Request body must be JSON."},
                }
            ), 400

        denial = _mcp_authorization_denial(payload)
        if denial is not None:
            return denial
        server = current_app.extensions["mcp_server"]
        response_body, status_code = server.handle_jsonrpc(payload)
        return jsonify(response_body), status_code

    return blueprint


def _mcp_authorization_denial(payload: dict[str, Any] | None = None):
    auth_context = current_auth_context()
    if auth_context.mode == "local":
        return None

    principal = auth_context.principal
    # Single source of truth: whoever the matrix grants control-plane access.
    allowed_roles = roles_with_capability(MCP_USE_CONTROL_PLANE)
    if (
        principal.is_authenticated
        and principal.principal_type == "user"
        and any(role in allowed_roles for role in principal.roles)
    ):
        return None
    if _app_scoped_mcp_call_allowed(payload):
        return None

    status_code = 401 if not principal.is_authenticated else 403
    request_id = payload.get("id") if isinstance(payload, dict) else None
    response_body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": codes_for("mcp_authorization_denied")[0],
            "message": "MCP control-plane access denied.",
            "data": {
                "category": "mcp_authorization_denied",
                "principal_id": principal.principal_id,
                "principal_type": principal.principal_type,
                "required_roles": sorted(allowed_roles),
            },
        },
    }
    return jsonify(response_body), status_code


def _app_scoped_mcp_call_allowed(payload: dict[str, Any] | None = None) -> bool:
    """Allow app-scoped MCP operations without granting global control-plane access."""

    auth_context = current_auth_context()
    principal = auth_context.principal
    if (
        not principal.is_authenticated
        or principal.principal_type != "user"
        or not isinstance(payload, dict)
    ):
        return False

    params = payload.get("params")
    if not isinstance(params, dict):
        return False
    method = payload.get("method")
    capability: str | None = None
    app_name: Any = None
    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            return False
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return False
        capability = _APP_SCOPED_TOOL_CAPABILITIES.get(tool_name)
        app_name = arguments.get("name")
        if tool_name in _JOB_SCOPED_TOOLS:
            job_id = arguments.get("job_id")
            if not isinstance(job_id, str):
                return False
            capability = "dashboard.export"
            app_name = current_app.extensions["consumption_service"].peek_job_app(job_id)
    elif method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str):
            return False
        match = APP_OUTPUTS_RESOURCE_RE.fullmatch(uri)
        if match is not None:
            capability = DASHBOARD_EXPORT
            app_name = match.group(1)
        else:
            export_match = EXPORT_RESOURCE_RE.fullmatch(uri)
            if export_match is None:
                return False
            capability = DASHBOARD_EXPORT
            app_name = current_app.extensions["consumption_service"].peek_job_app(
                export_match.group(1)
            )
    else:
        return False
    if capability is None:
        return False
    if not isinstance(app_name, str) or not app_name.strip():
        return False

    registry = current_app.extensions["registry"]
    app = registry.get_app(app_name.strip())
    if app is None:
        return False
    decision = current_app.extensions["authorization_service"].authorize_app(
        auth_context,
        app,
        capability,
    )
    return decision.allowed
