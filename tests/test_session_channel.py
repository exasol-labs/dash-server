"""Unit-level specs for the browser session channel.

Covers the registry, the single-flight queue, the server-side result caps, the
local-mode gate at injection time, and the two drift guards that keep the JavaScript
payload honest about the contract it implements.
"""

from __future__ import annotations

from pathlib import Path
import time

import pytest
from dash import Dash, html
from flask import Flask

from dash_server.dash_apps import branding
from dash_server.exceptions import DashServerError
from dash_server.session_channel.contract import (
    SENTINEL_KEYS,
    SENTINEL_TRUNCATED,
    SENTINEL_TYPE,
    TIER_DASH_COMPONENT_API,
    TIER_DOM,
    TIER_REACT_FIBER,
    app_name_from_mount_path,
    mount_kind_from_mount_path,
)
from dash_server.session_channel.guide import session_channel_guide
from dash_server.session_channel.queue import CommandBusyError, CommandQueue
from dash_server.session_channel.registry import SessionRegistry
from dash_server.session_channel.service import SessionChannelService

_JS_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dash_server"
    / "dash_apps"
    / "assets"
    / "session_channel.js"
)


# ---------------------------------------------------------------------------
# Mount-path parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mount_path", "expected_app", "expected_kind"),
    [
        ("/apps/sales", "sales", "live"),
        ("/apps/sales/", "sales", "live"),
        ("/apps/sales/page-2", "sales", "live"),
        ("/preview/sales/7", "sales", "preview"),
        ("/preview/sales/r000007", "sales", "preview"),
        ("/preview/sales/7/detail", "sales", "preview"),
        ("/manage/apps/sales", None, "unknown"),
        ("/", None, "unknown"),
        ("not-a-path", None, "unknown"),
    ],
)
def test_mount_path_parsing(mount_path: str, expected_app: str | None, expected_kind: str) -> None:
    """The page reports only its mount path; the control plane derives app + kind."""
    assert app_name_from_mount_path(mount_path) == expected_app
    assert mount_kind_from_mount_path(mount_path) == expected_kind


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _registry(**kwargs) -> SessionRegistry:
    defaults = {"max_sessions": 20, "stale_after_seconds": 6.0}
    defaults.update(kwargs)
    return SessionRegistry(**defaults)


def _register(registry: SessionRegistry, session_id: str, mount_path: str = "/apps/demo"):
    return registry.register(
        session_id=session_id,
        mount_path=mount_path,
        revision_number=1,
        pathname=mount_path,
        capabilities={"prop_tier": TIER_REACT_FIBER},
    )


def test_registry_marks_a_session_stale_once_it_stops_polling() -> None:
    registry = _registry(stale_after_seconds=0.5)
    session = _register(registry, "tab-1")
    assert registry.is_live(session)

    time.sleep(0.6)
    assert not registry.is_live(session)
    # A stale tab is still resolvable by explicit id so the caller can explain *why*
    # it is unusable, rather than silently answering about a different tab.
    assert registry.resolve(app_name="demo", session_id="tab-1") is session
    assert registry.resolve(app_name="demo", session_id="auto") is None


def test_registry_auto_resolves_the_most_recently_polled_live_session() -> None:
    registry = _registry()
    _register(registry, "tab-1")
    _register(registry, "tab-2")
    registry.touch("tab-1")

    resolved = registry.resolve(app_name="demo", session_id="auto")
    assert resolved is not None
    assert resolved.session_id == "tab-1"


def test_registry_evicts_least_recently_polled_beyond_the_cap() -> None:
    registry = _registry(max_sessions=3)
    for index in range(3):
        _register(registry, f"tab-{index}")
    registry.touch("tab-1")
    registry.touch("tab-2")

    _register(registry, "tab-3")

    remaining = {session["session_id"] for session in registry.list_sessions()}
    assert "tab-0" not in remaining, "the least-recently-polled session should be evicted"
    assert remaining == {"tab-1", "tab-2", "tab-3"}


def test_registry_ignores_sessions_from_other_apps_when_resolving() -> None:
    registry = _registry()
    _register(registry, "other-tab", mount_path="/apps/other")
    assert registry.resolve(app_name="demo", session_id="auto") is None


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_queue_is_single_flight_per_session() -> None:
    queue = CommandQueue()
    queue.enqueue(session_id="tab-1", code="1", timeout_seconds=5, command_seq=1)

    with pytest.raises(CommandBusyError):
        queue.enqueue(session_id="tab-1", code="2", timeout_seconds=5, command_seq=2)

    # A different session is unaffected.
    queue.enqueue(session_id="tab-2", code="3", timeout_seconds=5, command_seq=1)


def test_queue_delivers_a_command_exactly_once() -> None:
    """A reloaded page must not be handed the same command twice.

    The tab keeps its `sessionStorage` id across a reload, so re-delivery would run the
    agent's code a second time. The command times out instead.
    """

    queue = CommandQueue()
    queue.enqueue(session_id="tab-1", code="1", timeout_seconds=5, command_seq=1)

    assert queue.take("tab-1") is not None
    assert queue.take("tab-1") is None


