"""Web adapter for governed consumption output and export workflows."""

from __future__ import annotations

import secrets
from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from dash_server.auth import current_auth_context
from dash_server.exceptions import DashServerError


def create_consumption_blueprint() -> Blueprint:
    """Create control-plane routes that cannot collide with mounted Dash apps."""

    blueprint = Blueprint("consumption", __name__)

    @blueprint.get("/manage/apps/<name>/consumption")
    def output_catalog(name: str):
        service = current_app.extensions["consumption_service"]
        auth_context = current_auth_context()
        try:
            catalog = service.list_outputs(name, auth_context)
            if auth_context.principal.is_authenticated:
                exports = service.list_exports(auth_context, app_name=name)
                csrf_token = service.issue_csrf_token(auth_context, f"create:{name}")
            else:
                exports = {"jobs": [], "job_count": 0}
                csrf_token = ""
        except DashServerError as exc:
            return _error_response(exc)
        return render_template(
            "consumption_outputs.html",
            catalog=catalog,
            exports=exports,
            csrf_token=csrf_token,
            idempotency_key=secrets.token_urlsafe(24),
            can_export=auth_context.principal.is_authenticated,
        )

    @blueprint.post("/manage/apps/<name>/exports")
    def create_export(name: str):
        service = current_app.extensions["consumption_service"]
        auth_context = current_auth_context()
        try:
            service.verify_csrf_token(request.form.get("_csrf", ""), auth_context, f"create:{name}")
            parameters = _form_parameters(request.form)
            payload = service.create_export(
                name,
                request.form.get("output_id", ""),
                request.form.get("format", "csv"),
                parameters,
                auth_context,
                idempotency_key=request.form.get("idempotency_key"),
            )
        except DashServerError as exc:
            return _error_response(exc)
        return redirect(url_for("consumption.export_detail", job_id=payload["job"]["id"]), code=303)

    @blueprint.get("/manage/exports/<job_id>")
    def export_detail(job_id: str):
        service = current_app.extensions["consumption_service"]
        auth_context = current_auth_context()
        try:
            payload = service.get_export(job_id, auth_context)
            cancel_token = service.issue_csrf_token(auth_context, f"cancel:{job_id}")
            download = (
                service.create_download_link(job_id, auth_context) if payload["job"]["status"] == "succeeded" else None
            )
        except DashServerError as exc:
            return _error_response(exc)
        return render_template(
            "consumption_export.html",
            export=payload,
            cancel_token=cancel_token,
            download=download,
        )

    @blueprint.post("/manage/exports/<job_id>/cancel")
    def cancel_export(job_id: str):
        service = current_app.extensions["consumption_service"]
        auth_context = current_auth_context()
        try:
            service.verify_csrf_token(request.form.get("_csrf", ""), auth_context, f"cancel:{job_id}")
            service.cancel_export(job_id, auth_context)
        except DashServerError as exc:
            return _error_response(exc)
        return redirect(url_for("consumption.export_detail", job_id=job_id), code=303)

    @blueprint.get("/downloads/<token>")
    def download(token: str):
        try:
            path, artifact = current_app.extensions["consumption_service"].resolve_download(
                token, current_auth_context()
            )
        except DashServerError as exc:
            return _error_response(exc)
        response = send_file(
            path,
            mimetype=artifact.content_type,
            as_attachment=True,
            download_name=artifact.filename,
            conditional=False,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return blueprint


def _form_parameters(form: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for key in form:
        if not key.startswith("param__"):
            continue
        name = key.removeprefix("param__")
        raw_value = form.get(key)
        type_name = form.get(f"type__{name}", "string")
        if raw_value in (None, ""):
            continue
        if type_name == "integer":
            try:
                parameters[name] = int(raw_value)
            except ValueError:
                parameters[name] = raw_value
        elif type_name == "number":
            try:
                parameters[name] = float(raw_value)
            except ValueError:
                parameters[name] = raw_value
        elif type_name == "boolean":
            parameters[name] = str(raw_value).lower() in {"1", "true", "yes", "on"}
        else:
            parameters[name] = raw_value
    return parameters


def _error_response(exc: DashServerError):
    return jsonify({"error": exc.to_error_object()}), exc.http_status


__all__ = ["create_consumption_blueprint"]
