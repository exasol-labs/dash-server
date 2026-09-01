"""The ``callback_execution`` probe: does a "healthy" dashboard actually render?

Every other probe in the healthcheck suite is a GET or a direct SQL run, so a
dashboard whose callbacks all blow up used to report ``healthy`` -- the shell
serves, the layout and dependencies endpoints answer, the SQL files parse, and the
page is blank in the browser. These tests pin the two failure modes that matters:

* a callback that raises, which Dash answers with a 500 HTML error page, and
* a callback that *succeeds* and renders the injected Exasol helper's
  ``[{"_error": ...}]`` envelope as an error panel -- an ordinary HTTP 200 whose
  page shows the user nothing but an error.

The apps here are real Dash apps mounted on a real dispatcher and driven over real
``_dash-update-component`` requests. Nothing about the callback path is faked,
because a mocked callback cannot demonstrate that the probe catches a real one.
They are deliberately not marked ``slow``: they build in-process Dash objects with
no subprocess, no dependency install and no polling, and the regression they guard
is worth running on every CI job.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dash import ALL, Dash, Input, Output, State, dcc, html
from flask import Flask
from dash_server.runtime import callback_probe
from dash_server.runtime.dispatcher import DynamicPrefixDispatcher
from dash_server.runtime.prober import HealthProber

MOUNT = "/apps/probe-target"


# --------------------------------------------------------------------- harness


def _fallback(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
    return [b"no such app"]


class _StubService:
    """The only thing ``HealthProber`` needs to run this probe is a dispatcher."""

    def __init__(self, dispatcher: DynamicPrefixDispatcher) -> None:
        self.dispatcher = dispatcher


def _mount(build_layout_and_callbacks: Any, name: str) -> HealthProber:
    """Mount a real Dash app at :data:`MOUNT` and return a prober aimed at it."""

    server = Flask(f"probe_target_{name}")
    dash_app = Dash(
        f"probe_target_{name}",
        server=server,
        routes_pathname_prefix="/",
        requests_pathname_prefix=f"{MOUNT}/",
        suppress_callback_exceptions=True,
    )
    build_layout_and_callbacks(dash_app)
    dispatcher = DynamicPrefixDispatcher(_fallback)
    dispatcher.mount(MOUNT, server)
    return HealthProber(_StubService(dispatcher))  # type: ignore[arg-type]


def _probe(build: Any, name: str) -> dict[str, Any]:
    return _mount(build, name)._callback_execution_probe(MOUNT)


def _seeded_layout() -> html.Div:
    return html.Div([html.Div("seed-value", id="seed"), html.Div(id="out")])


# ----------------------------------------------------------- the failing cases


def test_probe_fails_when_a_callback_raises() -> None:
    """A callback that raises is a 500 with an HTML body -- not JSON, not an envelope."""

    def build(app: Dash) -> None:
        app.layout = _seeded_layout()

        @app.callback(Output("out", "children"), Input("seed", "children"))
        def _boom(_value: str) -> str:
            raise RuntimeError("deliberate callback explosion")

    probe = _probe(build, "raises")

    assert probe["name"] == "callback_execution"
    assert probe["status"] == "failed"
    details = probe["details"]
    # The probe must name *which* callback broke, not merely that one did.
    assert details["first_failed_callback"] == "out.children"
    assert details["callbacks_executed"] == 1
    assert details["callbacks_failed"] == 1
    assert details["failed_callbacks"][0]["status_code"] == 500
    assert "out.children" in details["message"]


def test_probe_fails_on_an_error_panel_returned_with_http_200() -> None:
    """The regression that matters: a broken query is a *successful* callback.

    ``dash_server_exasol.py`` never raises on a data-layer failure -- it returns an
    ``[{"_error": ...}]`` envelope, and the generated app renders it as a panel. HTTP
    is 200, Dash is happy, and the dashboard shows an error. Nothing but this check
    can tell the difference.
    """

    def build(app: Dash) -> None:
        app.layout = _seeded_layout()

        @app.callback(Output("out", "children"), Input("seed", "children"))
        def _panel(_value: str) -> html.Div:
            return html.Div(
                [
                    html.Strong(callback_probe.ERROR_PANEL_HEADING),
                    html.Pre("object SALES.ORDERS not found [line 3, column 14]"),
                ]
            )

    probe = _probe(build, "panel")

    assert probe["status"] == "failed"
    details = probe["details"]
    assert details["first_failed_callback"] == "out.children"
    assert details["failed_callbacks"][0]["status_code"] == 200, (
        "the point of this test: the HTTP layer reported success"
    )
    assert details["error_rendering_outputs"] == ["out.children"]
    # The probe reports the database's own message, so the fix is actionable.
    assert "object SALES.ORDERS not found" in details["latest_error"]


def test_probe_fails_on_a_raw_error_envelope_reaching_the_output() -> None:
    """The unrendered envelope shape counts too, not only the rendered panel."""

    def build(app: Dash) -> None:
        app.layout = _seeded_layout()

        @app.callback(Output("out", "children"), Input("seed", "children"))
        def _raw(_value: str) -> dcc.Store:
            return dcc.Store(id="rows", data=[{"_error": "connection refused"}])

    probe = _probe(build, "envelope")

    assert probe["status"] == "failed"
    assert "connection refused" in probe["details"]["latest_error"]


def test_probe_names_only_the_broken_callback_when_others_are_fine() -> None:
    """A mixed app fails, and the details point at the one callback to fix."""

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [
                html.Div("seed-value", id="seed"),
                html.Div(id="good-one"),
                html.Div(id="good-two"),
                html.Div(id="bad"),
            ]
        )

        @app.callback(Output("good-one", "children"), Input("seed", "children"))
        def _fine_one(value: str) -> str:
            return f"one:{value}"

        @app.callback(Output("good-two", "children"), Input("seed", "children"))
        def _fine_two(value: str) -> str:
            return f"two:{value}"

        @app.callback(Output("bad", "children"), Input("seed", "children"))
        def _broken(_value: str) -> str:
            raise ValueError("only this one is broken")

    probe = _probe(build, "mixed")

    assert probe["status"] == "failed"
    details = probe["details"]
    assert details["callbacks_executed"] == 3
    assert details["callbacks_failed"] == 1
    assert details["first_failed_callback"] == "bad.children"
    assert [failure["output"] for failure in details["failed_callbacks"]] == ["bad.children"]


# ----------------------------------------------------------- the passing cases


def test_probe_passes_on_a_working_callback() -> None:
    def build(app: Dash) -> None:
        app.layout = _seeded_layout()

        @app.callback(Output("out", "children"), Input("seed", "children"))
        def _ok(value: str) -> str:
            return f"rendered:{value}"

    probe = _probe(build, "healthy")

    assert probe["status"] == "passed"
    details = probe["details"]
    assert details["callbacks_executed"] == 1
    assert details["callbacks_failed"] == 0
    assert "error_rendering_outputs" not in details


def test_probe_passes_with_state_and_grouped_multi_output() -> None:
    """Grouped outputs use a different request shape; getting it wrong 500s."""

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [
                dcc.Dropdown(id="region", options=["EMEA"], value="EMEA"),
                dcc.Store(id="filters", data={"days": 30}),
                html.Div(id="left"),
                html.Div(id="right"),
            ]
        )

        @app.callback(
            Output("left", "children"),
            Output("right", "children"),
            Input("region", "value"),
            State("filters", "data"),
        )
        def _pair(region: str, filters: dict[str, Any]) -> tuple[str, str]:
            return f"left:{region}", f"right:{filters['days']}"

    probe = _probe(build, "grouped")

    assert probe["status"] == "passed", probe["details"]
    assert probe["details"]["callbacks_executed"] == 1


def test_probe_treats_prevent_update_as_a_successful_run() -> None:
    """204/PreventUpdate means the callback ran and declined -- that is not a fault."""

    def build(app: Dash) -> None:
        app.layout = _seeded_layout()

        @app.callback(Output("out", "children"), Input("seed", "children"))
        def _decline(_value: str) -> str:
            from dash.exceptions import PreventUpdate

            raise PreventUpdate

    probe = _probe(build, "prevented")

    assert probe["status"] == "passed"
    assert probe["details"]["callbacks_prevented_update"] == 1


def test_empty_figure_is_reported_but_does_not_fail_the_probe() -> None:
    """A figure with no traces is usually a broken query and sometimes a quiet day.

    Reporting it is useful; failing on it would make the healthcheck flaky against
    real data, so it stays advisory.
    """

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [html.Div("seed-value", id="seed"), dcc.Graph(id="chart")]
        )

        @app.callback(Output("chart", "figure"), Input("seed", "children"))
        def _empty(_value: str) -> dict[str, Any]:
            return {"data": [], "layout": {"title": "No rows"}}

    probe = _probe(build, "emptyfig")

    assert probe["status"] == "passed"
    assert probe["details"]["empty_figure_outputs"] == ["chart.figure"]


# ------------------------------------------------------- the degrade-gracefully cases


def test_app_without_callbacks_is_not_applicable_rather_than_failed() -> None:
    """A static dashboard is not a broken dashboard."""

    def build(app: Dash) -> None:
        app.layout = html.Div("nothing dynamic here", id="static")

    probe = _probe(build, "nocallbacks")

    assert probe["status"] == "not_applicable"
    assert probe["details"]["reason"] == "no_callbacks"


def test_prevent_initial_call_callbacks_are_skipped_with_a_reason() -> None:
    """Dash would not fire these on page load either, so neither do we -- loudly."""

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [html.Button("go", id="go", n_clicks=0), html.Div(id="out")]
        )

        @app.callback(
            Output("out", "children"),
            Input("go", "n_clicks"),
            prevent_initial_call=True,
        )
        def _on_click(clicks: int) -> str:
            return f"clicked {clicks}"

    probe = _probe(build, "preventinitial")

    assert probe["status"] == "skipped", "nothing ran, so nothing is proven"
    details = probe["details"]
    assert details["callbacks_executed"] == 0
    assert details["skipped"][0]["output"] == "out.children"
    assert details["skipped"][0]["reason"] == "prevent_initial_call"


def test_pattern_matching_callbacks_are_skipped_not_guessed() -> None:
    """Wildcard ids resolve against components another callback renders later."""

    def build(app: Dash) -> None:
        app.layout = html.Div([html.Div("seed-value", id="seed"), html.Div(id="rows")])

        @app.callback(
            Output("rows", "children"),
            Input({"type": "row", "index": ALL}, "n_clicks"),
        )
        def _rows(_clicks: list[int]) -> str:
            return "never reached by this probe"

    probe = _probe(build, "patternmatching")

    assert probe["status"] == "skipped"
    reasons = {entry["reason"] for entry in probe["details"]["skipped"]}
    assert reasons == {"pattern_matching_id"}


def test_concrete_dict_ids_are_executed_not_skipped() -> None:
    """A dict id that names one real component is addressable, so run it.

    Dash reports dict ids as canonical JSON strings in ``_dash-dependencies`` but as
    dicts in ``_dash-layout``; the probe indexes both to the same key so these
    callbacks are covered instead of written off as absent.
    """

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [
                html.Div("seed-value", id={"type": "src", "index": 1}),
                html.Div(id={"type": "dst", "index": 1}),
            ]
        )

        @app.callback(
            Output({"type": "dst", "index": 1}, "children"),
            Input({"type": "src", "index": 1}, "children"),
        )
        def _concrete(value: str) -> str:
            return f"rendered:{value}"

    probe = _probe(build, "dictids")

    assert probe["status"] == "passed", probe["details"]
    assert probe["details"]["callbacks_executed"] == 1
    assert probe["details"]["callbacks_skipped"] == 0


@pytest.mark.parametrize(
    ("component_id", "expected"),
    [
        ('{"index":["ALL"],"type":"row"}', True),
        ('{"index":["MATCH"],"type":"row"}', True),
        ('{"index":["ALLSMALLER"],"type":"row"}', True),
        ('{"index":1,"type":"row"}', False),
        ("plain-id", False),
        ('{"broken json', False),
    ],
)
def test_is_wildcard_id_separates_wildcards_from_concrete_dict_ids(
    component_id: str, expected: bool
) -> None:
    assert callback_probe.is_wildcard_id(component_id) is expected


def test_probe_is_skipped_when_the_app_is_not_mounted() -> None:
    """No mount means the dependencies endpoint 404s; that is the other probes' story."""

    dispatcher = DynamicPrefixDispatcher(_fallback)
    prober = HealthProber(_StubService(dispatcher))  # type: ignore[arg-type]

    probe = prober._callback_execution_probe(MOUNT)

    assert probe["status"] == "skipped"
    assert "404" in probe["details"]["message"]


