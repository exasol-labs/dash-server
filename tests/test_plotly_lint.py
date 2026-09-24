from __future__ import annotations

import ast

from dash_server.workspace.service import WorkspaceService


def _lint(tmp_path, source: str) -> list[str]:
    workspace = WorkspaceService(str(tmp_path / "workspaces"))
    return [warning["message"] for warning in workspace._lint_tree(ast.parse(source), "app.py")]


def test_lint_flags_apis_removed_in_plotly_7(tmp_path):
    """Hosted apps run on the server's plotly (7.x); these calls fail there at runtime."""

    messages = _lint(
        tmp_path,
        "import plotly.express as px\n"
        "import plotly.graph_objects as go\n"
        "import plotly.figure_factory as ff\n"
        "fig = go.Figure(go.Scattermapbox(lat=[0], lon=[0]))\n"
        "px.density_mapbox(df, lat='lat', lon='lon')\n"
        "ff.create_distplot([[1, 2]], ['a'])\n"
        "ff.create_hexbin_mapbox(df, lat='lat', lon='lon')\n"
        "fig.update_layout(mapbox={'style': 'open-street-map'})\n"
        "fig.write_image('out.png', engine='orca')\n",
    )

    assert any("Scattermapbox was removed in plotly 7; use go.Scattermap" in m for m in messages)
    assert any("density_mapbox was removed in plotly 7; use px.density_map" in m for m in messages)
    assert any("create_distplot was removed in plotly 7" in m for m in messages)
    assert any("create_hexbin_mapbox was removed in plotly 7; use ff.create_hexbin_map" in m for m in messages)
    assert any("layout.mapbox was removed in plotly 7" in m for m in messages)
    assert any("write_image(engine=...) was removed in plotly 7" in m for m in messages)


def test_lint_leaves_plotly_7_replacements_alone(tmp_path):
    messages = _lint(
        tmp_path,
        "import plotly.express as px\n"
        "import plotly.graph_objects as go\n"
        "fig = go.Figure(go.Scattermap(lat=[0], lon=[0]))\n"
        "px.scatter_map(df, lat='lat', lon='lon')\n"
        "fig.update_layout(map={'style': 'open-street-map'})\n"
        "fig.write_image('out.png')\n",
    )

    assert not [m for m in messages if "plotly 7" in m]
