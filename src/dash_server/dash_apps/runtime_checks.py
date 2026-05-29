"""Runtime verification helpers for mounted Dash apps."""

from __future__ import annotations

from typing import Any

from flask import Flask


def verify_dash_mount(server: Flask) -> dict[str, Any]:
    """Check that a mounted Dash/Flask server serves the expected root and layout endpoints."""

    client = server.test_client()
    root_response = client.get("/", follow_redirects=True)
    if root_response.status_code >= 400:
        return {
            "status": "failed",
            "category": "route_misconfiguration",
            "message": (
                "The app did not serve '/' at the mounted root. Ensure create_dash_app "
                "uses routes_pathname_prefix='/' and requests_pathname_prefix=url_base_pathname.rstrip('/') + '/'."
            ),
            "path": "/",
            "status_code": root_response.status_code,
        }

    layout_response = client.get("/_dash-layout", follow_redirects=True)
    if layout_response.status_code >= 400:
        return {
            "status": "failed",
            "category": "route_misconfiguration",
            "message": (
                "The app did not serve '/_dash-layout' at the mounted root. Ensure the app "
                "uses the provided url_base_pathname instead of registering absolute internal routes."
            ),
            "path": "/_dash-layout",
            "status_code": layout_response.status_code,
        }

    return {"status": "passed"}
