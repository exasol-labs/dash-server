from __future__ import annotations

import pytest

from _mcp_helpers import (
    _call_mcp,
    _dash_layout,
    _resource_json,
    _dash_callback,
    _layout_texts,
    _bundle,
    _callback_failure_app_py,
    _global_callback_app_py,
    _app_callback_app_py,
    _cross_module_from_import_app_py,
    _cross_module_alias_app_py,
    _cross_module_wildcard_app_py,
    _theme_labeled_bar_py,
    _missing_callback_id_app_py,
    _plotly_lint_app_py,
    _misconfigured_prefix_app_py,
)

@pytest.mark.slow
def test_mcp_validation_and_build_fail_for_invalid_python_draft(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "ops",
                    "Ops Dashboard v1",
                    summary="Initial ops revision.",
                    revenue="$3.2M",
                )
            },
        },
        request_id=14,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "ops",
                "files": [{"path": "app.py", "content": "def broken(\n"}],
            },
        },
        request_id=15,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "ops"}},
        request_id=16,
    )
    validation = validate_response.get_json()["result"]["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["syntax"]["status"] == "failed"
    assert validation["imports"]["status"] == "skipped"

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "ops"}},
        request_id=17,
    )
    assert build_response.status_code == 200
    result = build_response.get_json()["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["category"] == "workspace_validation_error"
    assert "Syntax error in app.py" in result["content"][0]["text"]


@pytest.mark.slow
def test_app_validate_reports_callback_inventory_for_healthy_app(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "callback-report"}}},
        request_id=31,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "callback-report",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Callback Report")}],
            },
        },
        request_id=32,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "callback-report"}},
        request_id=33,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is True
    assert validation["callbacks"]["status"] == "passed"
    assert validation["callbacks"]["count"] == 1
    callback = validation["callbacks"]["callbacks"][0]
    assert callback["outputs"][0]["id"] == "clock"
    assert callback["inputs"][0]["id"] == "tick"
    assert "Registered callbacks: 1" in result["content"][0]["text"]


@pytest.mark.slow
def test_app_validate_fails_fast_for_missing_local_symbol_import_and_recovers(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-import"}}},
        request_id=34,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-import",
                "files": [
                    {
                        "path": "app.py",
                        "content": _cross_module_from_import_app_py("Cross Module Import"),
                    },
                    {"path": "theme.py", "content": "from dash import html\n"},
                ],
            },
        },
        request_id=35,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-import"}},
        request_id=36,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    issue = validation["cross_module_symbols"]["issues"][0]
    assert validation["is_valid"] is False
    assert validation["cross_module_symbols"]["status"] == "failed"
    assert issue["path"] == "app.py"
    assert issue["line"] == 2
    assert issue["reference"] == "labeled_bar"
    assert issue["target_path"] == "theme.py"
    assert "theme.labeled_bar" in issue["message"]
    assert validation["dependency_install"]["status"] == "skipped"
    assert validation["imports"]["status"] == "skipped"
    assert validation["imports"]["category"] == "cross_module_symbols_failed"
    assert "Cross-module symbol validation failed" in result["content"][0]["text"]
    assert (
        result["structuredContent"]["guidance"]["next_step"]
        == "Patch the missing local symbol or import path, then validate the draft again."
    )
    assert "force_clean" not in result["content"][0]["text"]
    assert "force_clean" not in result["structuredContent"]["guidance"]["next_step"]
    assert result["structuredContent"]["guidance"]["suggested_tools"] == [
        "app_patch_file",
        "app_put_files",
        "app_validate",
    ]

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-import",
                "files": [{"path": "theme.py", "content": _theme_labeled_bar_py()}],
            },
        },
        request_id=37,
    )

    validate_fixed = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-import"}},
        request_id=38,
    )
    fixed_validation = validate_fixed.get_json()["result"]["structuredContent"]["validation"]
    assert fixed_validation["is_valid"] is True
    assert fixed_validation["cross_module_symbols"]["status"] == "passed"
    assert fixed_validation["imports"]["status"] == "passed"

    deploy_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_deploy_draft",
            "arguments": {"name": "cross-module-import", "deployment_target": "preview"},
        },
        request_id=39,
    )
    deploy_payload = deploy_response.get_json()["result"]["structuredContent"]
    assert deploy_payload["deployment"]["preview_revision"]["revision_number"] == 2
    preview_layout_texts = _layout_texts(_dash_layout(client, "/preview/cross-module-import/2"))
    assert "Labeled Revenue" in preview_layout_texts


@pytest.mark.slow
def test_app_validate_reports_missing_local_symbol_for_aliased_module_attribute(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-alias"}}},
        request_id=40,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-alias",
                "files": [
                    {"path": "app.py", "content": _cross_module_alias_app_py("Cross Module Alias")},
                    {"path": "theme.py", "content": "from dash import html\n"},
                ],
            },
        },
        request_id=41,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-alias"}},
        request_id=42,
    )
    validation = validate_response.get_json()["result"]["structuredContent"]["validation"]
    issue = validation["cross_module_symbols"]["issues"][0]
    assert validation["is_valid"] is False
    assert validation["cross_module_symbols"]["status"] == "failed"
    assert issue["path"] == "app.py"
    assert issue["line"] == 14
    assert issue["reference"] == "T.labeled_bar"
    assert issue["target_path"] == "theme.py"
    assert "theme.labeled_bar" in issue["message"]


