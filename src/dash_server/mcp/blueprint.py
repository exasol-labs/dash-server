"""Flask blueprint exposing the `/mcp` control-plane endpoint."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from dash_server.auth import current_auth_context


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
