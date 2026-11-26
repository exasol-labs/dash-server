"""`EXASOL_PROFILE_CREATION_ENABLED=false` keeps the operator's profiles the only ones."""

from __future__ import annotations

import json

from _helpers import call_mcp

ADMIN = {"X-Forwarded-User": "admin-1"}


def _tool_names(client, headers=None) -> list[str]:
    response = client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 200
    return [tool["name"] for tool in response.get_json()["result"]["tools"]]


def test_profile_creation_is_offered_by_default(make_hosted_app):
    client = make_hosted_app().test_client()
    assert "exasol_profile_create_local" in _tool_names(client, ADMIN)


def test_turned_off_profile_creation_leaves_tools_list(make_hosted_app):
    client = make_hosted_app(EXASOL_PROFILE_CREATION_ENABLED=False).test_client()
    names = _tool_names(client, ADMIN)
    assert "exasol_profile_create_local" not in names
    assert "exasol_profiles_list" in names


def test_turned_off_profile_creation_rejects_calls(make_hosted_app, tmp_path):
    app = make_hosted_app(EXASOL_PROFILE_CREATION_ENABLED="false")
    client = app.test_client()
    response = call_mcp(
        client,
        "exasol_profile_create_local",
        {
            "name": "intruder",
            "backend": "onprem",
            "credential_mode": "password",
            "dsn": "elsewhere:8563",
            "user": "sys",
            "secret_value": "secret",
        },
        headers=ADMIN,
    )
    result = response.get_json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "tool_disabled"
    assert not (tmp_path / "gitops-repo" / "profiles" / "exasol" / "intruder.json").exists()


def test_guidance_never_suggests_turned_off_tool(make_hosted_app):
    client = make_hosted_app(EXASOL_PROFILE_CREATION_ENABLED=False).test_client()
    response = call_mcp(client, "exasol_profiles_list", {}, headers=ADMIN)
    guidance = response.get_json()["result"]["structuredContent"]["guidance"]
    assert "exasol_profile_create_local" not in guidance["suggested_tools"]


def _read_resource(client, uri):
    response = client.post(
        "/mcp",
        headers=ADMIN,
        json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
    )
    assert response.status_code == 200
    return json.loads(response.get_json()["result"]["contents"][0]["text"])


def _tool_lists(value):
    """Every `related_tools` / `suggested_tools` list nested in a payload."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("related_tools", "suggested_tools") and isinstance(item, list):
                yield item
            else:
                yield from _tool_lists(item)
    elif isinstance(value, list):
        for item in value:
            yield from _tool_lists(item)


def test_help_resources_flag_turned_off_tool(make_hosted_app):
    client = make_hosted_app(EXASOL_PROFILE_CREATION_ENABLED=False).test_client()
    workflow = _read_resource(client, "dash://exasol/help/agent-workflow")
    lists = list(_tool_lists(workflow))
    assert lists and all("exasol_profile_create_local" not in tools for tools in lists)
    assert "exasol_profile_create_local" in workflow["disabled_tools"]
    modes = _read_resource(client, "dash://exasol/help/connection-modes")
    assert "exasol_profile_create_local" in modes["disabled_tools"]


def test_help_resources_unchanged_when_profile_creation_is_on(make_hosted_app):
    client = make_hosted_app().test_client()
    workflow = _read_resource(client, "dash://exasol/help/agent-workflow")
    assert any("exasol_profile_create_local" in tools for tools in _tool_lists(workflow))
    assert "disabled_tools" not in workflow
