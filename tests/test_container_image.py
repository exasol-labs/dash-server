"""Smoke test of the container image (Dockerfile, docs/container-image.md).

Runs only when DASH_SERVER_TEST_IMAGE names a built image. It starts the image in hosted mode behind
a trusted proxy on loopback, then drives the server from inside the container, where requests come
from 127.0.0.1 as they would from a proxy in the same pod.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid

import pytest

IMAGE = os.environ.get("DASH_SERVER_TEST_IMAGE")
CLI = os.environ.get("DASH_SERVER_CONTAINER_CLI", "docker")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.integration,
    pytest.mark.skipif(not IMAGE, reason="DASH_SERVER_TEST_IMAGE names no image"),
]

HOSTED_ENV = {
    "DASH_SERVER_MODE": "hosted",
    "DASH_SERVER_HOST": "127.0.0.1",
    "DASH_SERVER_SECRET_KEY": "image-test-secret",
    "DASH_SERVER_PUBLIC_BASE_URL": "https://dash.example.test",
    "DASH_SERVER_SESSION_COOKIE_SECURE": "true",
    "DASH_SERVER_AUTH_PROVIDER": "trusted_proxy",
    "DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED": "true",
    "DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS": "127.0.0.1/32",
    "DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS": "trusted_proxy:owner",
    "DASH_SERVER_EXASOL_PROFILE_CREATION_ENABLED": "false",
    "DASH_SERVER_DEMO_APP_ENABLED": "false",
    "DASH_SERVER_LIVE_PATH_PREFIX": "/dashboards/",
}

# Runs inside the container: one JSON line per check.
DRIVER = r"""
import json, time, urllib.request

BASE = "http://127.0.0.1:5100"
OWNER = {"X-Forwarded-User": "owner", "Content-Type": "application/json"}

def mcp(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(BASE + "/mcp", data=body, headers=OWNER)
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)["result"]

for _ in range(120):
    try:
        urllib.request.urlopen(urllib.request.Request(BASE + "/", headers=OWNER), timeout=2)
        break
    except Exception:
        time.sleep(0.5)

tools = [tool["name"] for tool in mcp("tools/list", {})["tools"]]
apps = mcp("tools/call", {"name": "apps_list", "arguments": {}})["structuredContent"]["apps"]
app_py = '''
import pandas as pd
import plotly.express as px
from dash import Dash, dcc, html

def create_dash_app(server, url_base_pathname, metadata):
    app = Dash(__name__, server=server, routes_pathname_prefix="/",
               requests_pathname_prefix=url_base_pathname.rstrip("/") + "/")
    frame = pd.DataFrame({"day": [1, 2, 3], "orders": [3, 5, 4]})
    app.layout = html.Div([html.H1("Orders"), dcc.Graph(figure=px.line(frame, x="day", y="orders"))])
    return app
'''
started = time.monotonic()
created = mcp("tools/call", {"name": "app_create_from_files", "arguments": {
    "name": "orders", "title": "Orders",
    "files": [{"path": "app.py", "content": app_py}, {"path": "requirements.txt", "content": "dash\nplotly\npandas\n"}],
}})
seconds = time.monotonic() - started
page = urllib.request.urlopen(urllib.request.Request(BASE + "/dashboards/orders/", headers=OWNER), timeout=60)
print(json.dumps({"tools": tools, "apps": apps, "created": created, "seconds": seconds, "page_status": page.status}))
"""


@pytest.fixture(scope="module")
def container():
    if shutil.which(CLI) is None:
        pytest.skip(f"{CLI} is not installed")
    name = f"dash-server-image-test-{uuid.uuid4().hex[:8]}"
    env_args = [arg for key, value in HOSTED_ENV.items() for arg in ("-e", f"{key}={value}")]
    subprocess.run([CLI, "run", "-d", "--name", name, *env_args, IMAGE], check=True, capture_output=True)
    try:
        yield name
    finally:
        subprocess.run([CLI, "rm", "-f", name], check=False, capture_output=True)


def _exec(container: str, *command: str) -> str:
    result = subprocess.run([CLI, "exec", container, *command], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def driven(container):
    time.sleep(1)
    return json.loads(_exec(container, "python", "-c", DRIVER).strip().splitlines()[-1])


def test_runs_as_non_root_user(container):
    assert _exec(container, "id", "-u").strip() == "10001"


def test_hosted_settings_apply(driven):
    assert "exasol_profile_create_local" not in driven["tools"]
    assert driven["apps"] == []


def test_dashboard_builds_from_cached_wheels(container, driven):
    assert driven["created"]["isError"] is False, driven["created"]
    assert driven["page_status"] == 200
    logs = _exec(container, "sh", "-c", "cat /var/lib/dash-server/app_envs/*/install.log")
    assert "Downloading" not in logs, logs
    # Without downloads, the environment build stays well inside an agent's tool call.
    assert driven["seconds"] < 60, driven["seconds"]