def test_queue_wait_releases_the_slot_on_timeout() -> None:
    queue = CommandQueue()
    command = queue.enqueue(session_id="tab-1", code="1", timeout_seconds=0.05, command_seq=1)

    assert queue.wait(command) is None
    assert command.abandoned is True
    # The session is not wedged: a new command can be dispatched immediately.
    queue.enqueue(session_id="tab-1", code="2", timeout_seconds=5, command_seq=2)


def test_queue_rejects_a_result_for_the_wrong_command() -> None:
    queue = CommandQueue()
    queue.enqueue(session_id="tab-1", code="1", timeout_seconds=5, command_seq=1)

    assert queue.complete(session_id="tab-1", command_id="not-the-one", result={"ok": True}) is False


def test_queue_drops_a_late_result_for_an_abandoned_command() -> None:
    queue = CommandQueue()
    command = queue.enqueue(session_id="tab-1", code="1", timeout_seconds=0.05, command_seq=1)
    queue.wait(command)

    accepted = queue.complete(
        session_id="tab-1",
        command_id=command.command_id,
        result={"ok": True, "value": 1},
    )
    assert accepted is False, "a result the handler already gave up on must not resolve anything"


# ---------------------------------------------------------------------------
# Service: caps and gate
# ---------------------------------------------------------------------------


def _service(**kwargs) -> SessionChannelService:
    defaults = {"enabled": True, "max_result_bytes": 2048}
    defaults.update(kwargs)
    return SessionChannelService(**defaults)


def test_service_truncates_an_oversized_value_explicitly() -> None:
    """The page bounds its own payloads; the control plane does not trust it to."""

    service = _service(max_result_bytes=1024)
    capped, truncated = service._cap_value(["x" * 200 for _ in range(100)])

    assert truncated is True
    assert capped[SENTINEL_TYPE] == "truncated"
    assert capped[SENTINEL_TRUNCATED] is True
    assert capped["$dsOmittedChars"] > 0


def test_service_keeps_a_value_that_fits() -> None:
    service = _service()
    capped, truncated = service._cap_value({"region": ["EMEA"]})
    assert truncated is False
    assert capped == {"region": ["EMEA"]}


def test_service_caps_console_entries_and_says_how_many_it_dropped() -> None:
    service = _service()
    console = [{"level": "log", "text": f"line {index}"} for index in range(120)]

    capped = service._cap_console(console)

    assert len(capped) == 51  # 50 entries plus the explicit meta line
    assert "further console entries omitted" in capped[-1]["text"]


def test_disabled_service_refuses_with_a_reason() -> None:
    service = _service(enabled=False, disabled_reason="hosted_mode")

    with pytest.raises(DashServerError) as excinfo:
        service.require_enabled(tool_name="app_session_eval_js")

    assert excinfo.value.category == "session_channel_unavailable"
    assert excinfo.value.details["reason"] == "hosted_mode"


def test_dispatch_without_a_live_session_lists_the_candidates() -> None:
    service = _service()

    with pytest.raises(DashServerError) as excinfo:
        service.dispatch(app_name="demo", code="ctx.page()")

    error = excinfo.value
    assert error.category == "session_channel_session_gone"
    assert error.details["reason"] == "no_live_session"
    assert error.details["live_sessions"] == []


def test_dispatch_refuses_a_session_belonging_to_another_app() -> None:
    """Answering about the wrong dashboard is worse than failing."""

    service = _service()
    service.register(
        {"session_id": "tab-1", "mount_path": "/apps/other", "revision_number": 1, "capabilities": {}}
    )

    with pytest.raises(DashServerError) as excinfo:
        service.dispatch(app_name="demo", code="ctx.page()", session_id="tab-1")

    assert excinfo.value.details["reason"] == "app_mismatch"


def test_dispatch_rejects_oversized_code_before_touching_a_session() -> None:
    service = _service(max_code_bytes=64)

    with pytest.raises(DashServerError) as excinfo:
        service.dispatch(app_name="demo", code="x" * 100)

    assert excinfo.value.category == "tool_validation_error"
    assert excinfo.value.details["field"] == "code"


def test_poll_of_an_unknown_session_asks_the_page_to_re_register() -> None:
    service = _service()
    payload = service.poll("never-seen")
    assert payload["register_required"] is True
    assert payload["command"] is None


def test_poll_interval_accelerates_while_a_command_is_outstanding() -> None:
    service = _service(poll_interval_ms=2000, active_poll_interval_ms=250)
    service.register(
        {"session_id": "tab-1", "mount_path": "/apps/demo", "revision_number": 1, "capabilities": {}}
    )
    assert service.poll("tab-1")["poll_interval_ms"] == 2000

    service.queue.enqueue(session_id="tab-1", code="1", timeout_seconds=5, command_seq=1)
    # The command is handed over on this poll, and the pace picks up for the round trip.
    assert service.poll("tab-1")["poll_interval_ms"] == 250


# ---------------------------------------------------------------------------
# Injection gate (enforcement point one of three)
# ---------------------------------------------------------------------------