def test_probe_stops_starting_callbacks_once_the_time_budget_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget bounds how many requests the probe starts, and says so."""

    def build(app: Dash) -> None:
        app.layout = html.Div(
            [
                html.Div("seed-value", id="seed"),
                html.Div(id="a"),
                html.Div(id="b"),
                html.Div(id="c"),
            ]
        )

        for target in ("a", "b", "c"):

            @app.callback(
                Output(target, "children"),
                Input("seed", "children"),
            )
            def _slow(value: str) -> str:
                return f"rendered:{value}"

    monkeypatch.setattr(callback_probe, "TIME_BUDGET_SECONDS", 0.0)
    probe = _probe(build, "budget")

    # Budget zero means nothing gets started, and every callback says why.
    assert probe["status"] == "skipped"
    details = probe["details"]
    assert details["callbacks_executed"] == 0
    assert {entry["reason"] for entry in details["skipped"]} == {"time_budget_exhausted"}


# ------------------------------------------------- end to end via the MCP surface


_BROKEN_APP_PY = """
from dash import Dash, Input, Output, html


def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix='/',
        requests_pathname_prefix=url_base_pathname.rstrip('/') + '/',
    )
    app.layout = html.Div([
        html.Div('seed', id='seed'),
        html.Div(id='kpi'),
    ])

    @app.callback(Output('kpi', 'children'), Input('seed', 'children'))
    def _kpi(_value):
        raise RuntimeError('SQL object SALES.ORDERS not found')

    return app
