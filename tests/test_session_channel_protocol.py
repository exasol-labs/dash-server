"""End-to-end specs for the session channel, driven through `/mcp`.

A `StubPage` speaks the register/poll/result protocol from a background thread, which
is what the injected JavaScript does in a real browser. That lets the whole round trip
— tool call, queue, poll, result, envelope — be exercised without a browser.
"""

from __future__ import annotations

import threading
from typing import Any
from collections.abc import Callable

import pytest
from flask import Flask

from dash_server.session_channel.contract import (
    BLUEPRINT_URL_PREFIX,
    ROUTE_POLL,
    ROUTE_REGISTER,
    ROUTE_RESULT,
    SENTINEL_TRUNCATED,
    TIER_REACT_FIBER,
)

from _helpers import call_mcp, read_resource_json, wait_for

Responder = Callable[[dict[str, Any]], dict[str, Any] | None]


class StubPage:
    """A fake browser tab: registers, polls, and answers commands."""

    def __init__(
        self,
        app: Flask,
        responder: Responder,
        *,
        mount_path: str = "/apps/demo",
        session_id: str = "tab-1",
        revision_number: int = 1,
    ) -> None:
        self.app = app
        self.responder = responder
        self.mount_path = mount_path
        self.session_id = session_id
        self.revision_number = revision_number
        self.answered: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, client: Any | None = None) -> Any:
        client = client or self.app.test_client()
        return client.post(
            f"{BLUEPRINT_URL_PREFIX}{ROUTE_REGISTER}",
            json={
                "session_id": self.session_id,
                "mount_path": self.mount_path,
                "revision_number": self.revision_number,
                "pathname": self.mount_path,
                "capabilities": {"prop_tier": TIER_REACT_FIBER, "set_props": True},
            },
        )

    def start(self) -> StubPage:
        self.register()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="stub-page")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # Each thread gets its own test client; the Flask client is not shared.
        client = self.app.test_client()
        while not self._stop.is_set():
            response = client.get(f"{BLUEPRINT_URL_PREFIX}{ROUTE_POLL}?session_id={self.session_id}")
            if response.status_code == 200:
                payload = response.get_json() or {}
                if payload.get("register_required"):
                    self.register(client)
                else:
                    command = payload.get("command")
                    if command:
                        self._answer(client, command)
            self._stop.wait(0.01)

    def _answer(self, client: Any, command: dict[str, Any]) -> None:
        reply = self.responder(command)
        if reply is None:
            return  # A page that never answers — exercises the server deadline.
        body = dict(reply)
        body.setdefault("session_id", self.session_id)
        body.setdefault("command_id", command["command_id"])
        self.answered.append(command)
        client.post(f"{BLUEPRINT_URL_PREFIX}{ROUTE_RESULT}", json=body)


@pytest.fixture()
def page(app: Flask):
    """Factory fixture that guarantees the stub page thread is stopped."""

    pages: list[StubPage] = []

    def _make(responder: Responder, **kwargs: Any) -> StubPage:
        stub = StubPage(app, responder, **kwargs).start()
        pages.append(stub)
        return stub

    yield _make
    for stub in pages:
        stub.stop()


def _echo_responder(value: Any) -> Responder:
    def _respond(command: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "value": value,
            "out": {"code_seen": command["code"]},
            "console": [{"level": "log", "text": "hello"}],
            "duration_ms": 12,
            "eval_mode": "expression",
            "tier_used": TIER_REACT_FIBER,
            "truncated": False,
        }

    return _respond


def _tool_payload(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_eval_round_trip_returns_the_pages_value(client, page) -> None:
    page(_echo_responder({"region-filter.value": ["EMEA"]}))

    result = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "ctx.props(['region-filter'])"})
    )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["ok"] is True
    assert payload["value"] == {"region-filter.value": ["EMEA"]}
    assert payload["out"]["code_seen"] == "ctx.props(['region-filter'])"
    assert payload["console"] == [{"level": "log", "text": "hello"}]
    assert payload["eval_mode"] == "expression"
    assert payload["tier_used"] == TIER_REACT_FIBER
    assert payload["session"]["session_id"] == "tab-1"
    assert payload["session"]["mount_path"] == "/apps/demo"
    assert payload["session"]["mount_kind"] == "live"
    # Freshness markers, so an agent can never mistake a replayed answer for a new one.
    assert payload["captured_at"]
    assert payload["command_seq"] == 1


def test_sessions_list_reports_liveness_and_capabilities(client, page) -> None:
    page(_echo_responder(None))

    result = _tool_payload(call_mcp(client, "app_sessions_list", {"name": "demo"}))

    payload = result["structuredContent"]
    assert payload["live_count"] == 1
    session = payload["sessions"][0]
    assert session["session_id"] == "tab-1"
    assert session["app"] == "demo"
    assert session["live"] is True
    assert session["capabilities"]["prop_tier"] == TIER_REACT_FIBER


