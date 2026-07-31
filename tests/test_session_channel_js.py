"""Behavioral specs for the session-channel page payload, run under node.

Collapsing the channel to one wire verb concentrated the feature's logic in JavaScript:
the `ctx` helpers, the compile modes, and the bounded serializer are the whole surface,
and none of it is reachable from the Python suite. These specs drive real commands
through the payload with a stubbed `window`/`fetch` so that logic is covered.

node is not a declared project dependency — every test here skips when it is absent, so
the suite still passes on a machine without it. What is *not* covered without a real
browser: React fiber traversal, Plotly graph divs, and DOM visibility, all of which need
a rendered Dash page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from dash_server.session_channel.contract import (
    SENTINEL_ITEMS,
    SENTINEL_LENGTH,
    SENTINEL_OMITTED_CHARS,
    SENTINEL_OMITTED_ITEMS,
    SENTINEL_TRUNCATED,
    SENTINEL_TYPE,
)

_ROOT = Path(__file__).resolve().parents[1]
_PAYLOAD = _ROOT / "src" / "dash_server" / "dash_apps" / "assets" / "session_channel.js"
_HARNESS = Path(__file__).parent / "js" / "session_channel_harness.js"

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not available")


def _run(code: str, *, fake_layout: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drive one command through the payload and return the harness report.

    ``fake_layout`` is an optional ``{components: ...}`` Dash layout tree (see
    ``session_channel_harness.js``'s ``SESSION_CHANNEL_FAKE_LAYOUT`` env var) that stubs
    ``window.dash_stores``/``window.dash_component_api.getLayout`` so the
    ``dash_component_api`` prop tier can be exercised without a real browser.
    """

    env = None
    if fake_layout is not None:
        env = {**os.environ, "SESSION_CHANNEL_FAKE_LAYOUT": json.dumps(fake_layout)}
    completed = subprocess.run(
        [str(_NODE), str(_HARNESS), str(_PAYLOAD), code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_ROOT),
        env=env,
    )
    assert completed.returncode == 0, f"harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _result(code: str, *, fake_layout: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run(code, fake_layout=fake_layout)["result"]


# ---------------------------------------------------------------------------
# The payload parses at all
# ---------------------------------------------------------------------------


