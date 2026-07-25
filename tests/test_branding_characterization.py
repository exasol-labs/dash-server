"""Characterization pins for hosted-chrome injection (`dash_apps/branding.py`).

These snapshot the *current* rendered-chrome behavior so the P7 hardening
refactor is provably behavior-preserving. They assert on the wrapped layout
tree (component ids, catalog/exports links) and the refresh mechanism
(store/interval/noop components, status route, clientside callback), plus the
double-application idempotence guard.

They are written against the public `apply_hosted_footer` API and are green
both before and after the refactor.
"""

from __future__ import annotations

from typing import Any

from dash import Dash, html
from flask import Flask

from dash_server.dash_apps.branding import apply_hosted_footer


def _iter_components(component: Any):
    """Yield every Dash component in a layout tree (depth-first)."""

    if component is None:
        return
    if isinstance(component, (list, tuple)):
        for child in component:
            yield from _iter_components(child)
        return
    if not hasattr(component, "children") and getattr(component, "id", None) is None:
        # Plain scalar (e.g. a string footer separator).
        return
    yield component
    children = getattr(component, "children", None)
    if children is not None and children is not component:
        yield from _iter_components(children)


def _ids(layout: Any) -> list[str]:
    return [
        cid
        for cid in (getattr(c, "id", None) for c in _iter_components(layout))
        if cid is not None
    ]


def _find_by_id(layout: Any, component_id: str) -> Any:
    for component in _iter_components(layout):
        if getattr(component, "id", None) == component_id:
            return component
    return None


def _make_app(name: str) -> Dash:
    dash_app = Dash(name, server=Flask(name))
    dash_app.layout = html.Div([html.H1("Original content")], id="user-root")
    return dash_app


def test_apply_hosted_footer_wraps_with_chrome_and_links():
    dash_app = _make_app("chrome-basic")

    apply_hosted_footer(
        dash_app,
        mount_path="/apps/demo",
        revision_number=7,
        app_name="demo",
        has_consumption_outputs=True,
    )

    layout = dash_app.layout
    # The wrapped layout root carries the hosted-chrome id.
    assert getattr(layout, "id", None) == "__dash-server-hosted-chrome"

    ids = _ids(layout)
    # Original user content is preserved inside the chrome.
    assert "user-root" in ids
    # Catalog + exports links are present.
    assert "__dash-server-catalog-link" in ids
    assert "__dash-server-exports-link" in ids

    catalog = _find_by_id(layout, "__dash-server-catalog-link")
    assert catalog.href == "/"
    exports = _find_by_id(layout, "__dash-server-exports-link")
    assert exports.href == "/manage/apps/demo/consumption"


def test_apply_hosted_footer_installs_refresh_mechanism():
    dash_app = _make_app("chrome-refresh")

    apply_hosted_footer(
        dash_app,
        mount_path="/apps/demo",
        revision_number=3,
        app_name="demo",
    )

    ids = _ids(dash_app.layout)
    assert "__dash-server-refresh-meta" in ids
    assert "__dash-server-refresh-interval" in ids
    assert "__dash-server-refresh-noop" in ids

    # The refresh metadata store carries the mount path + revision number.
    meta = _find_by_id(dash_app.layout, "__dash-server-refresh-meta")
    assert meta.data == {"mount_path": "/apps/demo", "revision_number": 3}

    # The status route backing the auto-refresh poll is registered.
    rules = {rule.rule for rule in dash_app.server.url_map.iter_rules()}
    assert "/__dash-server/status" in rules

    # The clientside refresh callback is registered exactly once.
    assert getattr(dash_app, "_dash_server_refresh_callback_registered", False) is True


def test_apply_hosted_footer_is_idempotent():
    dash_app = _make_app("chrome-idempotent")

    apply_hosted_footer(
        dash_app,
        mount_path="/apps/demo",
        revision_number=1,
        app_name="demo",
        has_consumption_outputs=True,
    )
    first_layout = dash_app.layout

    apply_hosted_footer(
        dash_app,
        mount_path="/apps/demo",
        revision_number=1,
        app_name="demo",
        has_consumption_outputs=True,
    )
    second_layout = dash_app.layout

    # Re-application is a no-op: same layout object, not double-wrapped.
    assert second_layout is first_layout
    chrome_count = sum(
        1 for cid in _ids(second_layout) if cid == "__dash-server-hosted-chrome"
    )
    assert chrome_count == 1


def test_apply_hosted_footer_leaves_already_chromed_layout_untouched():
    dash_app = Dash("chrome-preexisting", server=Flask("chrome-preexisting"))
    existing = html.Div([html.Div("chrome")], id="__dash-server-hosted-chrome")
    dash_app.layout = existing

    apply_hosted_footer(dash_app, mount_path="/apps/demo", revision_number=1)

    assert dash_app.layout is existing


def test_apply_hosted_footer_callable_layout_wrapped_on_render():
    dash_app = Dash("chrome-callable", server=Flask("chrome-callable"))
    dash_app.layout = lambda: html.Div([html.H1("dynamic")], id="user-root")

    apply_hosted_footer(dash_app, mount_path="/apps/demo", revision_number=5)

    rendered = dash_app.layout()
    assert getattr(rendered, "id", None) == "__dash-server-hosted-chrome"
    assert "user-root" in _ids(rendered)
