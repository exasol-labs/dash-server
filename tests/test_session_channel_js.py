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
from pathlib import Path
import shutil
import subprocess

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


def _run(code: str) -> dict:
    """Drive one command through the payload and return the harness report."""

    completed = subprocess.run(
        [str(_NODE), str(_HARNESS), str(_PAYLOAD), code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_ROOT),
    )
    assert completed.returncode == 0, f"harness failed:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _result(code: str) -> dict:
    return _run(code)["result"]


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


def test_console_output_during_a_command_is_captured() -> None:
    result = _result("console.log('hello', {a: 1});\n42")

    assert result["value"] == 42
    assert result["console"][0]["level"] == "log"
    assert "hello" in result["console"][0]["text"]
    assert '"a":1' in result["console"][0]["text"].replace(" ", "")
