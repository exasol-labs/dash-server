from __future__ import annotations

import json

import pytest

from dash_server.app_factory import create_app

from _helpers import base_test_config
from _mcp_helpers import (
    _call_mcp,
    _dash_layout,
    _dash_callback,
    _layout_texts,
    _bundle,
    _callback_failure_app_py,
    _app_callback_app_py,
    _artifact_sensitive_app_py,
)

@pytest.mark.slow
class TestMcpDiagnosticsRepairFlow:
    """Decomposition of the former single ``test_mcp_can_collect_diagnostics_
    repair_import_failure_and_redeploy`` walkthrough.

    The break-imports -> diagnose -> repair -> redeploy end-to-end path runs
    once in the class-scoped ``flow`` fixture; each focused test asserts one
    diagnostic surface against the captured payloads.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def flow(tmp_path_factory):
        tmp_path = tmp_path_factory.mktemp("mcp_diagnostics_repair")
        app = create_app(base_test_config(tmp_path))
        client = app.test_client()
        c: dict = {}

        create_response = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_create",
                "arguments": {
                    "bundle": _bundle(
                        "support",
                        "Support Dashboard v1",
                        summary="Initial support revision.",
                        revenue="$640K",
                    )
                },
            },
            request_id=18,
        )
        c["create_status"] = create_response.status_code

        break_imports = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_patch_file",
                "arguments": {
                    "name": "support",
                    "path": "app.py",
                    "search": "from dash import Dash, Input, Output, dcc, html",
                    "replace": "from totally_missing_package import Dash, Input, Output, dcc, html",
                },
            },
            request_id=19,
        )
        c["break_imports_status"] = break_imports.status_code

        validate_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_validate", "arguments": {"name": "support"}},
            request_id=20,
        )
        c["validation"] = validate_response.get_json()["result"]["structuredContent"]["validation"]

        build_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_build", "arguments": {"name": "support"}},
            request_id=21,
        )
        c["build_status"] = build_response.status_code

        diagnostics_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_collect_diagnostics", "arguments": {"name": "support"}},
            request_id=22,
        )
        c["diagnostics"] = diagnostics_response.get_json()["result"]["structuredContent"]

        logs_response = _call_mcp(
            client,
            "resources/read",
            {"uri": "dash://apps/support/logs/latest"},
            request_id=23,
        )
        c["logs"] = json.loads(logs_response.get_json()["result"]["contents"][0]["text"])

        errors_response = _call_mcp(
            client,
            "resources/read",
            {"uri": "dash://apps/support/errors"},
            request_id=24,
        )
        c["errors"] = json.loads(errors_response.get_json()["result"]["contents"][0]["text"])

        health_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_run_healthcheck", "arguments": {"name": "support"}},
            request_id=25,
        )
        c["health"] = health_response.get_json()["result"]["structuredContent"]

        build_logs_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_tail_logs", "arguments": {"name": "support", "channel": "build", "limit": 5}},
            request_id=26,
        )
        c["build_logs"] = build_logs_response.get_json()["result"]

        fix_imports = _call_mcp(
            client,
            "tools/call",
            {
                "name": "app_patch_file",
                "arguments": {
                    "name": "support",
                    "path": "app.py",
                    "search": "from totally_missing_package import Dash, Input, Output, dcc, html",
                    "replace": "from dash import Dash, Input, Output, dcc, html",
                },
            },
            request_id=26,
        )
        c["fix_imports_status"] = fix_imports.status_code

        validate_fixed = _call_mcp(
            client,
            "tools/call",
            {"name": "app_validate", "arguments": {"name": "support"}},
            request_id=27,
        )
        c["validate_fixed"] = validate_fixed.get_json()["result"]["structuredContent"]["validation"]

        build_fixed = _call_mcp(
            client,
            "tools/call",
            {"name": "app_build", "arguments": {"name": "support"}},
            request_id=28,
        )
        c["built"] = build_fixed.get_json()["result"]["structuredContent"]

        preview_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_start_preview", "arguments": {"name": "support", "revision_number": 2}},
            request_id=29,
        )
        c["preview_status"] = preview_response.status_code
        c["preview_texts"] = _layout_texts(_dash_layout(client, "/preview/support/2"))

        promote_response = _call_mcp(
            client,
            "tools/call",
            {"name": "app_promote_revision", "arguments": {"name": "support", "revision_number": 2}},
            request_id=30,
        )
        c["promote_status"] = promote_response.status_code
        c["live_texts"] = _layout_texts(_dash_layout(client, "/apps/support"))

        diagnostics_after_fix = _call_mcp(
            client,
            "tools/call",
            {"name": "app_collect_diagnostics", "arguments": {"name": "support"}},
            request_id=31,
        )
        c["after_fix"] = diagnostics_after_fix.get_json()["result"]["structuredContent"]

        return c

    def test_broken_import_fails_validation(self, flow):
        assert flow["create_status"] == 200
        assert flow["break_imports_status"] == 200
        assert flow["build_status"] == 200
        validation = flow["validation"]
        assert validation["is_valid"] is False
        assert validation["imports"]["status"] == "failed"
        assert "totally_missing_package" in validation["imports"]["error"]

    def test_diagnostics_report_import_error(self, flow):
        diagnostics = flow["diagnostics"]
        assert diagnostics["latest_build_result"]["status"] == "failed"
        assert diagnostics["latest_error"] is None
        assert diagnostics["latest_build_error"]["category"] == "import_error"
        assert diagnostics["latest_built_revision"]["revision_number"] == 1
        draft_vs_latest_statuses = {
            entry["path"]: entry["status"] for entry in diagnostics["draft_vs_latest_build"]["files"]
        }
        assert draft_vs_latest_statuses["app.py"] == "changed"
        assert diagnostics["artifact_comparison"]["focused_file"]["path"] == "app.py"
        assert diagnostics["artifact_comparison"]["source_context"] == "current_draft"
        assert diagnostics["parsed_traceback"]["category"] == "import_error"
        assert diagnostics["health"]["status"] == "healthy"
        assert "Restore valid imports in app.py or requirements.txt." in diagnostics["suggested_recovery_steps"]

    def test_logs_and_errors_surface_validation_failure(self, flow):
        assert any(
            "Workspace validation failed" in entry["message"]
            for entry in flow["logs"]["logs"]["entries"]
        )
        assert flow["errors"]["errors"][-1]["category"] == "import_error"

    def test_healthcheck_passes_despite_build_failure(self, flow):
        health = flow["health"]["health"]
        assert health["status"] == "healthy"
        # `worker_alive` returns status=not_applicable when running in in_process mode, which
        # is the default for this test. All other probes must pass.
        assert all(probe["status"] in {"passed", "not_applicable"} for probe in health["probes"])

    def test_build_logs_tail_reports_validation_failure(self, flow):
        build_logs = flow["build_logs"]
        assert build_logs["isError"] is False
        assert '"channel": "build"' in build_logs["content"][0]["text"]
        assert "Workspace validation failed" in build_logs["content"][0]["text"]

    def test_fix_revalidates_and_rebuilds(self, flow):
        assert flow["fix_imports_status"] == 200
        assert flow["validate_fixed"]["is_valid"] is True
        assert flow["built"]["revision"]["revision_number"] == 2

    def test_repaired_revision_previews_and_promotes(self, flow):
        assert flow["preview_status"] == 200
        assert "Support Dashboard v1" in flow["preview_texts"]
        assert flow["promote_status"] == 200
        assert "Support Dashboard v1" in flow["live_texts"]

    def test_diagnostics_after_fix_report_success(self, flow):
        after_fix = flow["after_fix"]
        assert after_fix["latest_build_result"]["status"] == "succeeded"
        assert after_fix["health"]["status"] == "healthy"


@pytest.mark.slow
def test_mcp_can_inspect_runtime_traceback_text(client):
    traceback_text = """Traceback (most recent call last):
  File "/srv/support/app.py", line 19, in create_dash_app
    raise RuntimeError("boom")
