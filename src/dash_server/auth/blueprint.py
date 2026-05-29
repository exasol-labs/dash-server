"""Authentication routes for hosted mode."""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, redirect, request

from dash_server.auth import current_auth_context
from dash_server.exceptions import DashServerError


def create_auth_blueprint() -> Blueprint:
    blueprint = Blueprint("auth", __name__)

    @blueprint.get("/auth/whoami")
    def whoami():
        return jsonify({"auth": current_auth_context().to_dict()})

    @blueprint.get("/auth/login")
    def login():
        identity_service = current_app.extensions["identity_service"]
        try:
            login_url = identity_service.oidc_authorization_url(
                next_url=request.args.get("next", "/"),
            )
        except DashServerError as exc:
            return jsonify({"error": exc.to_error_object()}), exc.http_status
        return redirect(login_url)

    @blueprint.get("/auth/callback")
    def callback():
        identity_service = current_app.extensions["identity_service"]
        try:
            _principal, next_url = identity_service.complete_oidc_callback(dict(request.args))
        except DashServerError as exc:
            return jsonify({"error": exc.to_error_object()}), exc.http_status
        return redirect(next_url)

    @blueprint.post("/auth/logout")
    def logout():
        identity_service = current_app.extensions["identity_service"]
        identity_service.logout()
        g.auth_context = identity_service.context_for_request()
        return jsonify({"auth": g.auth_context.to_dict()})

    return blueprint