def test_payload_is_syntactically_valid() -> None:
    """A syntax error here would only ever surface in a user's browser."""

    wrapped = "(" + _PAYLOAD.read_text(encoding="utf-8") + ");"
    check = subprocess.run(
        [str(_NODE), "--check", "-"],
        input=wrapped,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registration_reports_the_mount_and_a_probed_capability_set() -> None:
    report = _run("1")
    registered = report["registered"]

    assert registered["mount_path"] == "/apps/demo"
    assert registered["revision_number"] == 7
    assert registered["session_id"], "the page generates its own per-tab id"
    # With no rendered page, the probe must honestly report that no prop tier works
    # rather than claiming a tier it cannot deliver.
    assert registered["capabilities"]["prop_tier"] == "none"
    assert registered["capabilities"]["set_props"] is True


# ---------------------------------------------------------------------------
# Compile modes
# ---------------------------------------------------------------------------


def test_a_single_expression_is_returned() -> None:
    result = _result("1 + 1")
    assert result["ok"] is True
    assert result["value"] == 2
    assert result["eval_mode"] == "expression"


def test_a_trailing_expression_after_statements_is_returned() -> None:
    result = _result("const a = 2;\n({doubled: a * 2})")
    assert result["ok"] is True
    assert result["value"] == {"doubled": 4}
    assert result["eval_mode"] == "last_line"


def test_an_explicit_return_still_works() -> None:
    result = _result("ctx.out.hi = 'there';\nreturn 5;")
    assert result["ok"] is True
    assert result["value"] == 5
    assert result["out"]["hi"] == "there"
    assert result["eval_mode"] == "statements"


def test_ps27_bug013_semicolon_joined_one_liner_trailing_expression_is_returned() -> None:
    """PS27-BUG-013 / round-1 finding, now actually fixed rather than just documented:
    multiple statements semicolon-joined onto one physical line used to make the
    trailing expression unreachable (the old detector only ever looked at "the last
    physical line" as a whole, and wrapping several semicolon-separated statements in
    one `return (...)` is invalid syntax) - it silently fell through to `statements`
    mode and returned `undefined` with no error. A real, top-level-`;`-aware scan must
    now find the true trailing expression regardless of line layout.
    """

    result = _result("console.log('a'); console.warn('b'); 42")
    assert result["ok"] is True
    assert result["value"] == 42
    assert result["eval_mode"] == "last_line"


def test_ps27_bug007_a_multiline_trailing_expression_is_returned() -> None:
    """PS27-BUG-007: a trailing expression that itself spans multiple physical lines
    (a wrapped array literal is the common real-world shape) used to be split at
    exactly the wrong point - "the last physical line" was only the closing `]`,
    which isn't a valid expression on its own - falling through to `statements` mode
    and silently returning `undefined`.
    """

    code = "const parts = [1, 2, 3];\n[\n  parts.length,\n  parts[0]\n]"
    result = _result(code)
    assert result["ok"] is True
    assert result["value"] == [3, 1]
    assert result["eval_mode"] == "last_line"


def test_ps27_bug007_a_multiline_trailing_expression_after_a_semicolon_joined_prefix() -> None:
    """Both known failure shapes combined: semicolon-joined statements *and* a
    multi-line trailing expression in the same script."""

    code = "const a = 1; const b = 2;\n({\n  sum: a + b,\n  product: a * b\n})"
    result = _result(code)
    assert result["ok"] is True
    assert result["value"] == {"sum": 3, "product": 2}
    assert result["eval_mode"] == "last_line"


def test_ps27_bug007_a_semicolon_inside_a_string_is_not_mistaken_for_a_statement_boundary() -> None:
    result = _result("const label = 'a;b;c';\n({label: label, len: label.length})")
    assert result["ok"] is True
    assert result["value"] == {"label": "a;b;c", "len": 5}
    assert result["eval_mode"] == "last_line"


def test_ps27_bug007_a_semicolon_inside_a_template_literal_interpolation_is_not_a_boundary() -> None:
    code = "const n = 3;\n`total: ${(function () { const x = n; return x + 1; })()};done`"
    result = _result(code)
    assert result["ok"] is True
    assert result["value"] == "total: 4;done"
    assert result["eval_mode"] == "last_line"


def test_ps27_bug007_a_trailing_expression_with_a_nested_function_containing_semicolons() -> None:
    code = "const helper = function () { const a = 1; const b = 2; return a + b; };\nhelper() * 10"
    result = _result(code)
    assert result["ok"] is True
    assert result["value"] == 30
    assert result["eval_mode"] == "last_line"


def test_a_last_line_starting_with_a_keyword_is_not_mistaken_for_an_expression() -> None:
    """Guards the word-boundary fix: `format(...)` must not read as the `for` keyword."""

    result = _result("const format = (x) => x * 3;\nformat(4)")
    assert result["ok"] is True
    assert result["value"] == 12
    assert result["eval_mode"] == "last_line"


def test_await_is_supported() -> None:
    result = _result("await Promise.resolve('done')")
    assert result["ok"] is True
    assert result["value"] == "done"


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------


def test_a_thrown_error_reports_the_line_of_the_submitted_code() -> None:
    """The wrapper prologue must not leak into the reported line number."""

    result = _result("const a = 1;\na.map(x => x)")

    assert result["ok"] is False
    assert result["error"]["name"] == "TypeError"
    assert result["error"]["line"] == 2, result["error"]
    assert result["eval_mode"] == "last_line"


def test_a_syntax_error_is_reported_rather_than_crashing_the_channel() -> None:
    result = _result("const = = =")
    assert result["ok"] is False
    assert "Error" in result["error"]["name"]


# ---------------------------------------------------------------------------
# Bounded serializer
# ---------------------------------------------------------------------------


def test_undefined_is_tagged_rather_than_coerced_to_null() -> None:
    """`undefined` arriving as `null` would be a silent lie about the page's state."""

    result = _result("undefined")
    assert result["value"] == {SENTINEL_TYPE: "undefined"}


@pytest.mark.parametrize(
    ("code", "expected_type"),
    [("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity")],
)
def test_non_json_numbers_are_tagged(code: str, expected_type: str) -> None:
    assert _result(code)["value"] == {SENTINEL_TYPE: expected_type}


def test_a_long_string_is_clipped_with_an_explicit_omitted_count() -> None:
    result = _result("'x'.repeat(5000)")

    value = result["value"]
    assert value[SENTINEL_TYPE] == "string"
    assert value[SENTINEL_TRUNCATED] is True
    assert value[SENTINEL_OMITTED_CHARS] == 1000
    assert len(value[SENTINEL_ITEMS]) == 4000
    assert result["truncated"] is True


def test_a_long_array_keeps_its_true_length_when_clipped() -> None:
    """A clipped array must not look like a complete short one."""

    result = _result("[...Array(300).keys()]")

    value = result["value"]
    assert value[SENTINEL_TYPE] == "array"
    assert value[SENTINEL_LENGTH] == 300
    assert value[SENTINEL_OMITTED_ITEMS] == 100
    assert len(value[SENTINEL_ITEMS]) == 200
    assert result["truncated"] is True


def test_a_circular_structure_does_not_hang_the_page() -> None:
    result = _result("const a = {name: 'a'};\na.self = a;\na")
    assert result["ok"] is True
    assert result["value"]["name"] == "a"
    assert result["value"]["self"] == {SENTINEL_TYPE: "circular"}


def test_a_function_value_is_described_not_dropped() -> None:
    result = _result("({handler: function namedHandler() {}})")
    assert result["value"]["handler"] == "[Function namedHandler]"


def test_a_throwing_getter_does_not_sink_the_whole_snapshot() -> None:
    code = (
        "const obj = {safe: 1};\n"
        "Object.defineProperty(obj, 'boom', {get() { throw new Error('no'); }, enumerable: true});\n"
        "obj"
    )
    result = _result(code)
    assert result["value"]["safe"] == 1
    assert result["value"]["boom"] == {SENTINEL_TYPE: "getter-threw"}


# ---------------------------------------------------------------------------
# ctx helpers reachable without a rendered page
# ---------------------------------------------------------------------------


def test_ctx_page_reports_the_location_and_mount() -> None:
    result = _result("ctx.page()")

    page = result["value"]
    assert page["pathname"] == "/apps/demo"
    assert page["search"] == "?a=1"
    assert page["title"] == "Demo dashboard"
    assert page["mount_path"] == "/apps/demo"
    assert page["revision_number"] == 7
    assert page["viewport"]["width"] == 1280
    assert page["viewport"]["scroll_y"] == 40


def test_ctx_props_reports_its_tier_and_the_ids_it_could_not_find() -> None:
    """Degradation is reported, never hidden.

    With no rendered page there is no prop tier and no such component, so the helper must
    say both — an empty `values` on its own would read as "the component exists and is
    empty", which is a different answer entirely.
    """

    result = _result("ctx.props(['region-filter'])")

    payload = result["value"]
    assert payload["tier"] == "none"
    assert payload["missing"] == ["region-filter"]
    assert payload["values"] == {}


# ---------------------------------------------------------------------------
# PS26-BUG-002: the dash_component_api prop tier
#
# `layoutIndex()` used to call `window.dash_component_api.getLayout()` with no
# argument to get "the whole tree." The real `getLayout(componentPathOrId)` looks up
# exactly one component and throws on `undefined`; the bare try/catch around it
# swallowed that every time, so `ctx.props()`/`ctx.stores()` silently returned nothing
# at this tier while `capabilities.dash_component_api` still reported `true`. These
# specs stub `window.dash_stores` (via SESSION_CHANNEL_FAKE_LAYOUT) the way a real Dash
# page populates it, and would fail against the pre-fix implementation.
# ---------------------------------------------------------------------------

_FAKE_LAYOUT = {
    "components": {
        "type": "Div",
        "namespace": "dash_html_components",
        "props": {
            "children": [
                {
                    "type": "Dropdown",
                    "namespace": "dash_core_components",
                    "props": {
                        "id": "region-filter",
                        "value": "APAC",
                        "options": [{"label": "APAC", "value": "APAC"}],
                    },
                },
                {
                    # A dcc.Store renders no DOM node at all, so this can only ever be
                    # read through the dash_component_api tier - the whole point of the
                    # original bug. `storage_type` is deliberately omitted: Dash's
                    # initial layout JSON only serializes props the app explicitly
                    # passed, and most real `dcc.Store(id=...)` calls never pass
                    # `storage_type` (its Python-side default is "memory"). Only
                    # `modified_timestamp` is guaranteed present once live - see the
                    # PS26-BUG-002 follow-up fix in `readStores()`.
                    "type": "Store",
                    "namespace": "dash_core_components",
                    "props": {
                        "id": "filter-store",
                        "modified_timestamp": 1700000000000,
                        "data": {"selected": ["APAC"]},
                    },
                },
            ]
        },
    }
}


def test_ps26_bug002_ctx_props_reads_real_values_at_the_dash_component_api_tier() -> None:
    result = _result("ctx.props(['region-filter'])", fake_layout=_FAKE_LAYOUT)

    payload = result["value"]
    assert payload["tier"] == "dash_component_api"
    assert payload["partial"] is False
    assert payload["missing"] == []
    assert payload["values"]["region-filter.value"] == "APAC"


def test_ps26_bug002_ctx_props_still_reports_missing_ids_precisely() -> None:
    """A real id lookup miss must stay a miss, not a thrown exception that aborts the
    whole call (the real `getLayout` throws on an unknown id too, see the harness).
    """

    result = _result("ctx.props(['does-not-exist'])", fake_layout=_FAKE_LAYOUT)

    payload = result["value"]
    assert payload["tier"] == "dash_component_api"
    assert payload["missing"] == ["does-not-exist"]
    assert payload["values"] == {}


def test_ps26_bug002_ctx_props_with_no_ids_lists_every_known_component() -> None:
    result = _result("ctx.props([])", fake_layout=_FAKE_LAYOUT)

    payload = result["value"]
    assert payload["tier"] == "dash_component_api"
    assert payload["values"]["region-filter.value"] == "APAC"
    assert "filter-store.data" in payload["values"]


def test_ps26_bug002_ctx_stores_reads_a_store_that_never_renders_to_the_dom() -> None:
    """Also covers the follow-up fix: `_FAKE_LAYOUT`'s Store has no `storage_type` in
    props (realistic - most apps never pass it), so a correct detection heuristic must
    still find it via `modified_timestamp` and report the documented "memory" default.
    """

    result = _result("ctx.stores()", fake_layout=_FAKE_LAYOUT)

    payload = result["value"]
    assert payload["tier"] == "dash_component_api"
    assert payload["partial"] is False
    assert payload["stores"]["filter-store"]["storage_type"] == "memory"
    assert payload["stores"]["filter-store"]["data"] == {"selected": ["APAC"]}


def test_ps26_bug002_capability_probe_reports_dash_component_api_true() -> None:
    report = _run("1", fake_layout=_FAKE_LAYOUT)
    assert report["registered"]["capabilities"]["dash_component_api"] is True


def test_ctx_session_identifies_the_tab() -> None:
    report = _run("ctx.session")
    assert report["result"]["value"]["mount_path"] == "/apps/demo"
    assert report["result"]["value"]["session_id"] == report["registered"]["session_id"]


def test_ctx_set_props_goes_through_the_supported_clientside_api() -> None:
    report = _run("ctx.setProps('region-filter', {value: ['APAC']})")

    assert report["result"]["ok"] is True
    # The channel also uses set_props on its own Interval for adaptive pacing, so filter
    # to the component under test rather than asserting on the whole call list.
    component_calls = [
        call for call in report["set_props_calls"] if not call["id"].startswith("__dash-server")
    ]
    assert component_calls == [{"id": "region-filter", "props": {"value": ["APAC"]}}]


def test_the_channel_repaces_its_own_interval_from_the_server() -> None:
    """Adaptive polling: the server's interval is pushed onto the dcc.Interval."""

    report = _run("1")
    pacing_calls = [
        call for call in report["set_props_calls"] if call["id"].startswith("__dash-server")
    ]
    assert {"id": "__dash-server-session-interval", "props": {"interval": 250}} in pacing_calls


def test_ctx_wait_for_idle_returns_promptly_when_no_callbacks_are_running() -> None:
    result = _result("await ctx.waitForIdle(400)")

    idle = result["value"]
    assert idle["inflight"] == 0
    assert idle["fired"] == []
    assert idle["timed_out"] is True, "no callback traffic ever settled, which is reported honestly"


def test_ps27_bug004_wait_for_idle_detects_an_inline_clientside_callback_settling() -> None:
    """PS27-BUG-004 regression: `waitForIdle` used to watch `fetch` traffic exclusively,
    so a reactive chain driven entirely by a `clientside_callback` (registered under
    `dash_clientside._dashprivate_clientside_funcs`, the inline-string form) never set
    `lastSettle` - the full requested timeout always elapsed even when the update had
    completed instantly. Simulates the real-world shape: the function fires shortly
    after `waitForIdle` starts polling (as it would after a real `ctx.setProps` kicks
    off Dash's reactive dispatch), not before.
    """

    code = (
        "window.dash_clientside._dashprivate_clientside_funcs = "
        "{myhash: function (v) { return v + 1; }};"
        "const p = ctx.waitForIdle(3000);"
        "setTimeout(function () { "
        "window.dash_clientside._dashprivate_clientside_funcs.myhash(41); "
        "}, 30);"
        "return await p;"
    )
    result = _result(code)

    idle = result["value"]
    assert idle["timed_out"] is False, "a clientside-only settle must resolve before the full timeout"
    assert idle["idle_after_ms"] < 3000
    assert idle["fired"] == ["clientside:myhash"]


def test_ps27_bug004_wait_for_idle_detects_a_namespaced_clientside_callback_settling() -> None:
    code = (
        "window.dash_clientside.myns = {echo: function (v) { return v; }};"
        "const p = ctx.waitForIdle(3000);"
        "setTimeout(function () { window.dash_clientside.myns.echo('hi'); }, 30);"
        "return await p;"
    )
    result = _result(code)

    idle = result["value"]
    assert idle["timed_out"] is False
    assert idle["fired"] == ["clientside:myns.echo"]


def test_ps27_bug010_ctx_plots_reads_the_id_off_the_dash_assigned_wrapper() -> None:
    """PS27-BUG-010 regression: `readPlots()` used to read `.id` directly off the
    `.js-plotly-plot` div, but that node is Plotly's own inner element and never
    carries the Dash-assigned id - `dcc.Graph(id="trend-chart")` renders a wrapper
    `<div id="trend-chart">` containing an id-less `.js-plotly-plot` child, confirmed
    against a real rendered page. Every plot's documented `id` field came back `null`
    regardless of the component's real id. Fakes the two-level DOM shape directly
    (this file's harness stubs `document.querySelectorAll` to `[]` since Plotly graph
    divs normally need a real browser - overriding it here is enough to exercise the
    id-walking logic itself without one).
    """

    code = (
        "document.querySelectorAll = function (selector) {"
        "  if (selector !== '.js-plotly-plot') { return []; }"
        "  var wrapper = {id: 'trend-chart', parentElement: null};"
        "  var inner = {"
        "    id: '',"
        "    parentElement: wrapper,"
        "    data: [{type: 'scatter'}],"
        "    _fullLayout: {},"
        "    layout: {}"
        "  };"
        "  return [inner];"
        "};"
        "return ctx.plots();"
    )
    result = _result(code)

    plots = result["value"]
    assert len(plots) == 1
    assert plots[0]["id"] == "trend-chart"


def test_ps27_bug010_ctx_plots_walks_past_multiple_id_less_ancestors() -> None:
    code = (
        "document.querySelectorAll = function (selector) {"
        "  if (selector !== '.js-plotly-plot') { return []; }"
        "  var namedAncestor = {id: 'chart-container', parentElement: null};"
        "  var idLessWrapper = {id: '', parentElement: namedAncestor};"
        "  var inner = {"
        "    id: '',"
        "    parentElement: idLessWrapper,"
        "    data: [],"
        "    _fullLayout: {},"
        "    layout: {}"
        "  };"
        "  return [inner];"
        "};"
        "return ctx.plots();"
    )
    result = _result(code)

    assert result["value"][0]["id"] == "chart-container"


def test_console_output_during_a_command_is_captured() -> None:
    result = _result("console.log('hello', {a: 1});\n42")

    assert result["value"] == 42
    assert result["console"][0]["level"] == "log"
    assert "hello" in result["console"][0]["text"]
    assert '"a":1' in result["console"][0]["text"].replace(" ", "")
