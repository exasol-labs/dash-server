"""Drift guards for the hand-maintained MCP tool structures.

A tool is currently declared in several parallel places: the handler dict,
the tools/list definitions, the guidance map, and (for app-scoped tools) the
transport capability map in the blueprint. Until they are derived from one
ToolSpec table (Wave 2), this test freezes their agreement so a tool added to
one structure but not the others fails loudly instead of degrading silently.
"""

from __future__ import annotations

from dash_server.mcp.blueprint import _APP_SCOPED_TOOL_CAPABILITIES, _JOB_SCOPED_TOOLS


def _tool_names(server) -> set[str]:
    return set(server._tool_handlers)


def test_every_handler_has_a_definition_and_vice_versa(app):
    server = app.extensions["mcp_server"]
    handler_names = _tool_names(server)
    definition_names = {definition["name"] for definition in server._tool_definitions()}
    assert handler_names == definition_names, (
        f"handlers without definitions: {sorted(handler_names - definition_names)}; "
        f"definitions without handlers: {sorted(definition_names - handler_names)}"
    )


def test_every_tool_has_specific_guidance(app):
    server = app.extensions["mcp_server"]
    fallback = server._guidance_for_tool("__no_such_tool__", {}, is_error=False)
    missing = [
        name
        for name in sorted(_tool_names(server))
        if server._guidance_for_tool(name, {}, is_error=False) == fallback
    ]
    assert not missing, f"tools with only generic fallback guidance: {missing}"


def test_blueprint_capability_maps_reference_real_tools(app):
    server = app.extensions["mcp_server"]
    handler_names = _tool_names(server)
    unknown_scoped = sorted(set(_APP_SCOPED_TOOL_CAPABILITIES) - handler_names)
    unknown_job = sorted(set(_JOB_SCOPED_TOOLS) - handler_names)
    assert not unknown_scoped, f"blueprint app-scoped entries for unknown tools: {unknown_scoped}"
    assert not unknown_job, f"blueprint job-scoped entries for unknown tools: {unknown_job}"
    for tool_name, capability in _APP_SCOPED_TOOL_CAPABILITIES.items():
        assert isinstance(capability, str) and capability.startswith(
            ("dashboard.", "mcp.", "diagnostics.", "tenant.")
        ), f"{tool_name} declares suspicious capability {capability!r}"
