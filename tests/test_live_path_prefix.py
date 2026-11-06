"""`DASH_SERVER_LIVE_PATH_PREFIX` moves live dashboards from `/apps/<name>` to `<prefix><name>`."""

from __future__ import annotations

import json
import re

import pytest

from dash_server.exceptions import DashServerError
from dash_server.live_routes import normalize_live_path_prefix

from _helpers import call_mcp

ADMIN = {"X-Forwarded-User": "admin-1"}
# `/apps/` as a path, not inside `dash://apps/...` or `/manage/apps/...`.
_DEFAULT_PREFIX = re.compile(r"(?<![\w/])/apps/")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "/apps/"), ("", "/apps/"), ("/dashboards/", "/dashboards/"), ("dashboards", "/dashboards/")],
)
def test_prefix_is_normalized(value, expected):
    assert normalize_live_path_prefix(value) == expected


@pytest.mark.parametrize("value", ["/preview/", "/mcp", "/a/b/", "/Dashboards/", "/dash boards/"])
def test_unusable_prefix_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_live_path_prefix(value)


def test_server_refuses_to_start_with_an_unusable_prefix(make_app):
    with pytest.raises(DashServerError, match="DASH_SERVER_LIVE_PATH_PREFIX"):
        make_app(LIVE_PATH_PREFIX="/preview/")


def test_default_prefix_serves_under_apps(make_app):
    client = make_app().test_client()
    assert client.get("/apps/demo").status_code == 200


def test_configured_prefix_serves_live_dashboards(make_app):
    client = make_app(LIVE_PATH_PREFIX="/dashboards/").test_client()
    assert client.get("/dashboards/demo").status_code == 200
    assert client.get("/dashboards/demo/_dash-layout").status_code == 200
    assert client.get("/apps/demo").status_code == 404


def test_unknown_dashboard_names_configured_prefix(make_app):
    client = make_app(LIVE_PATH_PREFIX="/dashboards/").test_client()
    response = client.get("/dashboards/missing")
    assert response.status_code == 404
    assert "/dashboards/missing" in response.get_json()["error"]["message"]


def test_tool_texts_name_configured_prefix(make_hosted_app):
    client = make_hosted_app(LIVE_PATH_PREFIX="/dashboards/").test_client()
    response = client.post(
        "/mcp", headers=ADMIN, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    tools = response.get_json()["result"]["tools"]
    text = json.dumps([{k: tool[k] for k in ("description", "inputSchema")} for tool in tools])
    assert not _DEFAULT_PREFIX.search(text)
    deploy = next(tool for tool in tools if tool["name"] == "app_deploy_draft")
    assert "/dashboards/{name}" in deploy["description"]
    # Resource URIs keep their scheme.
    assert "dash://apps/" in text


def test_manifest_route_must_use_configured_prefix(make_hosted_app):
    client = make_hosted_app(LIVE_PATH_PREFIX="/dashboards/").test_client()
    bundle = {
        "manifest": {"name": "sales", "title": "Sales", "route": "/apps/sales", "template": "metric-cards"},
        "dashboard": {"headline": "Sales", "summary": "Sales.", "metrics": [{"label": "A", "value": "1"}]},
    }
    response = call_mcp(client, "app_create", {"bundle": bundle}, headers=ADMIN)
    result = response.get_json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["details"]["expected_prefix"] == "/dashboards/"


def test_created_dashboard_defaults_to_configured_prefix(make_hosted_app):
    client = make_hosted_app(LIVE_PATH_PREFIX="/dashboards/").test_client()
    bundle = {
        "manifest": {"name": "sales", "title": "Sales", "template": "metric-cards"},
        "dashboard": {"headline": "Sales", "summary": "Sales.", "metrics": [{"label": "A", "value": "1"}]},
    }
    response = call_mcp(client, "app_create", {"bundle": bundle}, headers=ADMIN)
    result = response.get_json()["result"]
    assert result["isError"] is False, result
    assert json.dumps(result["structuredContent"]).count("/dashboards/sales") >= 1
