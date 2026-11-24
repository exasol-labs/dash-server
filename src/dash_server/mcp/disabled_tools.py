"""Keep MCP payloads from steering agents to tools the operator turned off.

Help resources, error summaries, and guidance name tools in prose that is shared by every
server. When a named tool is turned off, ``annotate_disabled_tools`` drops it from the
payload's tool lists and adds a ``disabled_tools`` map explaining what to do instead, so an
agent does not call it and hit ``tool_disabled``.
"""

from __future__ import annotations

import json
from typing import Any

# Keys whose list values name tools to call next.
_TOOL_LIST_KEYS = frozenset({"suggested_tools", "related_tools"})

_NOTES = {
    "exasol_profile_create_local": (
        "Turned off on this server: the operator manages the Exasol profiles. Use an existing "
        "profile (exasol_profiles_list) and ask the operator to change its settings, such as "
        "session_init_sql or the DSN."
    ),
}
_DEFAULT_NOTE = "Turned off on this server; calls fail with tool_disabled."


def annotate_disabled_tools(payload: dict[str, Any], disabled: frozenset[str]) -> dict[str, Any]:
    """Return ``payload`` without disabled tools in its tool lists, noting any it still names."""

    if not disabled or not isinstance(payload, dict):
        return payload
    text = json.dumps(payload, default=str)
    mentioned = sorted(tool for tool in disabled if tool in text)
    if not mentioned:
        return payload
    result = _drop_disabled(payload, disabled)
    result["disabled_tools"] = {tool: _NOTES.get(tool, _DEFAULT_NOTE) for tool in mentioned}
    return result


def _drop_disabled(value: Any, disabled: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                [tool for tool in item if tool not in disabled]
                if key in _TOOL_LIST_KEYS and isinstance(item, list)
                else _drop_disabled(item, disabled)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_drop_disabled(item, disabled) for item in value]
    return value
