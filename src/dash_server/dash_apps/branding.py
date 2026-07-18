"""Shared branding helpers for hosted Dash apps."""

from __future__ import annotations
from typing import Any

from dash import Dash, Input, Output, State, dcc, html
from flask import jsonify

_EXASOL_URL = "https://www.exasol.com/"
_FOOTER_HEIGHT_PX = 32
_REFRESH_INTERVAL_MS = 2000
_REFRESH_INTERVAL_ID = "__dash-server-refresh-interval"
_REFRESH_META_ID = "__dash-server-refresh-meta"
_REFRESH_NOOP_ID = "__dash-server-refresh-noop"
_STATUS_ROUTE = "/__dash-server/status"
_HOSTED_CHROME_ID = "__dash-server-hosted-chrome"
_CATALOG_LINK_ID = "__dash-server-catalog-link"


def apply_hosted_footer(
    dash_app: Dash,
    *,
    mount_path: str | None = None,
    revision_number: int | None = None,
) -> None:
    """Wrap a Dash app layout with the standard hosted-app footer once."""

    if getattr(dash_app, "_dash_server_footer_applied", False):
        return

    original_layout = dash_app.layout
    if not callable(original_layout) and _contains_component_id(original_layout, _HOSTED_CHROME_ID):
        dash_app._dash_server_footer_applied = True  # type: ignore[attr-defined]
        return

    if mount_path and revision_number is not None:
        _register_refresh_status_route(
            dash_app,
            mount_path=mount_path,
            revision_number=revision_number,
        )
        _register_refresh_clientside_callback(dash_app)

    if callable(original_layout):
        def wrapped_layout() -> Any:
            return _with_footer(
                original_layout(),
                mount_path=mount_path,
                revision_number=revision_number,
            )

        dash_app.layout = wrapped_layout
    else:
        dash_app.layout = _with_footer(
            original_layout,
            mount_path=mount_path,
            revision_number=revision_number,
        )

    # Idempotency marker — stashed on the Dash app so re-wrapping is a no-op. Dash's
    # type stubs don't model arbitrary attributes; the read at line 26 uses `getattr`,
    # the write below is the matching set.
    dash_app._dash_server_footer_applied = True  # type: ignore[attr-defined]


def _with_footer(
    content: Any,
    *,
    mount_path: str | None,
    revision_number: int | None,
) -> Any:
    if _contains_component_id(content, _HOSTED_CHROME_ID):
        return content

    children: list[Any] = []
    if mount_path and revision_number is not None:
        children.extend(
            [
                dcc.Store(
                    id=_REFRESH_META_ID,
                    data={
                        "mount_path": mount_path,
                        "revision_number": revision_number,
                    },
                ),
                dcc.Interval(
                    id=_REFRESH_INTERVAL_ID,
                    interval=_REFRESH_INTERVAL_MS,
                    n_intervals=0,
                ),
                html.Div(id=_REFRESH_NOOP_ID, style={"display": "none"}),
            ]
        )
    children.extend(
        [
            html.Div(
                content,
                style={
                    "height": "100vh",
                    "overflowY": "auto",
                    "paddingBottom": f"{_FOOTER_HEIGHT_PX + 12}px",
                    "boxSizing": "border-box",
                },
            ),
            html.Footer(
                [
                    html.A(
                        "Dashboards",
                        id=_CATALOG_LINK_ID,
                        href="/",
                        title="Back to dashboard catalog",
                        style={"color": "#2456e6", "textDecoration": "none"},
                    ),
                    " · Delivered by ",
                    html.A(
                        "Exasol",
                        href=_EXASOL_URL,
                        target="_blank",
                        rel="noopener noreferrer",
                        style={"color": "#2456e6", "textDecoration": "none"},
                    ),
                ],
                style={
                    "backgroundColor": "rgba(255, 255, 255, 0.96)",
                    "borderTop": "1px solid rgba(15, 23, 42, 0.12)",
                    "color": "#475569",
                    "fontFamily": '"Helvetica Neue", Arial, sans-serif',
                    "fontSize": "12px",
                    "height": f"{_FOOTER_HEIGHT_PX}px",
                    "left": "0",
                    "lineHeight": f"{_FOOTER_HEIGHT_PX}px",
                    "position": "fixed",
                    "right": "0",
                    "bottom": "0",
                    "textAlign": "center",
                    "zIndex": "1000",
                },
            ),
        ]
    )
    return html.Div(
        children,
        id=_HOSTED_CHROME_ID,
        style={
            "minHeight": "100vh",
            "position": "relative",
        },
    )


def _contains_component_id(component: Any, component_id: str) -> bool:
    """Return whether a Dash component tree already contains platform chrome."""

    if component is None:
        return False
    if isinstance(component, (list, tuple)):
        return any(_contains_component_id(child, component_id) for child in component)
    if getattr(component, "id", None) == component_id:
        return True
    children = getattr(component, "children", None)
    if children is component:
        return False
    return _contains_component_id(children, component_id)


def _register_refresh_status_route(
    dash_app: Dash,
    *,
    mount_path: str,
    revision_number: int,
) -> None:
    server = dash_app.server
    route_key = "_dash_server_refresh_status_" + mount_path.strip("/").replace("/", "_").replace("-", "_")
    if getattr(server, route_key, False):
        return

    def refresh_status() -> Any:
        return jsonify(
            {
                "mount_path": mount_path,
                "revision_number": revision_number,
                "refresh_interval_ms": _REFRESH_INTERVAL_MS,
            }
        )

    server.add_url_rule(_STATUS_ROUTE, endpoint=route_key, view_func=refresh_status)
    setattr(server, route_key, True)


def _register_refresh_clientside_callback(dash_app: Dash) -> None:
    if getattr(dash_app, "_dash_server_refresh_callback_registered", False):
        return

    dash_app.clientside_callback(
        """
        function(nIntervals, meta) {
          if (!meta || typeof meta.mount_path !== "string" || typeof meta.revision_number !== "number") {
            return "";
          }
          var statusUrl = meta.mount_path.replace(/\\/$/, "") + "/__dash-server/status";
          fetch(statusUrl, {credentials: "same-origin", cache: "no-store"})
            .then(function(response) {
              if (!response.ok) {
                return null;
              }
              return response.json();
            })
            .then(function(payload) {
              if (!payload || typeof payload.revision_number !== "number") {
                return;
              }
              if (payload.revision_number !== meta.revision_number) {
                window.location.reload();
              }
            })
            .catch(function() {
              return null;
            });
          return "";
        }
        """,
        Output(_REFRESH_NOOP_ID, "children"),
        Input(_REFRESH_INTERVAL_ID, "n_intervals"),
        State(_REFRESH_META_ID, "data"),
    )
    dash_app._dash_server_refresh_callback_registered = True  # type: ignore[attr-defined]