RuntimeError: boom
"""
    inspect_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_inspect_traceback",
            "arguments": {
                "name": "demo",
                "traceback_text": traceback_text,
            },
        },
        request_id=32,
    )
    inspected = inspect_response.get_json()["result"]["structuredContent"]["traceback"]
    assert inspected["category"] == "runtime_crash"
    assert inspected["exception_type"] == "RuntimeError"
    assert inspected["frames"][0]["file"] == "/srv/support/app.py"


@pytest.mark.slow
def test_app_inspect_traceback_ignores_stale_errors_from_older_revisions(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "traceback-app"}}},
        request_id=33,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "traceback-app",
                "files": [
                    {
                        "path": "app.py",
                        "content": _callback_failure_app_py("Traceback App"),
                    }
                ],
            },
        },
        request_id=34,
    )
    failing_deploy = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "traceback-app"}},
        request_id=35,
    )
    assert failing_deploy.get_json()["result"]["isError"] is False

    callback_fail = _dash_callback(
        client,
        "/apps/traceback-app",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "explode"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_fail.status_code == 500

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "traceback-app",
                "files": [
                    {
                        "path": "app.py",
                        "content": _app_callback_app_py("Traceback App Healthy"),
                    }
                ],
            },
        },
        request_id=36,
    )
    healthy_deploy = _call_mcp(
        client,
        "tools/call",
        {"name": "app_deploy_draft", "arguments": {"name": "traceback-app"}},
        request_id=37,
    )
    assert healthy_deploy.get_json()["result"]["isError"] is False
    assert client.get("/apps/traceback-app").status_code == 200

    inspect_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_inspect_traceback", "arguments": {"name": "traceback-app"}},
        request_id=38,
    )
    result = inspect_response.get_json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "diagnostics_not_found"
    assert "current revision or latest failed build" in result["structuredContent"]["error"]["summary"]


@pytest.mark.slow
def test_collect_diagnostics_and_inspect_traceback_attribute_artifact_mismatch(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "artifact-mismatch",
                    "Artifact Mismatch v1",
                    summary="Initial live revision.",
                    revenue="$540K",
                )
            },
        },
        request_id=380,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "artifact-mismatch",
                "files": [
                    {
                        "path": "app.py",
                        "content": _artifact_sensitive_app_py(
                            "Artifact Mismatch v2",
                            failure_mode="preview",
                        ),
                    }
                ],
            },
        },
        request_id=381,
    )
    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "artifact-mismatch"}},
        request_id=382,
    )
    built = build_response.get_json()["result"]["structuredContent"]
    assert built["revision"]["revision_number"] == 2

    preview_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_start_preview", "arguments": {"name": "artifact-mismatch", "revision_number": 2}},
        request_id=383,
    )
    preview_result = preview_response.get_json()["result"]
    assert preview_result["isError"] is True
    traceback_text = preview_result["structuredContent"]["error"]["details"]["traceback_text"]
    assert "preview artifact mount exploded" in traceback_text

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "artifact-mismatch",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Artifact Mismatch Healthy")}],
            },
        },
        request_id=384,
    )

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "artifact-mismatch"}},
        request_id=385,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_runtime_error"]["category"] == "runtime_crash"
    assert diagnostics["latest_built_revision"]["revision_number"] == 2
    draft_vs_latest_statuses = {
        entry["path"]: entry["status"] for entry in diagnostics["draft_vs_latest_build"]["files"]
    }
    assert draft_vs_latest_statuses["app.py"] == "changed"
    assert diagnostics["artifact_comparison"]["focused_file"]["path"] == "app.py"
    assert diagnostics["artifact_comparison"]["source_context"] == "latest_built_artifact"
    assert any(
        "newer content for app.py" in hint
        for hint in diagnostics["artifact_comparison"]["hints"]
    )

    inspect_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_inspect_traceback",
            "arguments": {
                "name": "artifact-mismatch",
                "traceback_text": traceback_text,
            },
        },
        request_id=386,
    )
    inspected = inspect_response.get_json()["result"]["structuredContent"]
    assert inspected["artifact_comparison"]["focused_file"]["path"] == "app.py"
    assert inspected["artifact_comparison"]["source_context"] == "latest_built_artifact"
    assert any(
        "latest built artifact" in hint or "newer content for app.py" in hint
        for hint in inspected["artifact_guidance"]
    )
    assert inspected["suggested_recovery_steps"][0].startswith(
        "Compare the current draft against the latest built artifact"
    )



def test_ps27_bug002_append_jsonl_writes_one_atomic_line(tmp_path, monkeypatch) -> None:
    """PS27-BUG-002 regression: `_append_jsonl` used to issue two separate `write()`
    calls (the payload, then a trailing newline), so two threads appending around the
    same moment could interleave mid-record. Confirm it's now built as one string and
    written in a single call.
    """
    from pathlib import Path

    from dash_server.diagnostics.service import DiagnosticsService

    service = DiagnosticsService(str(tmp_path))
    path = tmp_path / "app" / "channel.jsonl"

    writes: list[str] = []
    real_open = Path.open

    class _RecordingHandle:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            writes.append(data)
            return self._handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

    def recording_open(self, *args, **kwargs):
        return _RecordingHandle(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", recording_open)
    service._append_jsonl(path, {"id": "a"})

    assert len(writes) == 1, "the payload and its trailing newline must be a single write() call"
    assert writes[0] == '{"id": "a"}\n'


def test_ps27_bug002_read_jsonl_recovers_records_from_a_corrupted_no_newline_join(tmp_path) -> None:
    """PS27-BUG-002 regression: an unlocked concurrent-append interleave used to leave
    two complete JSON objects concatenated with no separating newline on one physical
    line, and `_read_jsonl` raised `JSONDecodeError` on it - breaking every subsequent
    read of that log (`app_build`, `app_tail_logs`, `app_collect_diagnostics` all funnel
    through this reader). It must now recover both records instead.
    """
    from dash_server.diagnostics.service import DiagnosticsService

    service = DiagnosticsService(str(tmp_path))
    path = tmp_path / "app" / "channel.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"id": "good-1"}\n{"id": "d03"}{"id": "027f"}\n{"id": "good-2"}\n')

    records = service._read_jsonl(path)

    assert [r["id"] for r in records] == ["good-1", "d03", "027f", "good-2"]


def test_ps27_bug002_read_jsonl_skips_a_genuinely_unparseable_remainder(tmp_path) -> None:
    """A truly garbled line (not just a lost newline between two valid objects) must be
    skipped rather than crash the whole read - any parseable records before the
    garbled point on a line are still salvaged.
    """
    from dash_server.diagnostics.service import DiagnosticsService

    service = DiagnosticsService(str(tmp_path))
    path = tmp_path / "app" / "channel.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"id": "good-1"}\n{"id": "good-2"}{not valid json at all\n{"id": "good-3"}\n')

    records = service._read_jsonl(path)

    assert [r["id"] for r in records] == ["good-1", "good-2", "good-3"]