def _dash_app() -> Dash:
    server = Flask(f"session-channel-test-{id(object())}")
    dash_app = Dash(__name__, server=server, url_base_pathname="/")
    dash_app.layout = html.Div("content")
    return dash_app


def test_hosted_chrome_omits_the_channel_by_default() -> None:
    """A hosted-mode page contains no channel code at all — not disabled code, none."""

    dash_app = _dash_app()
    branding.apply_hosted_footer(dash_app, mount_path="/apps/demo", revision_number=1)

    rendered = str(dash_app.layout)
    assert "__dash-server-session-interval" not in rendered
    assert not getattr(dash_app, "_dash_server_session_channel_registered", False)


def test_hosted_chrome_injects_the_channel_when_asked() -> None:
    dash_app = _dash_app()
    branding.apply_hosted_footer(
        dash_app,
        mount_path="/apps/demo",
        revision_number=1,
        session_channel=True,
    )

    rendered = str(dash_app.layout)
    assert "__dash-server-session-interval" in rendered
    assert "__dash-server-session-meta" in rendered
    assert getattr(dash_app, "_dash_server_session_channel_registered", False) is True


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_js_payload_declares_every_contract_constant() -> None:
    """The JS is a static asset and cannot import `contract.py`.

    It therefore keeps its own copy of the sentinel keys and tier names. This guard is
    what stops the two halves from drifting apart silently.
    """

    source = _JS_PATH.read_text(encoding="utf-8")
    for sentinel in SENTINEL_KEYS:
        assert f'"{sentinel}"' in source, f"session_channel.js is missing sentinel {sentinel}"
    for tier in (TIER_DASH_COMPONENT_API, TIER_REACT_FIBER, TIER_DOM):
        assert f'"{tier}"' in source, f"session_channel.js is missing tier {tier}"


def test_every_documented_ctx_helper_exists_in_the_js_payload() -> None:
    """With one wire verb, the guide *is* the API surface.

    An agent that reads `dash://meta/session-channel-guide` and calls a helper that was
    never implemented gets a ReferenceError after a full round trip, so the guide and
    the payload are pinned to each other.
    """

    source = _JS_PATH.read_text(encoding="utf-8")
    guide = session_channel_guide()
    documented = [entry["name"] for entry in guide["ctx"]]
    assert documented, "the guide must document the ctx surface"

    for name in documented:
        member = name.split("(")[0].replace("ctx.", "")
        assert f"{member}:" in source, f"guide documents ctx.{member} but the JS payload has no such member"


def test_guide_is_readable_even_when_the_channel_is_disabled() -> None:
    """An agent should learn *why* it cannot use the channel where it learns how to."""

    guide = session_channel_guide({"enabled": False, "disabled_reason": "hosted_mode"})
    assert guide["channel"]["disabled_reason"] == "hosted_mode"
    assert "Local mode only" in guide["availability"]
    assert guide["recipes"], "the guide carries the discoverability the tool schema cannot"


def test_ps27_bug013_guide_documents_the_still_recommended_explicit_return_practice() -> None:
    """PS26-BUG-021 (round 1) documented that `eval_mode` detection was newline-based,
    not statement-based - a semicolon-joined one-liner silently returned `undefined`
    with no error while the identical logic split across newlines worked. Round 2
    (PS27-BUG-007/013) actually fixed the underlying detector to be statement-boundary-
    aware rather than line-based, so that specific failure no longer reproduces - but
    an explicit `return` still sidesteps the whole class of "which mode did this fall
    into" ambiguity, so the guide must keep recommending it.
    """

    guide = session_channel_guide()
    notes = guide["eval_semantics"]["notes"]
    mode_note = next((note for note in notes if "last_line" in note and "statements" in note), None)
    assert mode_note is not None, "guide must document the eval_mode contract"
    assert "return" in mode_note


def test_ps27_bug008_guide_documents_the_inactive_tab_read_write_asymmetry() -> None:
    """PS27-BUG-008: ctx.props()/ctx.dom() cannot see components inside an inactive
    dcc.Tabs panel at any tier, but ctx.setProps() can still silently write to them
    and trigger real callbacks - actively misleading for an agent deciding whether a
    component exists/is controllable. Must be documented since it isn't fixable
    without changing what Dash itself keeps mounted.
    """

    guide = session_channel_guide()
    caveat = guide["prop_tiers"]["inactive_tab_caveat"]
    assert "setProps" in caveat
    assert "dcc.Tabs" in caveat


def test_ps27_bug012_guide_has_a_switch_tab_recipe() -> None:
    """PS27-BUG-012: a dcc.Tabs without an explicit id/value has no addressable prop
    for ctx.setProps, and the guide's recipes previously only covered form-style
    setProps interactions - "switch tabs, then do X" is one of the most common first
    steps in testing any multi-tab app.
    """

    guide = session_channel_guide()
    recipes = guide["recipes"]
    switch_tab_recipe = next((r for r in recipes if "switch" in r["goal"].lower()), None)
    assert switch_tab_recipe is not None, "guide must document a tab-switching recipe"
    assert "querySelectorAll" in switch_tab_recipe["code"]
    assert ".click()" in switch_tab_recipe["code"]
