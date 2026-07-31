"""Characterization tests pinning the full ``validate_workspace`` payload.

These tests snapshot the entire nested return shape of
``WorkspaceService.validate_workspace`` for a known-good workspace and several
known-bad workspaces. The payload shape is consumed downstream (notably by
``runtime/service.py::_classify_validation_category`` and many MCP tests), so
these snapshots pin the top-level keys, their order, the nested report
structures, and ``is_valid``.

They exist as the behavior-preservation proof for the Wave 3 refactor of
``validate_workspace`` into a stage pipeline: the payload must remain
shape-identical before and after.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dash_server.workspace.service import WorkspaceService

CONTRACT_HASH = "b08492e54429a493c95c96d3ac1f259e3d81e51724193cb998c89a607b3f61ac"

EXPECTED_TOP_LEVEL_KEYS = [
    "app",
    "candidate_version",
    "manifest",
    "requirements",
    "lint",
    "syntax",
    "cross_module_symbols",
    "imports",
    "callbacks",
    "credential_safety",
    "exasol",
    "consumption",
    "dependency_install",
    "is_valid",
]


def _normalize(value: Any) -> Any:
    """Replace volatile fields (tracebacks) with stable placeholders."""

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "traceback" and isinstance(item, str):
                out[key] = "<traceback>"
            else:
                out[key] = _normalize(item)
        return out
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _build_workspace(
    tmp_path: Path,
    *,
    manifest: dict[str, Any],
    app_source: str,
    requirements: str = "",
    extra_files: dict[str, str] | None = None,
) -> WorkspaceService:
    workspace = WorkspaceService(str(tmp_path / "workspaces"))
    app_dir = tmp_path / "workspaces" / "test"
    app_dir.mkdir(parents=True)
    (app_dir / "dash-app.json").write_text(json.dumps(manifest))
    (app_dir / "requirements.txt").write_text(requirements)
    (app_dir / "app.py").write_text(app_source)
    (app_dir / ".draft-state.json").write_text(json.dumps({"candidate_version": 1}))
    for relative_path, content in (extra_files or {}).items():
        target = app_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return workspace


METRIC_MANIFEST = {
    "name": "test",
    "title": "Test",
    "route": "/apps/test",
    "description": "Test app.",
    "template": "metric-cards",
}

METRIC_MANIFEST_RESOLVED = {
    "name": "test",
    "title": "Test",
    "route": "/apps/test",
    "description": "Test app.",
    "template": "metric-cards",
    "data_sources": None,
    "consumption": None,
    "consumption_contract_hash": CONTRACT_HASH,
}

GOOD_APP = (
    "from dash import Dash, html, dcc, Input, Output\n\n"
    "def create_dash_app(server, url_base_pathname, metadata):\n"
    "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
    "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
    "    app.layout = html.Div([dcc.Dropdown(id='metric', options=[], value='a'), "
    "html.Div(id='out')])\n"
    "    @app.callback(Output('out', 'children'), Input('metric', 'value'))\n"
    "    def show(value):\n"
    "        return value\n"
    "    return app\n"
)

BAD_SYNTAX_APP = (
    "from dash import Dash, html\n\n"
    "def create_dash_app(server, url_base_pathname, metadata)\n"
    "    return None\n"
)

MISSING_DEP_APP = (
    "import missing_package_demo\n\n"
    "from dash import Dash, html\n\n"
    "def create_dash_app(server, url_base_pathname, metadata):\n"
    "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
    "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
    "    app.layout = html.Div(['ok'])\n"
    "    return app\n"
)

CROSS_MODULE_APP = (
    "import helper\n"
    "from dash import Dash, html\n"
    "def create_dash_app(server, url_base_pathname, metadata):\n"
    "    x = helper.MISSING_SYMBOL\n"
    "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
    "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
    "    app.layout = html.Div(['ok'])\n"
    "    return app\n"
)

EXASOL_MANIFEST = {
    "name": "test",
    "title": "Test",
    "route": "/apps/test",
    "description": "Test app.",
    "template": "exasol-analytics",
    "data_sources": {"primary": {"kind": "exasol", "profile": "sales"}},
}

EXASOL_APP = (
    "import pyexasol  # noqa: F401\n"
    "from dash import Dash, html\n"
    "import os  # noqa: F401\n"
    "def create_dash_app(server, url_base_pathname, metadata):\n"
    "    # never call pyexasol.connect(dsn='x') directly\n"
    "    # password = os.environ['EXA_PASSWORD']\n"
    "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
    "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
    "    app.layout = html.Div(['ok'])\n"
    "    return app\n"
)


def test_top_level_keys_and_order_good(tmp_path: Path):
    workspace = _build_workspace(tmp_path, manifest=METRIC_MANIFEST, app_source=GOOD_APP)
    payload = workspace.validate_workspace("test", mount_path="/apps/test")
    assert list(payload.keys()) == EXPECTED_TOP_LEVEL_KEYS


def test_good_workspace_full_payload(tmp_path: Path):
    workspace = _build_workspace(tmp_path, manifest=METRIC_MANIFEST, app_source=GOOD_APP)
    payload = _normalize(workspace.validate_workspace("test", mount_path="/apps/test"))

    callbacks = {
        "status": "passed",
        "count": 1,
        "callbacks": [
            {
                "output_key": "out.children",
                "outputs": [{"id": "out", "property": "children"}],
                "inputs": [{"id": "metric", "property": "value"}],
                "state": [],
                "missing_layout_ids": [],
            }
        ],
        "missing_layout_ids": [],
        "suppress_callback_exceptions": False,
    }
    assert payload == {
        "app": "test",
        "candidate_version": 1,
        "manifest": METRIC_MANIFEST_RESOLVED,
        "requirements": {"entries": [], "invalid": [], "packages": []},
        "lint": {"status": "passed", "warnings": []},
        "syntax": {"status": "passed", "errors": []},
        "cross_module_symbols": {
            "status": "passed",
            "issues": [],
            "warnings": [],
            "notes": "Validated direct local module imports and aliased local module attribute access.",
        },
        "imports": {
            "status": "passed",
            "error": None,
            "traceback": None,
            "callbacks": callbacks,
        },
        "callbacks": callbacks,
        "credential_safety": {"status": "passed", "findings": []},
        "exasol": {"status": "not_applicable", "issues": []},
        "consumption": {
            "status": "passed",
            "contract_hash": CONTRACT_HASH,
            "output_count": 0,
            "issues": [],
        },
        "dependency_install": {
            "status": "ready",
            "requirements": [],
            "notes": "No dependency installer was configured for workspace validation.",
        },
        "is_valid": True,
    }


def test_syntax_error_workspace_full_payload(tmp_path: Path):
    workspace = _build_workspace(
        tmp_path, manifest=METRIC_MANIFEST, app_source=BAD_SYNTAX_APP
    )
    payload = _normalize(workspace.validate_workspace("test", mount_path="/apps/test"))

    assert list(payload.keys()) == EXPECTED_TOP_LEVEL_KEYS
    assert payload["syntax"] == {
        "status": "failed",
        "errors": [{"path": "app.py", "line": 3, "message": "expected ':'"}],
    }
    assert payload["cross_module_symbols"] == {
        "status": "skipped",
        "issues": [],
        "warnings": [],
        "notes": "Skipped local cross-module symbol checks because Python syntax validation failed.",
    }
    assert payload["imports"] == {
        "status": "skipped",
        "error": None,
        "traceback": None,
    }
    assert payload["callbacks"] == {
        "status": "skipped",
        "count": 0,
        "callbacks": [],
        "missing_layout_ids": [],
        "suppress_callback_exceptions": False,
    }
    assert payload["dependency_install"] == {
        "status": "skipped",
        "requirements": [],
        "notes": "Skipped dependency install because Python syntax validation failed.",
    }
    assert payload["credential_safety"] == {"status": "passed", "findings": []}
    assert payload["exasol"] == {"status": "not_applicable", "issues": []}
    assert payload["is_valid"] is False


def test_missing_dependency_workspace_full_payload(tmp_path: Path):
    workspace = _build_workspace(
        tmp_path,
        manifest=METRIC_MANIFEST,
        app_source=MISSING_DEP_APP,
        requirements="missing-package-demo\n",
    )
    payload = _normalize(workspace.validate_workspace("test", mount_path="/apps/test"))

    assert payload["requirements"] == {
        "entries": ["missing-package-demo"],
        "invalid": [],
        "packages": ["missing-package-demo"],
    }
    assert payload["syntax"] == {"status": "passed", "errors": []}
    assert payload["cross_module_symbols"]["status"] == "passed"
    assert payload["imports"] == {
        "status": "failed",
        "category": "environment_missing_dependency",
        "error": "ModuleNotFoundError: No module named 'missing_package_demo'",
        "traceback": "<traceback>",
        "missing_dependency": "missing_package_demo",
        "declared_in_requirements": True,
    }
    assert payload["callbacks"] == {
        "status": "skipped",
        "count": 0,
        "callbacks": [],
        "missing_layout_ids": [],
        "suppress_callback_exceptions": False,
    }
    assert payload["dependency_install"] == {
        "status": "ready",
        "requirements": ["missing-package-demo"],
        "notes": "No dependency installer was configured for workspace validation.",
    }
    assert payload["is_valid"] is False


def test_cross_module_symbol_failure_full_payload(tmp_path: Path):
    workspace = _build_workspace(
        tmp_path,
        manifest=METRIC_MANIFEST,
        app_source=CROSS_MODULE_APP,
        extra_files={"helper.py": "VALUE = 1\n"},
    )
    payload = _normalize(workspace.validate_workspace("test", mount_path="/apps/test"))

    assert payload["cross_module_symbols"] == {
        "status": "failed",
        "issues": [
            {
                "path": "app.py",
                "line": 4,
                "message": "helper.MISSING_SYMBOL is referenced but not defined in helper.py.",
                "symbol": "MISSING_SYMBOL",
                "reference": "helper.MISSING_SYMBOL",
                "target_path": "helper.py",
            }
        ],
        "warnings": [],
        "notes": "Validated direct local module imports and aliased local module attribute access.",
    }
    assert payload["imports"] == {
        "status": "skipped",
        "category": "cross_module_symbols_failed",
        "error": "Skipped import smoke check because local cross-module symbol validation failed.",
        "traceback": None,
    }
    assert payload["dependency_install"] == {
        "status": "skipped",
        "requirements": [],
        "notes": "Skipped dependency install because local cross-module symbol validation failed.",
    }
    assert payload["is_valid"] is False


def test_exasol_credential_and_lint_full_payload(tmp_path: Path):
    workspace = _build_workspace(
        tmp_path,
        manifest=EXASOL_MANIFEST,
        app_source=EXASOL_APP,
        extra_files={"queries/q.sql": "SELECT id AS DAY FROM my_table\n"},
    )
    payload = _normalize(workspace.validate_workspace("test", mount_path="/apps/test"))

    assert payload["credential_safety"] == {
        "status": "failed",
        "findings": [
            {
                "path": "app.py",
                "message": (
                    "Hosted apps must not call pyexasol.connect(...) directly. "
                    "Use a server-side Exasol profile and runtime helper instead."
                ),
            },
            {
                "path": "app.py",
                "message": (
                    "Hosted apps must not read EXA_/EXASOL_ credential environment variables directly. "
                    "Bind an Exasol profile and let the server resolve credentials."
                ),
            },
            {
                "path": "app.py",
                "message": (
                    "Hosted app source appears to define database credential parameters directly. "
                    "Move Exasol credentials into the server-side profile configuration."
                ),
            },
        ],
    }
    assert payload["exasol"] == {
        "status": "passed_with_warnings",
        "issues": [
            {
                "level": "warning",
                "path": "app.py",
                "message": "Exasol-backed hosted apps should rely on the server helper path instead of importing pyexasol directly.",
            },
            {
                "level": "warning",
                "path": "queries/q.sql",
                "line": 1,
                "message": 'AS DAY uses an Exasol reserved word as a bare alias. Quote it: AS "DAY".',
            },
            {
                "level": "warning",
                "path": "queries/q.sql",
                "message": "This query does not declare LIMIT or obvious aggregation. Ensure Exasol is doing bounded or aggregated work before returning rows.",
            },
            {
                "level": "info",
                "path": "queries/q.sql",
                "message": (
                    "queries/q.sql is not referenced by any .py file in the workspace. "
                    "Delete unused SQL files with app_delete_file to keep the workspace tidy."
                ),
            },
        ],
    }
    assert payload["imports"]["status"] == "passed"
    assert payload["is_valid"] is False


def test_ps26_bug017_cast_to_reserved_word_type_is_not_flagged_as_a_bare_alias(tmp_path: Path):
    """`CAST(x AS DATE)` is cast syntax, not a column alias - the lint must not fire,
    since its own suggested fix (`AS "DATE"`) would change what the cast does. A genuine
    bare alias using the same reserved word, in the same file, must still be flagged.
    """

    workspace = _build_workspace(
        tmp_path,
        manifest=EXASOL_MANIFEST,
        app_source=EXASOL_APP,
        extra_files={
            "queries/q.sql": (
                "SELECT CAST(created_at AS DATE) AS created_date, id AS DAY\n"
                "FROM my_table\n"
            )
        },
    )
    payload = workspace.validate_workspace("test", mount_path="/apps/test")

    reserved_alias_messages = [
        issue["message"]
        for issue in payload["exasol"]["issues"]
        if issue["path"] == "queries/q.sql" and "reserved word as a bare alias" in issue["message"]
    ]
    assert reserved_alias_messages == ['AS DAY uses an Exasol reserved word as a bare alias. Quote it: AS "DAY".']


def test_ps26_bug017_cast_detection_ignores_a_same_named_identifier_prefix(tmp_path: Path):
    """A column literally named `mycast` immediately before `(... AS DATE)` must not be
    mistaken for the `CAST` keyword - the word-boundary check must require `CAST` to be
    a whole token, not a suffix of a longer identifier.
    """

    workspace = _build_workspace(
        tmp_path,
        manifest=EXASOL_MANIFEST,
        app_source=EXASOL_APP,
        extra_files={"queries/q.sql": "SELECT mycast(created_at AS DATE) FROM my_table\n"},
    )
    payload = workspace.validate_workspace("test", mount_path="/apps/test")

    reserved_alias_messages = [
        issue["message"]
        for issue in payload["exasol"]["issues"]
        if issue["path"] == "queries/q.sql" and "reserved word as a bare alias" in issue["message"]
    ]
    assert reserved_alias_messages == ['AS DATE uses an Exasol reserved word as a bare alias. Quote it: AS "DATE".']


PATTERN_MATCHING_APP = (
    "from dash import Dash, Input, Output, State, ALL, html\n\n"
    "def create_dash_app(server, url_base_pathname, metadata):\n"
    "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
    "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/'{suppress_kwarg})\n"
    "    app.layout = html.Div([html.Div(id='container'), html.Div(id='ack-store')])\n"
    "    @app.callback(\n"
    "        Output('ack-store', 'children'),\n"
    "        Input({{'type': 'ack-btn', 'index': ALL}}, 'n_clicks'),\n"
    "        State('ack-store', 'children'),\n"
    "        prevent_initial_call=True,\n"
    "    )\n"
    "    def ack(_clicks, current):\n"
    "        return current\n"
    "    return app\n"
)


def test_ps27_bug005_pattern_matching_callback_gets_an_actionable_hint(tmp_path: Path):
    """PS27-BUG-005 regression: a standard Dash pattern-matching (ALL/MATCH/ALLSMALLER)
    callback whose concrete components are rendered dynamically by another callback
    fails validation (as it always has, without suppress_callback_exceptions=True) but
    used to give zero hint that this was the fix - reading exactly like a genuine typo.
    """

    workspace = _build_workspace(
        tmp_path,
        manifest=METRIC_MANIFEST,
        app_source=PATTERN_MATCHING_APP.format(suppress_kwarg=""),
    )
    payload = workspace.validate_workspace("test", mount_path="/apps/test")

    assert payload["callbacks"]["status"] == "failed"
    assert payload["is_valid"] is False
    hint = payload["callbacks"]["hint"]
    assert "suppress_callback_exceptions=True" in hint
    assert "ack-btn" in hint


def test_ps27_bug005_pattern_matching_callback_passes_with_suppress_callback_exceptions(tmp_path: Path):
    workspace = _build_workspace(
        tmp_path,
        manifest=METRIC_MANIFEST,
        app_source=PATTERN_MATCHING_APP.format(suppress_kwarg=", suppress_callback_exceptions=True"),
    )
    payload = workspace.validate_workspace("test", mount_path="/apps/test")

    assert payload["callbacks"]["status"] == "passed_with_warnings"
    assert payload["is_valid"] is True
    assert "hint" not in payload["callbacks"]


def test_ps27_bug005_a_genuine_missing_plain_id_gets_no_wildcard_hint(tmp_path: Path):
    """A real typo/bug (a plain, non-pattern id that's simply missing from the layout)
    must not get the pattern-matching hint - only genuine wildcard-shaped ids should.
    """

    app_source = (
        "from dash import Dash, Input, Output, html\n\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    app = Dash(__name__, server=server, routes_pathname_prefix='/', "
        "requests_pathname_prefix=url_base_pathname.rstrip('/') + '/')\n"
        "    app.layout = html.Div([html.Div(id='out')])\n"
        "    @app.callback(Output('out', 'children'), Input('typo-id', 'value'))\n"
        "    def show(value):\n"
        "        return value\n"
        "    return app\n"
    )
    workspace = _build_workspace(tmp_path, manifest=METRIC_MANIFEST, app_source=app_source)
    payload = workspace.validate_workspace("test", mount_path="/apps/test")

    assert payload["callbacks"]["status"] == "failed"
    assert payload["callbacks"]["missing_layout_ids"] == ["typo-id"]
    assert "hint" not in payload["callbacks"]


def test_ps27_bug003_read_all_files_ignores_a_hidden_binary_file_in_the_workspace(tmp_path: Path):
    """PS27-BUG-003 regression: an app's own code can create an arbitrary binary file in
    its workspace as an import-time side effect (e.g. diskcache.Cache() for the
    background-callback pattern) - previously this made read_all_files() (used by
    app_build/app_validate/diffing) crash with an unhandled UnicodeDecodeError trying to
    read it as UTF-8 text. A hidden (dot-prefixed) file/directory - the conventional
    home for this class of generated cache/tool cruft - must now be excluded entirely
    rather than crash the read.
    """

    workspace = _build_workspace(tmp_path, manifest=METRIC_MANIFEST, app_source=GOOD_APP)
    binary_dir = tmp_path / "workspaces" / "test" / ".diskcache"
    binary_dir.mkdir(parents=True)
    (binary_dir / "cache.db").write_bytes(bytes([0x53, 0x51, 0x4C, 0x69, 0x74, 0x65, 0x00, 0x8A]))

    files = workspace.read_all_files("test")

    assert "app.py" in files
    assert not any(".diskcache" in path for path in files)


def test_ps27_bug003_is_artifact_source_part_excludes_any_hidden_path_component():
    from dash_server.artifacts_io import is_artifact_source_part

    assert is_artifact_source_part(Path("app.py")) is True
    assert is_artifact_source_part(Path("queries/detail.sql")) is True
    assert is_artifact_source_part(Path(".diskcache/cache.db")) is False
    assert is_artifact_source_part(Path("nested/.cache/data.bin")) is False
    assert is_artifact_source_part(Path(".git/HEAD")) is False
    assert is_artifact_source_part(Path("__pycache__/app.cpython-310.pyc")) is False