@pytest.mark.slow
def test_app_validate_fails_when_callback_references_missing_layout_ids(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "missing-callback-id"}}},
        request_id=34,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "missing-callback-id",
                "files": [{"path": "app.py", "content": _missing_callback_id_app_py("Missing Callback Id")}],
            },
        },
        request_id=35,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "missing-callback-id"}},
        request_id=36,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["imports"]["status"] == "passed"
    assert validation["callbacks"]["status"] == "failed"
    assert validation["callbacks"]["missing_layout_ids"] == ["missing-output"]
    assert "Callback validation failed" in result["content"][0]["text"]


@pytest.mark.slow
def test_app_validate_keeps_wildcard_local_import_checks_non_fatal(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "cross-module-wildcard"}}},
        request_id=37,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "cross-module-wildcard",
                "files": [
                    {
                        "path": "app.py",
                        "content": _cross_module_wildcard_app_py("Cross Module Wildcard"),
                    },
                    {"path": "theme.py", "content": "VALUE = 1\n"},
                ],
            },
        },
        request_id=38,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "cross-module-wildcard"}},
        request_id=39,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    warning = validation["cross_module_symbols"]["warnings"][0]
    assert validation["is_valid"] is True
    assert validation["cross_module_symbols"]["status"] == "passed_with_warnings"
    assert warning["path"] == "app.py"
    assert warning["line"] == 2
    assert "wildcard import from theme" in warning["message"]
    assert "Cross-module symbol warning" in result["content"][0]["text"]


@pytest.mark.slow
def test_app_validate_surfaces_plotly_specific_lint_warnings(client):
    _call_mcp(
        client,
        "tools/call",
        {"name": "app_create", "arguments": {"bundle": {"name": "plotly-lint"}}},
        request_id=37,
    )
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "plotly-lint",
                "files": [
                    {"path": "app.py", "content": _plotly_lint_app_py("Plotly Lint")},
                    {"path": "requirements.txt", "content": "dash\nplotly\n"},
                ],
            },
        },
        request_id=38,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "plotly-lint"}},
        request_id=39,
    )
    result = validate_response.get_json()["result"]
    validation = result["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["imports"]["status"] == "failed"
    assert "fillcolor" in validation["imports"]["error"]
    assert validation["lint"]["status"] == "passed_with_warnings"
    warning_messages = [warning["message"] for warning in validation["lint"]["warnings"]]
    assert any("fillcolor uses 8-digit hex" in message for message in warning_messages)
    assert any("update_layout may set 'margin' twice" in message for message in warning_messages)
    assert "Import smoke check failed" in result["content"][0]["text"]
    assert "Lint warning in app.py" in result["content"][0]["text"]


@pytest.mark.slow
def test_validation_does_not_leak_dash_global_callbacks_across_revisions(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "clock-app"}},
        },
        request_id=56,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "clock-app",
                "files": [{"path": "app.py", "content": _global_callback_app_py("Clock App")}],
            },
        },
        request_id=57,
    )

    validate_global = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "clock-app"}},
        request_id=58,
    )
    assert validate_global.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_global = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "clock-app"}},
        request_id=59,
    )
    assert build_global.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "clock-app",
                "files": [{"path": "app.py", "content": _app_callback_app_py("Clock App")}],
            },
        },
        request_id=60,
    )

    validate_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "clock-app"}},
        request_id=61,
    )
    assert validate_app.get_json()["result"]["structuredContent"]["validation"]["is_valid"] is True

    build_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "clock-app"}},
        request_id=62,
    )
    assert build_app.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 3

    promote_app = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "clock-app", "revision_number": 3}},
        request_id=63,
    )
    assert promote_app.get_json()["result"]["structuredContent"]["current_revision"]["revision_number"] == 3

    live_response = client.get("/apps/clock-app")
    assert live_response.status_code == 200
    live_layout_texts = _layout_texts(_dash_layout(client, "/apps/clock-app"))
    assert "App Callback Revision" in live_layout_texts


@pytest.mark.slow
def test_mcp_rejects_dash_apps_that_do_not_serve_the_mounted_root(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {"bundle": {"name": "broken-prefix"}},
        },
        request_id=64,
    )

    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "broken-prefix",
                "files": [{"path": "app.py", "content": _misconfigured_prefix_app_py("Broken Prefix")}],
            },
        },
        request_id=65,
    )

    validate_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "broken-prefix"}},
        request_id=66,
    )
    validate_result = validate_response.get_json()["result"]
    validate_payload = validate_result["structuredContent"]["validation"]
    assert validate_payload["is_valid"] is False
    assert validate_payload["imports"]["category"] == "route_misconfiguration"
    assert validate_payload["imports"]["details"]["path"] in {"/", "/_dash-layout"}
    assert "Mounted route verification failed" in validate_result["content"][0]["text"]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "broken-prefix"}},
        request_id=67,
    )
    build_result = build_response.get_json()["result"]
    assert build_result["isError"] is True
    assert build_result["structuredContent"]["error"]["category"] == "workspace_validation_error"
    assert "Mounted route verification failed" in build_result["content"][0]["text"]

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "broken-prefix"}},
        request_id=68,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_error"] is None
    assert diagnostics["latest_build_error"]["category"] == "route_misconfiguration"
    assert "Use routes_pathname_prefix='/'" in diagnostics["suggested_recovery_steps"][1]


