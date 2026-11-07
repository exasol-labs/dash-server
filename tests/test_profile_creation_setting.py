"""`EXASOL_PROFILE_CREATION_ENABLED=false` keeps the operator's profiles the only ones."""

from __future__ import annotations

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