def test_auto_targets_the_only_live_session(client, page) -> None:
    page(_echo_responder(1), session_id="only-tab")

    payload = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "1", "session_id": "auto"})
    )["structuredContent"]

    assert payload["session"]["session_id"] == "only-tab"


# ---------------------------------------------------------------------------
# Code failure is a result, not a transport error
# ---------------------------------------------------------------------------


def test_a_page_side_exception_comes_back_as_a_result_with_the_submitted_line(client, page) -> None:
    """The agent needs the message and the line it wrote, not an error envelope."""

    def _thrower(_command: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "name": "TypeError",
                "message": "Cannot read properties of undefined (reading 'map')",
                "stack": "TypeError: ...\n    at <anonymous>:4:11",
                "line": 2,
            },
            "console": [],
            "duration_ms": 3,
            "eval_mode": "statements",
        }

    page(_thrower)

    result = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "const a = 1;\na.map(x => x)"})
    )

    assert result["isError"] is False, "a page-side throw is a result, not a tool error"
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert payload["error"]["name"] == "TypeError"
    assert payload["error"]["line"] == 2
    assert "line 2 of the submitted code" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


def test_eval_without_a_live_tab_is_an_error_that_lists_candidates(client) -> None:
    result = _tool_payload(call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "1"}))

    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "session_channel_session_gone"
    assert error["details"]["reason"] == "no_live_session"
    assert error["details"]["live_sessions"] == []
    guidance = result["structuredContent"]["guidance"]
    assert "app_sessions_list" in guidance["suggested_tools"]


def test_eval_times_out_when_the_page_never_answers(client, page) -> None:
    page(lambda _command: None)  # Polls (so it stays live) but never posts a result.

    result = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "1", "timeout_seconds": 1})
    )

    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "session_channel_timeout"
    assert error["details"]["timeout_seconds"] == 1
    assert "cannot be cancelled" in error["details"]["hint"]


def test_a_second_concurrent_command_is_refused_rather_than_queued(app, client, page) -> None:
    page(_echo_responder(1), session_id="tab-busy")
    service = app.extensions["session_channel_service"]
    wait_for(
        lambda: service.registry.get("tab-busy") is not None,
        message="stub page registration",
    )
    # Occupy the single slot directly: the point under test is the refusal, not the
    # first command's own round trip.
    service.queue.enqueue(session_id="tab-busy", code="1", timeout_seconds=30, command_seq=99)

    result = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "2", "session_id": "tab-busy"})
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "session_channel_busy"


def test_a_stale_tab_is_reported_gone_rather_than_answered_from_memory(app, client) -> None:
    """The failure mode this feature exists to prevent."""

    service = app.extensions["session_channel_service"]
    service.register(
        {
            "session_id": "closed-tab",
            "mount_path": "/apps/demo",
            "revision_number": 1,
            "capabilities": {},
        }
    )
    # Back-date the liveness clock rather than sleeping past the stale window: the
    # monotonic clock is the thing under test, and a wall-clock race here would make
    # the spec flaky in both directions.
    session = service.registry.get("closed-tab")
    assert session is not None
    session.last_poll_monotonic -= 3600

    result = _tool_payload(
        call_mcp(
            client,
            "app_session_eval_js",
            {"name": "demo", "code": "1", "session_id": "closed-tab"},
        )
    )

    error = result["structuredContent"]["error"]
    assert error["category"] == "session_channel_session_gone"
    assert error["details"]["reason"] == "stale"
    assert "do not report" in error["details"]["hint"].lower()


def test_unknown_app_is_reported_before_the_session_lookup(client) -> None:
    """A typo must not read as "the user has no tab open"."""

    result = _tool_payload(call_mcp(client, "app_session_eval_js", {"name": "no-such-app", "code": "1"}))

    assert result["structuredContent"]["error"]["category"] == "app_not_found"


def test_oversized_code_is_rejected_before_dispatch(client, page) -> None:
    page(_echo_responder(1))

    result = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "x" * 20000})
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["details"]["field"] == "code"


# ---------------------------------------------------------------------------
# Server-side caps
# ---------------------------------------------------------------------------


def test_an_oversized_page_result_is_truncated_by_the_control_plane(app, client, page) -> None:
    app.extensions["session_channel_service"].max_result_bytes = 2048
    page(_echo_responder([{"row": "x" * 500} for _ in range(200)]))

    payload = _tool_payload(
        call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "big"})
    )["structuredContent"]

    assert payload["truncated"] is True
    assert payload["value"][SENTINEL_TRUNCATED] is True


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_hosted_mode_refuses_the_tool_and_serves_no_channel_routes(make_hosted_app) -> None:
    hosted = make_hosted_app()
    hosted_client = hosted.test_client()
    headers = {"X-Forwarded-User": "admin-1", "X-Forwarded-Email": "admin@example.test"}

    result = _tool_payload(
        call_mcp(hosted_client, "app_session_eval_js", {"name": "demo", "code": "1"}, headers=headers)
    )
    assert result["structuredContent"]["error"]["category"] == "session_channel_unavailable"
    assert result["structuredContent"]["error"]["details"]["reason"] == "hosted_mode"

    for path, method in (
        (f"{BLUEPRINT_URL_PREFIX}{ROUTE_POLL}?session_id=x", "get"),
        (f"{BLUEPRINT_URL_PREFIX}{ROUTE_REGISTER}", "post"),
        (f"{BLUEPRINT_URL_PREFIX}{ROUTE_RESULT}", "post"),
    ):
        response = getattr(hosted_client, method)(path, json={})
        assert response.status_code == 404, f"{path} must not be served in hosted mode"


