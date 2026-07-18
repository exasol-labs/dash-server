"""Flask blueprint exposing the `/mcp` control-plane endpoint."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from dash_server.auth import current_auth_context


_APP_SCOPED_TOOL_CAPABILITIES = {
    "app_share_get": "dashboard.manage_sharing",
    "app_share_grant": "dashboard.manage_sharing",
    "app_share_revoke": "dashboard.manage_sharing",
    "app_share_set_link_scope": "dashboard.manage_sharing",
    "app_share_explain_access": "dashboard.manage_sharing",
    "app_share_create_one_time_link": "dashboard.manage_sharing",
    "app_share_revoke_one_time_link": "dashboard.manage_sharing",
    "app_invite_external_user": "dashboard.manage_sharing",
    "app_revoke_external_invitation": "dashboard.manage_sharing",
    "app_list_files": "dashboard.edit_draft",
    "app_delete": "dashboard.delete",
}


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
                    "error": {"code": -32700, "message": "Request body must be JSON."},
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
    allowed_roles = {"admin", "owner", "editor"}
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
            "code": -32030,
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
    """Allow app owners to use sharing tools without global MCP access."""

    auth_context = current_auth_context()
    principal = auth_context.principal
    if (
        not principal.is_authenticated
        or principal.principal_type != "user"
        or not isinstance(payload, dict)
        or payload.get("method") != "tools/call"
    ):
        return False

    params = payload.get("params")
    if not isinstance(params, dict):
        return False
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        return False
    capability = _APP_SCOPED_TOOL_CAPABILITIES.get(tool_name)
    if capability is None:
        return False
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return False
    app_name = arguments.get("name")
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
