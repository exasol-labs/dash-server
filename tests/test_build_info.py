from __future__ import annotations

import subprocess

from dash_server import build_info


def test_git_build_info_reports_current_head_when_run_from_a_checkout() -> None:
    build_info.git_build_info.cache_clear()
    info = build_info.git_build_info()
    assert info["source"] == "git"
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=build_info.__file__.rsplit("/src/", 1)[0],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert info["commit_sha"] == expected_sha
    assert info["commit_timestamp"]
    assert isinstance(info["working_tree_dirty"], bool)


def test_git_build_info_falls_back_cleanly_outside_a_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_info, "__file__", str(tmp_path / "not_a_repo" / "build_info.py"))
    build_info.git_build_info.cache_clear()
    try:
        info = build_info.git_build_info()
    finally:
        build_info.git_build_info.cache_clear()
    assert info == {"source": "unknown", "commit_sha": None, "commit_timestamp": None}


def test_server_build_status_includes_process_started_at() -> None:
    status = build_info.server_build_status()
    assert "process_started_at" in status
    assert status["process_started_at"] == build_info._PROCESS_STARTED_AT
