"""Control-plane routes the injected page payload talks to.

Registered by ``create_app`` **only** in local mode with the channel enabled — and
each view re-checks the gate anyway, so a registration bug degrades to 404 rather
than to an open command channel. That redundancy is deliberate: this blueprint is
unauthenticated by design (local mode has no auth), so the mode gate is the only
thing standing between it and the network.

Why these routes live on the control plane rather than on the app's own server:
app names match ``^[a-z][a-z0-9-]*$``, so ``/__dash-server/session/...`` matches no
mount prefix and ``DynamicPrefixDispatcher`` falls through to the control-plane
Flask app. That means the channel needs nothing from the worker — no second
listener, no token, no protocol change — and it behaves identically in
``in_process`` and ``isolated`` runtime mode.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request

from dash_server.exceptions import DashServerError

from .contract import (
    BLUEPRINT_URL_PREFIX,
    ROUTE_POLL,
    ROUTE_REGISTER,
    ROUTE_RESULT,
)
from .service import SessionChannelService


def _service() -> SessionChannelService:
    """Return the enabled channel service, or 404 the request.

    Second of the three enforcement points (injection / route / tool). A hosted-mode
    page never contains the client payload, but if one somehow reached these routes
    they must not answer.
    """

    service = current_app.extensions.get("session_channel_service")
    if service is None or not service.enabled:
        abort(404)
    return service


def create_session_channel_blueprint() -> Blueprint:
    blueprint = Blueprint("session_channel", __name__, url_prefix=BLUEPRINT_URL_PREFIX)

    @blueprint.post(ROUTE_REGISTER)
    def register() -> Any:
        service = _service()
        try:
            payload = service.register(
                request.get_json(silent=True),
                user_agent=request.headers.get("User-Agent"),
            )
        except DashServerError as exc:
            return _error_response(exc)
        return jsonify(payload)

    @blueprint.get(ROUTE_POLL)
    def poll() -> Any:
        service = _service()
        try:
            payload = service.poll(
                request.args.get("session_id"),
                pathname=request.args.get("pathname"),
            )
        except DashServerError as exc:
            return _error_response(exc)
        response = jsonify(payload)
        # The poll response is pure control state; never let an intermediary cache it.
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post(ROUTE_RESULT)
    def result() -> Any:
        service = _service()
        try:
            payload = service.submit_result(request.get_json(silent=True))
        except DashServerError as exc:
            return _error_response(exc)
        return jsonify(payload)

    return blueprint


def _error_response(exc: DashServerError) -> Any:
    response = jsonify({"error": {"category": exc.category, "summary": exc.summary, **exc.details}})
    response.status_code = exc.http_status or 400
    return response


__all__ = ["create_session_channel_blueprint"]
