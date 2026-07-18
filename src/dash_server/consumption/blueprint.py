"""Read-only web adapter for governed consumption output discovery."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template

from dash_server.auth import current_auth_context
from dash_server.exceptions import DashServerError


def create_consumption_blueprint() -> Blueprint:
    """Create control-plane routes that cannot collide with mounted Dash apps."""

    blueprint = Blueprint("consumption", __name__)

    @blueprint.get("/manage/apps/<name>/consumption")
    def output_catalog(name: str):
        try:
            catalog = current_app.extensions["consumption_service"].list_outputs(
                name,
                current_auth_context(),
            )
        except DashServerError as exc:
            return jsonify({"error": exc.to_error_object()}), exc.http_status
        return render_template("consumption_outputs.html", catalog=catalog)

    return blueprint