@pytest.mark.slow
def test_mcp_dependency_report_captures_invalid_requirement_entries(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "finance",
                    "Finance Dashboard v1",
                    summary="Initial finance revision.",
                    revenue="$4.1M",
                )
            },
        },
        request_id=56,
    )

    requirements_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "finance",
                "files": [
                    {
                        "path": "requirements.txt",
                        "content": "dash=>2.18\npandas=2.2\nplotly>=5\n",
                    }
                ],
            },
        },
        request_id=57,
    )
    assert requirements_response.status_code == 200

    validation = _call_mcp(
        client,
        "tools/call",
        {"name": "app_validate", "arguments": {"name": "finance"}},
        request_id=58,
    ).get_json()["result"]["structuredContent"]["validation"]
    assert validation["is_valid"] is False
    assert validation["requirements"]["invalid"] == ["dash=>2.18", "pandas=2.2"]

    dependency_report = _resource_json(
        client,
        "dash://apps/finance/dependency-report",
        request_id=59,
    )
    assert dependency_report["dependency_report"]["declared_requirements"] == ["plotly>=5"]
    assert dependency_report["dependency_report"]["invalid_requirements"] == [
        "dash=>2.18",
        "pandas=2.2",
    ]

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "finance"}},
        request_id=60,
    )
    assert build_response.status_code == 200
    build_result = build_response.get_json()["result"]
    assert build_result["isError"] is True
    assert build_result["structuredContent"]["error"]["category"] == "workspace_validation_error"
    assert "Invalid requirements: dash=>2.18, pandas=2.2" in build_result["content"][0]["text"]

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "finance"}},
        request_id=61,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["latest_error"] is None
    assert diagnostics["latest_build_error"]["category"] == "dependency_conflict"
    assert "Correct invalid or conflicting requirement specifiers in requirements.txt." in diagnostics["suggested_recovery_steps"]


@pytest.mark.slow
def test_mcp_records_callback_failures_from_live_dash_app(client):
    _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_create",
            "arguments": {
                "bundle": _bundle(
                    "alerts",
                    "Alerts Dashboard v1",
                    summary="Initial alerts revision.",
                    revenue="$320K",
                )
            },
        },
        request_id=50,
    )

    put_response = _call_mcp(
        client,
        "tools/call",
        {
            "name": "app_put_files",
            "arguments": {
                "name": "alerts",
                "files": [{"path": "app.py", "content": _callback_failure_app_py("Alerts Workspace App")}],
            },
        },
        request_id=51,
    )
    assert put_response.status_code == 200

    build_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_build", "arguments": {"name": "alerts"}},
        request_id=52,
    )
    assert build_response.get_json()["result"]["structuredContent"]["revision"]["revision_number"] == 2

    promote_response = _call_mcp(
        client,
        "tools/call",
        {"name": "app_promote_revision", "arguments": {"name": "alerts", "revision_number": 2}},
        request_id=53,
    )
    assert promote_response.status_code == 200

    callback_ok = _dash_callback(
        client,
        "/apps/alerts",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "safe"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_ok.status_code == 200
    assert "Mode: safe" in callback_ok.get_data(as_text=True)

    callback_fail = _dash_callback(
        client,
        "/apps/alerts",
        output="callback-result.children",
        outputs={"id": "callback-result", "property": "children"},
        inputs=[{"id": "mode", "property": "value", "value": "explode"}],
        changed_prop_ids=["mode.value"],
    )
    assert callback_fail.status_code == 500

    callback_failures = _resource_json(
        client,
        "dash://apps/alerts/callback-failures",
        request_id=54,
    )
    latest_failure = callback_failures["callback_failures"][-1]
    assert latest_failure["category"] == "dash_callback_error"
    assert latest_failure["details"]["path"].endswith("/_dash-update-component")
    assert latest_failure["details"]["output"] == "callback-result.children"
    assert latest_failure["details"]["changed_prop_ids"] == ["mode.value"]
    assert latest_failure["details"]["inputs"][0]["value"] == "explode"
    assert latest_failure["parsed_traceback"]["exception_type"] == "RuntimeError"

    diagnostics = _call_mcp(
        client,
        "tools/call",
        {"name": "app_collect_diagnostics", "arguments": {"name": "alerts"}},
        request_id=55,
    ).get_json()["result"]["structuredContent"]
    assert diagnostics["callback_failure_summary"]["callback_failures"][-1]["category"] == "dash_callback_error"
    assert "Inspect the callback function and the referenced component ids." in diagnostics["suggested_recovery_steps"]

