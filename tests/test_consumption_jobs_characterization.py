"""Characterization pin for the consumption export-job lifecycle.

Snapshots a full export-job lifecycle (queued -> running -> succeeded) driven
through the MCP surface for a built app, plus the resulting job/output payload
shape. Written before the ``jobs.py`` extraction so it proves that pure
refactor is behavior-preserving; it must stay green afterwards.

Mirrors the app-building helpers in ``tests/test_consumption.py`` (that file is
left untouched) and reuses the shared ``call_mcp``/``wait_for`` plumbing.
"""

from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from threading import Event
from typing import Any

import pytest

from _helpers import call_mcp, wait_for

pytestmark = pytest.mark.slow


_APP_PY = """from dash import Dash, html


def create_dash_app(server, url_base_pathname, metadata):
    prefix = url_base_pathname.rstrip(\"/\") + \"/\"
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix=\"/\",
        requests_pathname_prefix=prefix,
        title=metadata.get(\"title\", \"Consumption Test\"),
    )
    app.layout = html.Div([html.H1(metadata.get(\"title\", \"Consumption Test\"))])
    return app
"""


def _consumption_contract() -> dict[str, Any]:
    return {
        "outputs": [
            {
                "id": "monthly-close-detail",
                "title": "Monthly close detail",
                "description": "Posting detail for one accounting period.",
                "kind": "dataset",
                "source": {
                    "type": "exasol_sql",
                    "data_source": "primary",
                    "path": "queries/export.sql",
                },
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "pattern": "^[0-9]{4}-[0-9]{2}$",
                        }
                    },
                    "required": ["period"],
                    "additionalProperties": False,
                },
                "formats": ["csv", "xlsx"],
                "classification": "confidential",
                "limits": {"max_rows": 5000, "max_bytes": 1000000},
                "allow_subscriptions": True,
                "allow_alerts": False,
            }
        ]
    }


class _ConsumptionSmokeFakeConnection:
    """PS26-BUG-005: `create_app` now always preflights revision 1, which runs the
    `sql_smoke` probe against the bound Exasol profile - a bare "parses fine" success
    is all this fixture needs since none of its queries reference a bad column.
    """

    def execute(self, sql_text: str, params: dict[str, Any] | None = None) -> object:
        return None

    def close(self) -> None:
        return None


class _ConsumptionSmokeFakePyExasolModule:
    def connect(self, **kwargs: Any) -> _ConsumptionSmokeFakeConnection:
        return _ConsumptionSmokeFakeConnection()


def _create_output_app(app, client, *, name: str = "finance-outputs") -> None:
    app.extensions["exasol_dashboard_service"].connection_manager.connector_loader = (
        lambda: _ConsumptionSmokeFakePyExasolModule()
    )
    profile_response = call_mcp(
        client,
        "exasol_profile_create_local",
        {
            "name": "analytics-prod",
            "backend": "onprem",
            "credential_mode": "password",
            "dsn": "demodb.exasol.com:8563",
            "user": "sys",
            "secret_value": "super-secret",
        },
    )
    assert profile_response.status_code == 200
    files = [
        {"path": "app.py", "content": _APP_PY},
        {
            "path": "queries/export.sql",
            "content": "SELECT {period!s} AS PERIOD FROM DUAL\n",
        },
        {
            "path": "queries/sql_smoke.json",
            "content": json.dumps({"queries/export.sql": {"period": "2026-07"}}) + "\n",
        },
    ]
    response = call_mcp(
        client,
        "app_create_from_files",
        {
            "name": name,
            "title": "Finance Outputs",
            "data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}},
            "consumption": _consumption_contract(),
            "files": files,
        },
    )
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["isError"] is False


class _FakeDatasetExecutor:
    def __init__(self) -> None:
        self.rows = [["2026-07", "=SUM(A1:A2)"], ["2026-07", "safe"]]
        self.preflight_calls = 0

    def preflight(self, revision, output) -> None:
        self.preflight_calls += 1

    def stream(self, revision, output, parameters, *, cancelled):
        from dash_server.consumption.execution import DatasetStream

        return DatasetStream(columns=["PERIOD", "VALUE"], batches=iter([[*self.rows]]))


