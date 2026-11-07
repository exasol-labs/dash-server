"""`DASH_SERVER_DEMO_APP_ENABLED=false` starts a new instance without the built-in demo dashboard."""

from __future__ import annotations

from dash_server.app_factory import create_app

from _helpers import base_test_config, call_mcp, hosted_test_config

ADMIN = {"X-Forwarded-User": "admin-1"}


def _app_names(client) -> list[str]:
    result = call_mcp(client, "apps_list", {}, headers=ADMIN).get_json()["result"]
    assert result["isError"] is False, result
    return [app["name"] for app in result["structuredContent"]["apps"]]


def test_demo_app_is_seeded_by_default(make_hosted_app):
    assert _app_names(make_hosted_app().test_client()) == ["demo"]


def test_turned_off_demo_app_leaves_instance_empty(make_hosted_app):
    client = make_hosted_app(DEMO_APP_ENABLED=False).test_client()
    assert _app_names(client) == []
    assert client.get("/apps/demo", headers=ADMIN).status_code == 404


def test_instance_without_demo_stays_bootstrapped_across_restarts(tmp_path):
    config = hosted_test_config(tmp_path, DEMO_APP_ENABLED="false")
    create_app(config)
    # A restart, even with the default setting, does not seed the demo into an existing instance.
    restarted = create_app(hosted_test_config(tmp_path)).test_client()
    assert _app_names(restarted) == []


def test_local_mode_honors_the_setting(tmp_path):
    client = create_app(base_test_config(tmp_path, DEMO_APP_ENABLED=False)).test_client()
    assert client.get("/apps/demo").status_code == 404
