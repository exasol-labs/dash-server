"""Regression specs for PS26-BUG-003: concurrent per-app operations.

Two simultaneous `app_build` calls for the same app used to race on revision-number
allocation, artifact writes, and git tag/commit creation, and any resulting exception
that wasn't a `DashServerError` (a git `CalledProcessError`, a `sqlite3.IntegrityError`,
...) escaped every `except DashServerError` handler between the runtime service and the
MCP dispatcher, surfacing as an unhandled Flask 500 with no diagnostics trail.

The fix has two independent layers, each tested here:
  1. `AppRuntimeService._locked_app_operation` serializes mutating operations per app
     name (a non-blocking `threading.RLock`) and converts any stray exception into a
     structured `DashServerError` with a diagnostics record.
  2. `DispatchMixin._call_tool`/`handle_jsonrpc` carry a backstop: even a tool handler
     that somehow still raises something other than `DashServerError` comes back as a
     clean JSON-RPC/tool-shaped error rather than leaking past Flask's default handler.

Threading tests use a background thread parked *inside* the locked section (synchronized
via `threading.Event`, not sleep-and-hope) so the "concurrent caller is rejected" assertion
is deterministic rather than a timing race.
"""

from __future__ import annotations

import threading

import pytest
from flask import Flask

from dash_server.exceptions import DashServerError

from _mcp_helpers import _call_mcp


def _runtime(app: Flask):
    return app.extensions["runtime_service"]


def _hold_lock_in_background(runtime, app_name: str, operation: str):
    """Start a thread that holds `app_name`'s lock until released; return (thread, release_event).

    Blocks until the background thread has genuinely entered the locked section, so the
    caller can immediately assert against a real, in-progress hold rather than guessing
    at timing.
    """

    entered = threading.Event()
    release = threading.Event()

    def hold():
        with runtime._locked_app_operation(app_name, operation):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert entered.wait(timeout=5), "background thread never entered the locked section"
    return thread, release


def test_ps26_bug003_concurrent_operation_on_the_same_app_is_rejected_immediately(make_app) -> None:
    """The reported repro's core mechanism: a second caller for the same app must get an
    immediate, structured rejection - not block indefinitely, not corrupt shared state.
    """

    runtime = _runtime(make_app())
    thread, release = _hold_lock_in_background(runtime, "demo", "app_build")
    try:
        with pytest.raises(DashServerError) as exc_info:
            with runtime._locked_app_operation("demo", "app_build"):
                pytest.fail("must not enter while another operation holds this app's lock")
        assert exc_info.value.category == "app_operation_in_progress"
        assert exc_info.value.details["app"] == "demo"
    finally:
        release.set()
        thread.join(timeout=5)

    # Once released, a fresh caller proceeds normally - the lock isn't stuck held.
    with runtime._locked_app_operation("demo", "app_build"):
        pass


def test_ps26_bug003_locking_is_per_app_not_global(make_app) -> None:
    """A held lock on one app must never block an operation on a different app."""

    flask_app = make_app()
    runtime = _runtime(flask_app)
    _call_mcp(
        flask_app.test_client(),
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": {
                    "manifest": {
                        "name": "other-app",
                        "title": "Other App",
                        "route": "/apps/other-app",
                        "description": "second app for the per-app lock test",
                        "template": "metric-cards",
                    },
                    "dashboard": {"headline": "Other App", "summary": "s", "metrics": []},
                }
            },
        },
        request_id=1,
    )

    thread, release = _hold_lock_in_background(runtime, "demo", "app_build")
    try:
        with runtime._locked_app_operation("other-app", "app_build"):
            pass  # must not raise - "demo" holding its lock is irrelevant to "other-app"
    finally:
        release.set()
        thread.join(timeout=5)


def test_ps26_bug003_unexpected_exception_becomes_structured_error_with_diagnostics(make_app) -> None:
    """Any exception that isn't already a `DashServerError` must be converted, not leaked,
    and must leave a diagnostics trail (the original bug left none at all).
    """

    flask_app = make_app()
    runtime = _runtime(flask_app)

    with pytest.raises(DashServerError) as exc_info:
        with runtime._locked_app_operation("demo", "app_build"):
            raise RuntimeError("simulated git/sqlite-style failure")

    assert exc_info.value.category == "unexpected_runtime_error"
    assert "RuntimeError" in exc_info.value.summary
    assert "simulated git/sqlite-style failure" in exc_info.value.summary

    errors = runtime.get_errors("demo")["errors"]
    assert errors, "the unexpected exception must be recorded to diagnostics"
    assert errors[0]["category"] == "unexpected_runtime_error"
    assert "RuntimeError" in (errors[0]["traceback_text"] or "")


def test_ps26_bug003_lock_is_released_after_an_exception(make_app) -> None:
    """A failed operation must not leave the app permanently locked (no `finally` bug)."""

    runtime = _runtime(make_app())
    with pytest.raises(DashServerError):
        with runtime._locked_app_operation("demo", "app_build"):
            raise ValueError("boom")

    # If the lock leaked, this would raise `app_operation_in_progress` instead of entering.
    with runtime._locked_app_operation("demo", "app_build"):
        pass


def test_ps26_bug003_dispatch_backstop_converts_unexpected_tool_exceptions(client, app) -> None:
    """End-to-end version of the dispatch-level backstop: even a tool handler that raises
    a plain exception (not a `DashServerError`) must come back as a clean, JSON-parseable
    `isError: true` tool result - never a raw Flask 500 - matching what a JSON-RPC client
    (the exact thing that broke in the original bug) requires to function at all.
    """

    server = app.extensions["mcp_server"]
    original = server._tool_handlers["apps_list"]

    def _boom(arguments):
        raise RuntimeError("simulated unexpected failure")

    server._tool_handlers["apps_list"] = _boom
    try:
        response = _call_mcp(client, "tools/call", {"name": "apps_list", "arguments": {}}, request_id=1)
    finally:
        server._tool_handlers["apps_list"] = original

    assert response.status_code == 200, "must stay a JSON-RPC 200, not an unhandled-exception 500"
    payload = response.get_json()
    assert payload is not None, "response must be JSON, not an HTML error page"
    result = payload["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "unexpected_runtime_error"
    assert "RuntimeError" in result["structuredContent"]["error"]["summary"]