class _BlockingDatasetExecutor(_FakeDatasetExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def stream(self, revision, output, parameters, *, cancelled):
        from dash_server.consumption.execution import DatasetStream

        def batches():
            self.started.set()
            self.release.wait(timeout=5)
            yield self.rows

        return DatasetStream(columns=["PERIOD", "VALUE"], batches=batches())


def _enable_phase1(app, executor):
    service = app.extensions["consumption_service"]
    service.policy = replace(service.policy, exports_enabled=True)
    service.policy_version = service.policy.version
    service.executor = executor
    return service


def _export_get(client, job_id: str) -> dict[str, Any]:
    response = call_mcp(client, "export_get", {"job_id": job_id})
    assert response.status_code == 200
    return response.get_json()["result"]["structuredContent"]


def test_export_job_lifecycle_queued_running_succeeded(app, client):
    _create_output_app(app, client)
    executor = _BlockingDatasetExecutor()
    service = _enable_phase1(app, executor)

    created = call_mcp(
        client,
        "app_export_create",
        {
            "name": "finance-outputs",
            "output_id": "monthly-close-detail",
            "format": "csv",
            "parameters": {"period": "2026-07"},
            "idempotency_key": "characterization-1",
        },
    ).get_json()["result"]["structuredContent"]

    # The create response returns the freshly enqueued job. The coordinator's
    # thread pool may already have claimed it to "running" by the time we read the
    # response, so accept either non-terminal state — the deterministic
    # queued→running→succeeded transitions are pinned below via the blocking
    # executor. (Asserting the transient "queued" here is a race.)
    job_id = created["job"]["id"]
    assert created["job"]["status"] in {"queued", "running"}
    assert created["artifact"] is None

    # running: the blocking executor holds the worker mid-stream.
    assert executor.started.wait(timeout=5)
    running = wait_for(
        lambda: (
            payload
            if (payload := _export_get(client, job_id))["job"]["status"] == "running"
            else None
        ),
        message="job to reach running",
    )
    assert running["job"]["status"] == "running"

    # succeeded: release the stream and let the pipeline publish the artifact.
    executor.release.set()
    completed = wait_for(
        lambda: (
            payload
            if (payload := _export_get(client, job_id))["job"]["status"]
            in {"succeeded", "failed", "cancelled"}
            else None
        ),
        message="job to reach a terminal state",
    )

    # Terminal job payload shape.
    assert completed["job"]["status"] == "succeeded"
    assert completed["job"]["id"] == job_id
    assert completed["job"]["app_name"] == "finance-outputs"
    assert completed["job"]["output_id"] == "monthly-close-detail"
    assert completed["job"]["revision_number"] == 1
    assert completed["job"]["requested_format"] == "csv"
    assert completed["job"]["output_contract_hash"]
    assert completed["job"]["policy_version"] == service.policy_version
    assert completed["job"]["progress"] == {"phase": "complete", "rows": 2, "bytes": completed["artifact"]["byte_size"]}
    assert completed["job"]["error"] is None
    # Decoded parameters are never surfaced in the payload.
    assert "parameters" not in completed["job"]

    # Resulting artifact payload shape.
    artifact = completed["artifact"]
    assert artifact["job_id"] == job_id
    assert artifact["row_count"] == 2
    assert artifact["byte_size"] > 0
    assert artifact["sha256"]
    assert artifact["content_type"]
    assert artifact["filename"] == "finance-outputs-monthly-close-detail.csv"
    assert artifact["classification"] == "confidential"

    # The preflight ran exactly once and encrypted parameters are stored, not plaintext.
    assert executor.preflight_calls == 1
    with sqlite3.connect(app.config["REGISTRY_DB_PATH"]) as connection:
        encoded = connection.execute(
            "SELECT parameters_json FROM consumption_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert "2026-07" not in encoded
    assert service.parameter_codec.decode(encoded) == {"period": "2026-07"}

    # Audit trail records the full lifecycle.
    with sqlite3.connect(app.config["REGISTRY_DB_PATH"]) as connection:
        audit_events = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM consumption_audit_events WHERE job_id = ?", (job_id,)
            ).fetchall()
        }
    assert {"export.created", "export.succeeded"} <= audit_events