def test_a_non_loopback_bind_disables_the_channel(make_app) -> None:
    """Local mode plus a public bind would publish an unauthenticated command channel."""

    exposed = make_app(DASH_SERVER_HOST="0.0.0.0")
    service = exposed.extensions["session_channel_service"]

    assert service.enabled is False
    assert service.disabled_reason == "non_loopback_bind"
    assert exposed.test_client().get(f"{BLUEPRINT_URL_PREFIX}{ROUTE_POLL}?session_id=x").status_code == 404


def test_the_non_loopback_override_is_honored_when_set_explicitly(make_app) -> None:
    exposed = make_app(DASH_SERVER_HOST="0.0.0.0", SESSION_CHANNEL_ALLOW_NON_LOOPBACK=True)
    assert exposed.extensions["session_channel_service"].enabled is True


def test_the_channel_can_be_switched_off_in_local_mode(make_app) -> None:
    disabled = make_app(SESSION_CHANNEL_ENABLED=False)
    service = disabled.extensions["session_channel_service"]
    assert service.enabled is False
    assert service.disabled_reason == "disabled_by_config"


def test_channel_routes_are_not_reachable_through_a_mounted_app_prefix(app, client) -> None:
    """The channel lives on the control plane, not behind any app mount.

    Note what is *not* asserted here: a 404. Dash registers a catch-all route under its
    base prefix so client-side routing works, which means an unknown sub-path of a
    mounted app returns the app's index HTML with 200. A status-code assertion would
    therefore prove nothing either way. The property that matters is that the request
    never reaches the channel — no command payload comes back, and no session state
    changes.
    """

    service = app.extensions["session_channel_service"]
    before = len(service.registry.list_sessions())

    response = client.get(f"/apps/demo{BLUEPRINT_URL_PREFIX}{ROUTE_POLL}?session_id=x")
    body = response.get_data(as_text=True)
    # An HTML document, not a channel response. (Sniffing for protocol strings in the
    # body would be misleading: the page has the channel's own JavaScript inlined into
    # it, so those words legitimately appear in the app index.)
    assert "application/json" not in (response.headers.get("Content-Type") or "")
    assert body.lstrip().startswith("<!DOCTYPE"), "expected the Dash index, not a channel payload"

    registered = client.post(
        f"/apps/demo{BLUEPRINT_URL_PREFIX}{ROUTE_REGISTER}",
        json={"session_id": "smuggled", "mount_path": "/apps/demo", "capabilities": {}},
    )
    assert registered.status_code in {200, 404, 405}, registered.status_code
    assert service.registry.get("smuggled") is None, "the app prefix must not reach the registry"
    assert len(service.registry.list_sessions()) == before


# ---------------------------------------------------------------------------
# Audit and resources
# ---------------------------------------------------------------------------


def test_every_command_is_audited_with_its_code(client, page) -> None:
    page(_echo_responder(1))
    call_mcp(client, "app_session_eval_js", {"name": "demo", "code": "ctx.page()"})

    logs = _tool_payload(
        call_mcp(client, "app_tail_logs", {"name": "demo", "channel": "session.commands"})
    )["structuredContent"]

    entries = logs["logs"]["entries"]
    assert entries, "the session.commands channel should carry the dispatched command"
    data = entries[-1]["data"]
    assert data["event"] == "session_command"
    assert data["code"] == "ctx.page()"
    assert data["outcome"] == "ok"
    assert data["session_id"] == "tab-1"


def test_guide_resource_documents_the_ctx_surface_and_the_live_settings(client) -> None:
    guide = read_resource_json(client, "dash://meta/session-channel-guide")

    assert guide["channel"]["enabled"] is True
    assert [entry["name"] for entry in guide["ctx"]][:1] == ["ctx.props(ids?)"]
    assert guide["recipes"]
    assert "session_channel_session_gone" in guide["failure_modes"]


def test_app_sessions_resource_lists_registered_tabs(client, page) -> None:
    page(_echo_responder(1))

    payload = read_resource_json(client, "dash://apps/demo/sessions")

    assert payload["app"] == "demo"
    assert [session["session_id"] for session in payload["sessions"]] == ["tab-1"]


def test_both_session_tools_are_advertised(client) -> None:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    names = {tool["name"] for tool in response.get_json()["result"]["tools"]}
    assert {"app_session_eval_js", "app_sessions_list"} <= names