"""


def _mcp(client: Any, tool: str, arguments: dict[str, Any], request_id: int) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    return response.get_json()["result"]


def test_app_run_healthcheck_no_longer_reports_healthy_for_a_broken_callback(
    client: Any,
) -> None:
    """The whole point, through the tool an agent actually calls.

    Every GET-based probe passes on this app -- the route serves, the layout and
    dependencies endpoints answer, the assets resolve -- and its one callback raises.
    Before the callback_execution probe this returned ``healthy``.
    """

    manifest = json.dumps(
        {
            "name": "broken-cb",
            "title": "Broken Callback Dashboard",
            "route": "/apps/broken-cb",
            "template": "metric-cards",
        }
    )
    created = _mcp(
        client,
        "app_create_from_files",
        {
            "name": "broken-cb",
            "title": "Broken Callback Dashboard",
            "files": [
                {"path": "app.py", "content": _BROKEN_APP_PY},
                {"path": "dash-app.json", "content": manifest},
                {"path": "requirements.txt", "content": "dash>=4.0,<5.0\n"},
            ],
        },
        request_id=8010,
    )
    assert created.get("isError") is False, created

    deployed = _mcp(
        client,
        "app_deploy_draft",
        {"name": "broken-cb", "deployment_target": "live"},
        request_id=8011,
    )
    assert deployed.get("isError") is False, deployed

    health = _mcp(client, "app_run_healthcheck", {"name": "broken-cb"}, request_id=8012)
    assert health.get("isError") is False, health
    payload = health["structuredContent"]["health"]
    probes = {probe["name"]: probe for probe in payload["probes"]}

    # Everything that could see the app before this change still says it is fine.
    for name in ("http_ready", "dash_layout", "dash_dependencies", "static_assets"):
        assert probes[name]["status"] == "passed", name

    assert probes["callback_execution"]["status"] == "failed"
    assert probes["callback_execution"]["details"]["first_failed_callback"] == "kpi.children"
    assert payload["status"] == "degraded", (
        "a dashboard whose only callback raises must not come back healthy"
    )


def test_app_run_healthcheck_stays_healthy_for_a_working_callback(client: Any) -> None:
    """The other half of the guarantee: the probe must not cry wolf."""

    working_app_py = _BROKEN_APP_PY.replace(
        "        raise RuntimeError('SQL object SALES.ORDERS not found')",
        "        return 'KPI: 42'",
    )
    manifest = json.dumps(
        {
            "name": "working-cb",
            "title": "Working Callback Dashboard",
            "route": "/apps/working-cb",
            "template": "metric-cards",
        }
    )
    created = _mcp(
        client,
        "app_create_from_files",
        {
            "name": "working-cb",
            "title": "Working Callback Dashboard",
            "files": [
                {"path": "app.py", "content": working_app_py},
                {"path": "dash-app.json", "content": manifest},
                {"path": "requirements.txt", "content": "dash>=4.0,<5.0\n"},
            ],
        },
        request_id=8020,
    )
    assert created.get("isError") is False, created

    deployed = _mcp(
        client,
        "app_deploy_draft",
        {"name": "working-cb", "deployment_target": "live"},
        request_id=8021,
    )
    assert deployed.get("isError") is False, deployed

    health = _mcp(client, "app_run_healthcheck", {"name": "working-cb"}, request_id=8022)
    payload = health["structuredContent"]["health"]
    probes = {probe["name"]: probe for probe in payload["probes"]}

    assert probes["callback_execution"]["status"] == "passed"
    assert probes["callback_execution"]["details"]["callbacks_executed"] >= 1
    assert payload["status"] == "healthy"


# ----------------------------------------------------------------- unit checks


@pytest.mark.parametrize(
    ("encoded", "expected", "grouped"),
    [
        ("out.children", [{"id": "out", "property": "children"}], False),
        (
            "..left.children...right.children..",
            [
                {"id": "left", "property": "children"},
                {"id": "right", "property": "children"},
            ],
            True,
        ),
        (
            "my\\.dotted\\.id.figure",
            [{"id": "my.dotted.id", "property": "figure"}],
            False,
        ),
    ],
)
def test_split_output_targets_inverts_dash_callback_ids(
    encoded: str, expected: list[dict[str, str]], grouped: bool
) -> None:
    parsed = callback_probe.split_output_targets(encoded)

    assert parsed is not None
    targets, is_grouped = parsed
    assert targets == expected
    assert is_grouped is grouped


@pytest.mark.parametrize("encoded", ["", "no-property-here"])
def test_split_output_targets_refuses_shapes_it_cannot_parse(encoded: str) -> None:
    """Returning None lets the caller skip rather than POST a body Dash rejects."""

    assert callback_probe.split_output_targets(encoded) is None


def test_index_layout_props_survives_a_deeply_nested_layout() -> None:
    """The walk is iterative: a deep layout must not hit the recursion limit."""

    node: dict[str, Any] = {"props": {"id": "leaf", "children": "done"}}
    for depth in range(3000):
        node = {"props": {"id": f"node-{depth}", "children": [node]}}

    index = callback_probe.index_layout_props(node)

    assert index["leaf"]["children"] == "done"


def test_plan_callbacks_skips_a_callback_whose_input_is_not_in_the_layout() -> None:
    dependencies = [
        {
            "output": "out.children",
            "inputs": [{"id": "rendered-later", "property": "value"}],
            "state": [],
        }
    ]

    plans, skipped = callback_probe.plan_callbacks(dependencies, {"out": {"id": "out"}})

    assert plans == []
    assert skipped[0].reason == "input_not_in_initial_layout"
    assert skipped[0].detail == "rendered-later.value"


def test_plan_callbacks_binds_a_missing_property_to_none() -> None:
    """An unset prop (a button's ``n_clicks``) binds to None, exactly as Dash sends it."""

    dependencies = [
        {
            "output": "out.children",
            "inputs": [{"id": "go", "property": "n_clicks"}],
            "state": [],
        }
    ]

    plans, skipped = callback_probe.plan_callbacks(dependencies, {"go": {"id": "go"}})

    assert skipped == []
    assert plans[0].body["inputs"] == [
        {"id": "go", "property": "n_clicks", "value": None}
    ]
    assert plans[0].body["changedPropIds"] == []


def test_plan_callbacks_honours_the_callback_ceiling() -> None:
    dependencies = [
        {"output": f"out-{index}.children", "inputs": [], "state": []}
        for index in range(callback_probe.MAX_CALLBACKS + 3)
    ]

    plans, skipped = callback_probe.plan_callbacks(dependencies, {})

    assert len(plans) == callback_probe.MAX_CALLBACKS
    assert {entry.reason for entry in skipped} == {"callback_limit_reached"}


def test_classify_response_reads_a_dash_error_envelope() -> None:
    outcome = callback_probe.classify_response(
        "out.children", 200, {"error": {"message": "boom"}}
    )

    assert outcome.ok is False
    assert "boom" in (outcome.error or "")


def test_classify_response_summarizes_an_html_error_page() -> None:
    """Dash serves HTML when dev tools are off; report it as text, not markup."""

    outcome = callback_probe.classify_response(
        "out.children",
        500,
        None,
        body_text="<html><body><h1>Internal Server Error</h1></body></html>",
    )

    assert outcome.ok is False
    assert "<" not in (outcome.error or "")
    assert "Internal Server Error" in (outcome.error or "")


def test_classify_response_does_not_cry_error_over_ordinary_content() -> None:
    """A dashboard charting an error rate is not a dashboard in error."""

    outcome = callback_probe.classify_response(
        "out.children",
        200,
        {
            "multi": True,
            "response": {
                "out": {
                    "children": {
                        "props": {"children": "Error rate: 0.4% (within budget)"},
                        "type": "Div",
                        "namespace": "dash_html_components",
                    }
                }
            },
        },
    )

    assert outcome.ok is True
    assert outcome.error_outputs == []
