"""Best-effort "what code is this process actually running" signal.

PS27-BUG (round-2 persona study, recommendation #1): four independent personas hit a
confusing gap where the live server was serving stale tool schemas/resources because it
had never been restarted after a same-day code change, and none of them had an in-band
way to detect this - each had to independently correlate `git log` timestamps against
`dash://runtime/status`'s process-start time. This module gives `dash://runtime/status` a
direct answer instead: the git commit the running process was started from, and when the
process itself started.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import subprocess
from typing import Any

from dash_server.timestamps import now_iso

_PROCESS_STARTED_AT = now_iso()


@lru_cache(maxsize=1)
def git_build_info() -> dict[str, Any]:
    """The git commit this running process was launched from, computed once per process.

    Returns ``source: "git"`` with ``commit_sha``/``commit_timestamp`` when running from a
    git checkout, or ``source: "unknown"`` with both fields ``None`` (e.g. an installed
    wheel with no ``.git`` directory, or ``git`` unavailable) - never raises.
    """

    repo_root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        commit_timestamp = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return {"source": "unknown", "commit_sha": None, "commit_timestamp": None}
    return {
        "source": "git",
        "commit_sha": sha or None,
        "commit_timestamp": commit_timestamp or None,
        "working_tree_dirty": dirty,
    }


def server_build_status() -> dict[str, Any]:
    """The full `dash://runtime/status` build-drift payload: this process's git commit and
    when the process itself started - so an agent can independently notice "the docs/tool
    schemas I'm reading may be ahead of what's actually running here" without correlating
    timestamps itself.
    """

    return {
        "process_started_at": _PROCESS_STARTED_AT,
        **git_build_info(),
    }
