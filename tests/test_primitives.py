from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dash_server.config import coerce_bool
from dash_server.consumption.models import ConsumptionPolicy
from dash_server.exceptions import DashServerError
from dash_server.paths import safe_join, safe_relative_path
from dash_server.timestamps import now_iso, parse_iso8601, to_iso


def test_safe_relative_path_rejects_traversal_and_absolute():
    assert safe_relative_path("queries/export.sql") == "queries/export.sql"
    assert safe_relative_path("queries\\export.sql") == "queries/export.sql"
    assert safe_relative_path("./x/./y") == "x/y"  # harmless "." parts normalize away
    for bad in ("", "/etc/passwd", "../secret", "a/../../b", "."):
        with pytest.raises(ValueError):
            safe_relative_path(bad)


def test_safe_join_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "link").symlink_to(outside)

    assert safe_join(root, "inner/file.txt") == (root / "inner" / "file.txt").resolve()
    with pytest.raises(ValueError):
        safe_join(root, "link/secret.txt")
    with pytest.raises(ValueError):
        safe_join(root, "../outside/secret.txt")


def test_workspace_write_path_rejects_symlink_escape(tmp_path: Path):
    """Wave 1 gate: the old lexical check missed symlink escapes on the agent write path."""
    from dash_server.workspace.service import WorkspaceService

    outside = tmp_path / "outside"
    outside.mkdir()
    service = WorkspaceService(str(tmp_path / "workspaces"))
    service.put_files("victim", [{"path": "app.py", "content": "print('hi')\n"}])
    workspace_dir = Path(service.workspace_location("victim")["workspace_path"])
    (workspace_dir / "link").symlink_to(outside)

    with pytest.raises(DashServerError) as excinfo:
        service.put_files("victim", [{"path": "link/evil.py", "content": "pwned"}])
    assert excinfo.value.category == "tool_validation_error"
    assert not (outside / "evil.py").exists()


def test_coerce_bool_treats_string_false_as_false():
    assert coerce_bool("false") is False
    assert coerce_bool("False") is False
    assert coerce_bool("0") is False
    assert coerce_bool("no") is False
    assert coerce_bool("true") is True
    assert coerce_bool(None, default=True) is True
    assert coerce_bool(True) is True
    assert coerce_bool(False, default=True) is False


def test_string_false_in_dict_config_disables_flags():
    """Wave 1 gate: '"false"' supplied through dict config disables the flag it configures."""
    policy = ConsumptionPolicy.from_config(
        {
            "DASH_SERVER_CONSUMPTION_ENABLED": "false",
            "DASH_SERVER_CONSUMPTION_EXPORTS_ENABLED": "false",
        }
    )
    assert policy.enabled is False
    assert policy.exports_enabled is False


def test_timestamp_helpers_round_trip():
    stamp = now_iso()
    assert stamp.endswith("Z")
    parsed = parse_iso8601(stamp)
    assert parsed is not None and parsed.tzinfo is not None
    assert to_iso(parsed) == stamp
    assert to_iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)) == "2026-07-20T12:00:00Z"
