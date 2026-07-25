"""Shared branding helpers for hosted Dash apps."""

from __future__ import annotations
from pathlib import Path
from typing import Any

from dash import Dash, Input, Output, State, dcc, html
from flask import jsonify

_EXASOL_URL = "https://www.exasol.com/"
_FOOTER_HEIGHT_PX = 32
_REFRESH_INTERVAL_MS = 2000

# Single source of truth for the `__dash-server` element ids the hosted chrome
# injects. The auto-refresh clientside callback (see `hosted_refresh.js`) binds
# to these; keeping them here avoids the ids drifting between Python and JS.
_REFRESH_INTERVAL_ID = "__dash-server-refresh-interval"
_REFRESH_META_ID = "__dash-server-refresh-meta"
_REFRESH_NOOP_ID = "__dash-server-refresh-noop"
_STATUS_ROUTE = "/__dash-server/status"
_HOSTED_CHROME_ID = "__dash-server-hosted-chrome"
_CATALOG_LINK_ID = "__dash-server-catalog-link"
_EXPORTS_LINK_ID = "__dash-server-exports-link"

# The auto-refresh clientside callback body lives in a dedicated assets file so
# it is version-controlled, lint/test-visible, and read exactly once at import
# time rather than re-parsed from an embedded literal on every registration.
_REFRESH_JS_PATH = Path(__file__).with_name("assets") / "hosted_refresh.js"
_REFRESH_CLIENTSIDE_JS = _REFRESH_JS_PATH.read_text(encoding="utf-8")


def apply_hosted_footer(
    dash_app: Dash,
    *,
    mount_path: str | None = None,
    revision_number: int | None = None,
    app_name: str | None = None,
    has_consumption_outputs: bool = False,
    wrap: bool = True,
) -> None:
    """Wrap a Dash app layout with the standard hosted-app footer once.

    Wrapping is gated on the explicit ``wrap`` flag supplied by the caller — the
    call site decides whether the app should receive hosted chrome rather than
    branding inferring it from a layout tree-walk. Idempotence is guarded
    explicitly: a marker attribute stashed on the Dash app records that chrome
    was applied, and a layout whose root already carries the hosted-chrome id is
    left untouched (an O(1) root check, not a recursive scan of ``children``).
    """

    if not wrap:
        return

    if getattr(dash_app, "_dash_server_footer_applied", False):
        return

    original_layout = dash_app.layout
    if not callable(original_layout) and _is_hosted_chrome(original_layout):
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
                app_name=app_name,
                has_consumption_outputs=has_consumption_outputs,
            )

        dash_app.layout = wrapped_layout
    else:
        dash_app.layout = _with_footer(
            original_layout,
            mount_path=mount_path,
            revision_number=revision_number,
            app_name=app_name,
            has_consumption_outputs=has_consumption_outputs,
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
    app_name: str | None,
    has_consumption_outputs: bool,
) -> Any:
    if _is_hosted_chrome(content):
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
    footer_children: list[Any] = [
        html.A(
            "Dashboards",
            id=_CATALOG_LINK_ID,
            href="/",
            title="Back to dashboard catalog",
            style={"color": "#2456e6", "textDecoration": "none"},
        )
    ]
    if has_consumption_outputs and app_name:
        footer_children.extend(
            [
                " · ",
                html.A(
                    "Export",
                    id=_EXPORTS_LINK_ID,
                    href=f"/manage/apps/{app_name}/consumption",
                    title="Export a governed dashboard output",
                    style={"color": "#2456e6", "textDecoration": "none"},
                ),
            ]
        )
    footer_children.extend(
        [
            " · ",
            "Delivered by ",
            html.A(
                "Exasol",
                href=_EXASOL_URL,
                target="_blank",
                rel="noopener noreferrer",
                style={"color": "#2456e6", "textDecoration": "none"},
            ),
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
                footer_children,
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


def _is_hosted_chrome(component: Any) -> bool:
    """Return whether ``component`` is *already* a hosted-chrome wrapper.

    The wrapper `_with_footer` builds always carries `_HOSTED_CHROME_ID` on its
    root ``Div``, so an O(1) root-id check reliably detects a previously wrapped
    layout. This deliberately replaces the old recursive `children` walk, which
    was fragile: it descended only into ``children`` and missed components that
    store layout in other props.
    """

    if isinstance(component, (list, tuple)):
        return False
    return getattr(component, "id", None) == _HOSTED_CHROME_ID


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
        _REFRESH_CLIENTSIDE_JS,
        Output(_REFRESH_NOOP_ID, "children"),
        Input(_REFRESH_INTERVAL_ID, "n_intervals"),
        State(_REFRESH_META_ID, "data"),
    )
    dash_app._dash_server_refresh_callback_registered = True  # type: ignore[attr-defined]
