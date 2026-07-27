"""Characterization pins for the runtime-service decomposition (Wave 3, P7).

These snapshot the *shape* (stable keys, probe names/order, overall status,
reconcile result) of the seeded ``demo`` app's runtime outputs so the
``RuntimeMounter`` / ``GitReconciler`` / ``HealthProber`` extraction can be
proven behavior-preserving. They intentionally avoid asserting on volatile
values (timestamps, hashes, artifact paths).
"""

from __future__ import annotations

import pytest
from flask import Flask

pytestmark = pytest.mark.slow


def _runtime(app: Flask):
    return app.extensions["runtime_service"]


def test_run_healthcheck_shape(make_app) -> None:
    runtime = _runtime(make_app())
    payload = runtime.run_healthcheck("demo", record=False)

    assert sorted(payload.keys()) == ["app", "health", "revision", "target"]
    assert payload["target"] == "live"
    assert sorted(payload["health"].keys()) == ["probes", "status"]
    assert payload["health"]["status"] == "healthy"

    probes = payload["health"]["probes"]
    assert [probe["name"] for probe in probes] == [
        "publication",
        "process_alive",
        "http_ready",
        "dash_layout",
        "dash_dependencies",
        "static_assets",
        "data_layer",
        "sql_smoke",
        "worker_alive",
        "worker_http",
    ]
    assert [(p["name"], p["status"]) for p in probes] == [
        ("publication", "passed"),
        ("process_alive", "passed"),
        ("http_ready", "passed"),
        ("dash_layout", "passed"),
        ("dash_dependencies", "passed"),
        ("static_assets", "passed"),
        ("data_layer", "passed"),
        ("sql_smoke", "not_applicable"),
        ("worker_alive", "not_applicable"),
        ("worker_http", "not_applicable"),
    ]
    # Every probe carries the (name, status, details) contract.
    for probe in probes:
        assert {"name", "status", "details"} <= set(probe.keys())

    assert sorted(payload["app"].keys()) == [
        "auth_policy",
        "current_revision_id",
        "current_revision_number",
        "draft_candidate_version",
        "enabled",
        "exposure",
        "mounted",
        "name",
        "permissions",
        "preview_mounted",
        "preview_path",
        "preview_revision_id",
        "preview_revision_number",
        "published",
        "rollback_revision_id",
        "rollback_revision_number",
        "route",
        "status",
        "title",
        "visibility",
    ]
    assert sorted(payload["revision"].keys()) == [
        "artifact_path",
        "commit_sha",
        "created_at",
        "dependency_lock_hash",
        "git_branch",
        "git_tag",
        "id",
        "lifecycle_state",
        "release_manifest_path",
        "revision_number",
        "source_hash",
    ]


def test_run_healthcheck_preview_target_unavailable(make_app) -> None:
    from dash_server.exceptions import DashServerError

    runtime = _runtime(make_app())
    with pytest.raises(DashServerError) as excinfo:
        runtime.run_healthcheck("demo", target="preview", record=False)
    assert excinfo.value.category == "preview_unavailable"


def test_reconcile_app_shape(make_app) -> None:
    runtime = _runtime(make_app())
    result = runtime.reconcile_app("demo")
    assert result == {
        "app": "demo",
        "status": "reconciled",
        "live_revision": 1,
        "preview_revision": None,
        "route": "/apps/demo",
    }


def test_reconcile_app_unknown_is_skipped(make_app) -> None:
    runtime = _runtime(make_app())
    result = runtime.reconcile_app("does-not-exist")
    assert result == {
        "app": "does-not-exist",
        "status": "skipped",
        "reason": "app_not_registered",
    }


def test_ps26_bug016_app_with_no_desired_state_is_untracked_not_reconciled(make_app) -> None:
    """PS26-BUG-016 regression: an app with no Git desired-state at all (neither live
    nor preview - e.g. a registry row left over from before GitOps tracking existed,
    or whose desired-state files were lost) used to get the identical `"reconciled"`
    label as an app whose desired state was genuinely just applied, distinguishable
    only by separately noticing both revision fields are null. It must report a
    distinct status instead.
    """

    runtime = _runtime(make_app())
    result = runtime.reconciler._reconcile_app_desired_state("demo", None, None, {})
    assert result["status"] == "untracked"
    assert result["live_revision"] is None
    assert result["preview_revision"] is None

    # A real desired-state reconcile (the common case) must still say "reconciled".
    assert runtime.reconcile_app("demo")["status"] == "reconciled"


def test_reconcile_git_desired_state_shape(make_app) -> None:
    runtime = _runtime(make_app())
    result = runtime.reconcile_git_desired_state()
    assert sorted(result.keys()) == ["desired_state", "repo", "results"]
    demo_results = [r for r in result["results"] if r.get("app") == "demo"]
    assert demo_results and demo_results[0]["status"] == "reconciled"


def test_get_app_status_shape(make_app) -> None:
    runtime = _runtime(make_app())
    status = runtime.get_app_status("demo")
    assert sorted(status.keys()) == [
        "app",
        "current_revision",
        "draft",
        "exposure",
        "gitops",
        "preview_revision",
        "rollback_revision",
        "runtime",
    ]
    assert status["app"]["name"] == "demo"
    assert status["app"]["route"] == "/apps/demo"


def test_get_manifest_shape(make_app) -> None:
    runtime = _runtime(make_app())
    manifest = runtime.get_manifest("demo")
    assert sorted(manifest.keys()) == [
        "app",
        "desired_state",
        "exposure",
        "manifest",
        "revision",
    ]
    assert manifest["manifest"]["name"] == "demo"
    assert manifest["app"]["route"] == "/apps/demo"
