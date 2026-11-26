"""Seed bundle used for the built-in demo app."""

from __future__ import annotations

from dash_server.live_routes import default_live_route


def build_demo_bundle() -> dict[str, object]:
    """Return the built-in demo bundle."""

    return {
        "manifest": {
            "name": "demo",
            "title": "Demo Dashboard",
            "route": default_live_route("demo"),
            "description": "Built-in dashboard used to prove the hosting shape.",
            "template": "metric-cards",
        },
        "dashboard": {
            "headline": "Demo Dashboard",
            "summary": "Stage 4 proves MCP-driven editing, diagnostics, and revisioned Dash hosting.",
            "metrics": [
                {"label": "Active Apps", "value": "1"},
                {"label": "Registry", "value": "SQLite"},
                {"label": "Control Plane", "value": "MCP"},
            ],
        },
    }
