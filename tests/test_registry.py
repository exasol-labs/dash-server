from __future__ import annotations

from pathlib import Path


def test_registry_returns_seeded_demo_app(app):
    registry = app.extensions["registry"]
    runtime_service = app.extensions["runtime_service"]

    demo = registry.get_app("demo")
    revision = registry.get_current_revision("demo")
    events = registry.list_events("demo")
    status = runtime_service.get_app_status("demo")

    assert demo is not None
    assert revision is not None
    assert demo.name == "demo"
    assert demo.route == "/apps/demo"
    assert demo.status == "running"
    assert demo.current_revision_number == 1
    assert demo.preview_revision_number is None
    assert demo.rollback_revision_number is None
    assert revision.manifest["template"] == "metric-cards"
    assert revision.lifecycle_state == "live"
    assert Path(revision.artifact_path).is_dir()
    assert revision.artifact_path.endswith("demo/r000001")
    assert revision.git_tag == "dash-server/demo/r000001"
    assert revision.git_branch == "draft/demo"
    assert revision.release_manifest_path == "releases/demo/r000001.yaml"
    assert len(revision.commit_sha) == 40
    assert events[0].event_type == "app_seeded"
    assert status["app"]["mounted"] is True
    assert status["draft"]["candidate_version"] == 1
    assert status["current_revision"]["git_tag"] == "dash-server/demo/r000001"
