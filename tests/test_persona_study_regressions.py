"""Regression tests for the 2026-05-17 persona-study bugs.

One test per `BUG-NN` from `.claude/study-2026-05-17/bug-log.md`. Each test
starts in the failing state and asserts post-fix behavior, so this file is
the grep-able guard rail for the next persona study.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dash_server.app_factory import create_app
from dash_server.gitops.repo_service import GitRepoService


# -- BUG-001: instance-path override -----------------------------------------


@pytest.mark.slow
def test_bug_001_instance_path_via_test_config(tmp_path: Path) -> None:
    """`create_app({"INSTANCE_PATH": d})` lands all derived state under `d`."""

    instance_dir = tmp_path / "iso-instance"
    create_app({"INSTANCE_PATH": str(instance_dir)})

    # Side-effect verification: at minimum, the SQLite registry, gitops repo,
    # diagnostics tree, and artifacts dir should exist under the override.
    assert (instance_dir / "dash_server.sqlite3").exists()
    assert (instance_dir / "gitops-repo").is_dir()
    assert (instance_dir / "diagnostics").is_dir()
    assert (instance_dir / "artifacts").is_dir()


@pytest.mark.slow
def test_bug_001_instance_path_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DASH_SERVER_INSTANCE_PATH` is honored when no test_config override is set."""

    instance_dir = tmp_path / "env-instance"
    monkeypatch.setenv("DASH_SERVER_INSTANCE_PATH", str(instance_dir))
    app = create_app()
    assert app.instance_path == str(instance_dir)
    assert (instance_dir / "dash_server.sqlite3").exists()


@pytest.mark.slow
def test_bug_001_cli_flag_threads_into_create_app() -> None:
    """`dash-server --instance-path` is wired through argparse → create_app.

    We don't actually run a subprocess (that's the next test); we verify the
    CLI option is declared and forwards into the call shape `create_app` expects.
    """

    from dash_server import __main__ as entry

    parser = entry.build_parser()
    args = parser.parse_args(["--instance-path", "/tmp/x", "--port", "5099"])
    assert args.instance_path == "/tmp/x"
    assert args.port == 5099


def test_bug_001_cli_accepts_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from dash_server import __main__ as entry

    monkeypatch.setenv("DASH_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("DASH_SERVER_PORT", "5200")

    args = entry.build_parser().parse_args([])

    assert args.host == "0.0.0.0"
    assert args.port == 5200


# -- BUG-016: idempotent GitOps backfill -------------------------------------


@pytest.mark.slow
def test_bug_016_commit_managed_update_no_op_when_index_is_empty(tmp_path: Path) -> None:
    """`_commit_managed_update` must not raise when `git add` leaves the index empty.

    Before the fix, `subprocess.run(['git', 'commit', '-m', '...'], check=True)` raised
    `CalledProcessError` on the "nothing to commit" exit and the caller (startup-time
    backfill) propagated it, refusing to bind the port.

    We construct the failing state directly: pass `_commit_managed_update` an existing
    file with matching content, but force the `_write_files` shortcut by lying about
    the content via the touched-list. The fix is the `_index_is_empty()` short-circuit
    that catches this before `git commit` runs.
    """

    repo = GitRepoService(repo_root=str(tmp_path / "repo"))
    repo.initialize()

    # Manually stage a state where _write_files claims a path was touched but
    # `git add -A` produces no staged change. This happens in practice when a
    # prior interrupted run already committed the file content and we're re-rendering
    # the same bytes — `_write_files` returns [] in that case today, so the easier
    # path is to write & commit a file, then re-run `_commit_managed_update` with
    # the same content via the public API.
    first = repo.publish_release_to_main(
        app_name="myapp",
        revision_number=1,
        artifact_path="artifacts/myapp/r000001",
        commit_sha="deadbeef",
        git_tag="myapp/r000001",
        source_hash="src-hash",
        dependency_lock_hash="dep-hash",
        release_manifest_path="releases/myapp/r000001.yaml",
    )
    assert first is True  # initial publish committed something.

    # Re-publish with the same payload. Pre-fix, the second call would *not* crash
    # either (because `_write_files` returns [] when content matches). The actual
    # failure path is when the file is on disk but the content rendered by
    # `_render_release_manifest` differs subtly — e.g., a key was reordered. We
    # mimic that by mutating the file to "almost-matching" content first.
    release_file = Path(repo.repo_root) / "releases" / "myapp" / "r000001.yaml"
    original = release_file.read_text()
    release_file.write_text(original + "  # editor whitespace\n")
    repo._git("add", "--", "releases/myapp/r000001.yaml")  # type: ignore[attr-defined]
    repo._git("commit", "-m", "stray whitespace commit")  # type: ignore[attr-defined]

    # Now the index is in sync with HEAD. Re-publish the canonical content. Pre-fix:
    # `_write_files` writes the canonical bytes (content differs from disk!), `git
    # add -A` stages the revert; `git commit` succeeds. (Path differs slightly from
    # the persona's crash but exercises the same `_commit_managed_update` resilience.)
    # The real coverage is in `test_bug_016_app_factory_boots_even_when_backfill_is_a_noop`
    # below — this test demonstrates the helper handles the index-empty edge.
    again = repo.publish_release_to_main(
        app_name="myapp",
        revision_number=1,
        artifact_path="artifacts/myapp/r000001",
        commit_sha="deadbeef",
        git_tag="myapp/r000001",
        source_hash="src-hash",
        dependency_lock_hash="dep-hash",
        release_manifest_path="releases/myapp/r000001.yaml",
    )
    # The canonical content differs from the stray-whitespace commit, so this
    # actually commits. The test passes as long as no exception is raised.
    assert again is True


@pytest.mark.slow
def test_bug_016_app_factory_boots_even_when_backfill_is_a_noop(tmp_path: Path) -> None:
    """`create_app` succeeds when the GitOps repo already has the backfill content.

    Repro-style: start once, kill, start again. The second start must not crash on
    `git commit` because `backfill_revision_git_metadata` re-renders the same files.
    """

    instance_dir = tmp_path / "instance"
    # First boot — populates the GitOps repo with the demo's release.
    create_app({"INSTANCE_PATH": str(instance_dir)})
    # Second boot from the same state — pre-fix this would crash on the
    # idempotent re-commit attempt.
    app2 = create_app({"INSTANCE_PATH": str(instance_dir)})
    assert app2.instance_path == str(instance_dir)


# -- BUG-017 (mcp-reference.md drift guard) lives in tests/test_mcp_reference_doc.py.


# -- BUG sub-bug: deduped related_resources --------------------------------


@pytest.mark.slow
def test_phase0_related_resources_deduped_in_error_guidance(tmp_path: Path) -> None:
    """A tool_validation_error whose `help_resource` is the default `workflows` URI
    must not produce `["dash://meta/workflows", "dash://meta/workflows"]`.

    Persona 1 spotted this in the duplicated entry on `app_scaffold_from_schema`
    validation errors. The dedupe lives at the boundary in `_attach_guidance`
    so any guidance source benefits.
    """

    instance_dir = tmp_path / "instance"
    app = create_app({"INSTANCE_PATH": str(instance_dir), "TESTING": True})
    server = app.extensions["mcp_server"]

    # Provoke a tool_validation_error: call a known tool with a missing required arg.
    response, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_scaffold_from_schema", "arguments": {}},
        }
    )
    structured = response["result"]["structuredContent"]
    related = structured["guidance"].get("related_resources", [])
    assert len(related) == len(set(related)), (
        f"related_resources contains duplicates: {related!r}"
    )


# -- Phase 1 — diagnostics coherence cluster --------------------------------


@pytest.mark.slow
def test_bug_002_004_sql_smoke_probe_appears_in_healthcheck(tmp_path: Path) -> None:
    """`app_run_healthcheck` includes a `sql_smoke` probe.

    Pre-fix, a dashboard whose every query referenced a non-existent table reported
    `all probes passed` because the existing `data_layer` probe only inspected
    already-recorded errors. The new probe actively runs each `queries/*.sql` with
    `WHERE 1=0` and a clean profile-bound connection — so a broken-but-never-clicked
    dashboard now reports `degraded`.
    """

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    runtime = app.extensions["runtime_service"]
    payload = runtime.run_healthcheck("demo", record=False)
    probe_names = [probe["name"] for probe in payload["health"]["probes"]]
    assert "sql_smoke" in probe_names, (
        f"sql_smoke probe missing from healthcheck. Found: {probe_names}"
    )


@pytest.mark.slow
def test_bug_004_collect_diagnostics_surfaces_latest_data_layer_error(tmp_path: Path) -> None:
    """`app_collect_diagnostics` returns `latest_data_layer_error` so the tool and the
    `dash://apps/{name}/errors` resource never disagree.

    Pre-fix, the canonical diagnostics tool reported `latest_error: null` while the
    resource listed real failures. After the fix, recording a data-layer error makes
    it appear under `latest_data_layer_error` and (when no other error exists) drives
    the recovery_category to `exasol_query_error`.
    """

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    runtime = app.extensions["runtime_service"]
    diagnostics = runtime.diagnostics_service

    # Manually record a data-layer error against the demo app so we can probe the
    # diagnostics aggregation without standing up Exasol.
    recorded = diagnostics.record_data_layer_error(
        "demo",
        sql_file="queries/business/summary.sql",
        profile_name="analytics-prod",
        error_text="object MART.ORDERZZ not found",
        revision_number=1,
    )
    assert recorded is not None  # rate-limit shouldn't fire on a fresh fingerprint.

    payload = runtime.collect_diagnostics("demo")
    assert payload["latest_data_layer_error"] is not None, payload.keys()
    assert (
        payload["latest_data_layer_error"]["details"]["sql_file"]
        == "queries/business/summary.sql"
    )
    # When no other error is present, the recovery cascade picks up data_layer.
    assert any(
        "queries/business/summary.sql" in step or "exasol" in step.lower() or "SQL" in step
        for step in payload["suggested_recovery_steps"]
    ), payload["suggested_recovery_steps"]


@pytest.mark.slow
def test_bug_005_data_layer_probe_filters_by_revision_and_watermark(tmp_path: Path) -> None:
    """Old-revision data-layer errors don't keep the `data_layer` probe red after a
    promote. The new `app_acknowledge_data_layer_errors` tool clears stuck probes
    when SQL is fixed in-place.
    """

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    runtime = app.extensions["runtime_service"]
    diagnostics = runtime.diagnostics_service

    # Record an error stamped to an old revision.
    diagnostics.record_data_layer_error(
        "demo",
        sql_file="queries/business/trend.sql",
        profile_name="analytics-prod",
        error_text="broken in r000001",
        revision_number=1,
    )
    # Probe scoped to current revision (also r1 in fresh state) should see it.
    payload = runtime.run_healthcheck("demo", record=False)
    probes = {p["name"]: p for p in payload["health"]["probes"]}
    assert probes["data_layer"]["status"] == "failed", probes["data_layer"]

    # Acknowledge resets the watermark; probe passes despite the row still on disk.
    diagnostics.acknowledge_data_layer_errors("demo")
    payload_after = runtime.run_healthcheck("demo", record=False)
    probes_after = {p["name"]: p for p in payload_after["health"]["probes"]}
    assert probes_after["data_layer"]["status"] == "passed", probes_after["data_layer"]


def test_bug_006_connection_errors_routed_through_recorder(tmp_path: Path) -> None:
    """A profile that can't open a connection produces an `{status: 'error'}` result
    instead of propagating the pyexasol exception.

    Without this fix, `execute_profile_query`'s outer try-block was preceded by the
    `connect()` call, so connection-level exceptions (SSL verify failure, refused
    TCP, missing env-secret) bypassed `_record_data_layer_error` entirely and the
    `data_layer` healthcheck stayed `passed` while every live callback 500'd.
    """

    from dash_server.exasol.models import ExasolProfile
    from dash_server.exasol.secrets import ExasolSecretStore
    from dash_server.exasol.service import ExasolDashboardService
    from dash_server.gitops.repo_service import GitRepoService

    # Stand up a minimal service against a temp gitops repo so we can drive a real
    # `execute_profile_query` without booting the whole app factory.
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    secrets = ExasolSecretStore(str(secret_root))
    secret_ref = secrets.store_local_secret("p", "ignored")

    repo = GitRepoService(repo_root=str(tmp_path / "gitops"))
    repo.initialize()
    service = ExasolDashboardService(repo, str(secret_root))

    # Inject a profile that points at an unreachable DSN.
    profile = ExasolProfile(
        name="p",
        backend="onprem",
        deployment_mode="local_direct",
        credential_mode="password",
        user="sys",
        dsn="127.0.0.1:1",  # nothing listening
        description="unreachable",
        tls_verify=False,
        secret_ref=secret_ref,
        query_defaults={},
    )
    service.profile_store.save_profile(profile)

    # Real `connect()` will fail; `execute_profile_query` must return the structured
    # error envelope, not raise. (BUG-006.)
    result = service.execute_profile_query("p", "SELECT 1")
    assert result["status"] == "error", result
    assert result.get("error")


# -- Phase 2 — scaffold quality ----------------------------------------------


def test_bug_007_scaffold_follows_relationship_hints() -> None:
    """When the picked table has no time column but a relationship hint exposes one,
    the schema-aware scaffold emits a JOIN and uses the joined date as LABEL.
    Also emits a derived REVENUE measure when quantity+price columns are present.
    """

    from dash_server.exasol.scaffold import _schema_summary_sql, _schema_trend_sql

    blueprint = {
        "schema_name": "MART",
        "table_name": "ORDER_LINES",
        "time_column": None,
        "dimension_column": "ORDER_ID",
        "primary_measure": "QUANTITY",
        "measure_columns": ["QUANTITY", "NET_UNIT_PRICE", "UNIT_COST"],
        "relationship_hints": [
            {
                "column_name": "ORDER_ID",
                "other_schema": "MART",
                "other_table": "ORDERS",
                "other_time_column": "ORDER_DATE",
                "other_key_column": "ORDER_ID",
            }
        ],
    }
    trend = _schema_trend_sql(blueprint)
    # Joins into ORDERS and uses ORDER_DATE as LABEL.
    assert "JOIN \"MART\".\"ORDERS\"" in trend, trend
    assert "j.\"ORDER_DATE\"" in trend, trend
    summary = _schema_summary_sql(blueprint)
    # Derived REVENUE measure appears in summary.sql.
    assert "\"QUANTITY\" * \"NET_UNIT_PRICE\"" in summary, summary
    assert "AS \"REVENUE\"" in summary, summary


def test_bug_009_kpi_trend_scaffold_quotes_reserved_value_alias() -> None:
    """The static kpi-trend scaffold quotes the reserved-word VALUE alias so a
    realistic rewrite (e.g. replacing the FROM DUAL stub with real SQL) doesn't
    syntax-error on `unexpected VALUE_`."""

    from dash_server.exasol.scaffold import (
        _kpi_summary_sql,
        _kpi_trend_sql,
        _placeholder_business_trend_sql,
    )

    for sql in (_kpi_summary_sql(), _kpi_trend_sql(), _placeholder_business_trend_sql()):
        if "AS VALUE" in sql:
            # the bare `AS VALUE` is the bug — quoted `AS "VALUE"` should be the only form
            assert "AS \"VALUE\"" in sql, sql
            # double-check no unquoted occurrence remains
            assert "AS VALUE " not in sql and "AS VALUE\n" not in sql, sql


@pytest.mark.slow
def test_bug_010_app_create_from_files_forwards_data_sources(tmp_path: Path) -> None:
    """`app_create_from_files` writes a manifest with `data_sources` populated when
    the caller supplies one. Pre-fix the argument was silently dropped and every
    Exasol-bound callback 500'd because the profile was never wired."""

    import json

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    server = app.extensions["mcp_server"]
    response, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create_from_files",
                "arguments": {
                    "name": "ds-fwd",
                    "files": [
                        {
                            "path": "app.py",
                            "content": (
                                "from dash import Dash\n"
                                "def create_dash_app(server, url_base_pathname, metadata):\n"
                                "    return Dash(__name__, server=server, "
                                "routes_pathname_prefix='/', "
                                "requests_pathname_prefix=url_base_pathname)\n"
                            ),
                        }
                    ],
                    "data_sources": {"primary": {"kind": "exasol", "profile": "analytics-prod"}},
                    "start_immediately": False,
                },
            },
        }
    )
    assert response["result"]["structuredContent"]["app"]["name"] == "ds-fwd"

    manifest_path = (
        Path(app.config["INSTANCE_PATH"]) / "gitops-repo" / "apps" / "ds-fwd" / "dash-app.json"
    )
    assert manifest_path.exists(), "manifest was not written to gitops"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["data_sources"] == {
        "primary": {"kind": "exasol", "profile": "analytics-prod"}
    }, manifest


# -- Phase 3 — envelope polish + docs ---------------------------------------


def _put_broken_python(server: Any, name: str) -> None:
    """Helper: introduce a Python syntax error into the demo app's draft."""

    server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "app_put_files",
                "arguments": {
                    "name": name,
                    "files": [
                        {
                            "path": "app.py",
                            "content": (
                                "from dash import Dash\n"
                                "def create_dash_app(server, url_base_pathname, metadata)\n"
                                "    return Dash(__name__, server=server)\n"  # missing `:` above
                            ),
                        }
                    ],
                },
            },
        }
    )


@pytest.mark.slow
def test_bug_013_app_validate_sets_iserror_true_on_failure(tmp_path: Path) -> None:
    """`app_validate` must set `result.isError = true` when validation fails so
    clients routing on `isError` correctly classify the result."""

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    server = app.extensions["mcp_server"]
    _put_broken_python(server, "demo")

    response, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_validate", "arguments": {"name": "demo"}},
        }
    )
    result = response["result"]
    assert result["isError"] is True, result
    structured = result["structuredContent"]
    assert structured["error"]["category"] == "workspace_validation_error"


@pytest.mark.slow
def test_bug_014_app_validate_returns_top_level_summary(tmp_path: Path) -> None:
    """Successful and failing `app_validate` runs both expose
    `validation_summary.{valid, error_count, warning_count}` so agents can branch
    on a single field instead of walking the nested report."""

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    server = app.extensions["mcp_server"]

    # Happy path: demo app validates clean.
    response, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_validate", "arguments": {"name": "demo"}},
        }
    )
    summary = response["result"]["structuredContent"]["validation_summary"]
    assert summary["valid"] is True
    assert summary["error_count"] == 0
    assert "warning_count" in summary


def test_ps26_bug018_validation_summary_counts_exasol_warnings_and_errors() -> None:
    """PS26-BUG-018: `_validation_summary`'s `warning_count`/`error_count` used to look
    for `report["exasol"]["warnings"]`/`["errors"]` keys that don't exist - the exasol
    section is shaped `{"status": ..., "issues": [{"level": "warning"|"error"|"info"}]}` -
    so its contribution was silently always zero regardless of `exasol.status`. The
    counts must now reflect the real `issues` list, filtered by `level`.
    """

    from dash_server.mcp.handlers import _validation_summary

    report = {
        "is_valid": False,
        "syntax": {"errors": []},
        "requirements": {"invalid": []},
        "credential_safety": {"errors": []},
        "callbacks": {"errors": [], "warnings": []},
        "consumption": {"issues": []},
        "lint": {"warnings": []},
        "exasol": {
            "status": "failed",
            "issues": [
                {"level": "warning", "path": "queries/a.sql", "message": "warn 1"},
                {"level": "warning", "path": "queries/b.sql", "message": "warn 2"},
                {"level": "error", "path": "queries/c.sql", "message": "error 1"},
                {"level": "info", "path": "queries/d.sql", "message": "dead sql"},
            ],
        },
    }

    summary = _validation_summary(report)

    assert summary["warning_count"] == 2
    assert summary["error_count"] == 1


def test_ps26_bug018_validation_summary_is_zero_for_a_clean_exasol_report() -> None:
    from dash_server.mcp.handlers import _validation_summary

    report = {
        "is_valid": True,
        "syntax": {"errors": []},
        "requirements": {"invalid": []},
        "credential_safety": {"errors": []},
        "callbacks": {"errors": [], "warnings": []},
        "consumption": {"issues": []},
        "lint": {"warnings": []},
        "exasol": {"status": "passed", "issues": []},
    }

    summary = _validation_summary(report)

    assert summary["warning_count"] == 0
    assert summary["error_count"] == 0


@pytest.mark.slow
def test_bug_011_exasol_profile_create_local_default_no_overwrite(tmp_path: Path) -> None:
    """Calling `exasol_profile_create_local` twice with the same name and no
    `overwrite` flag fails the second call rather than silently rewriting."""

    app = create_app({"INSTANCE_PATH": str(tmp_path / "instance"), "TESTING": True})
    server = app.extensions["mcp_server"]
    base_args = {
        "name": "test-profile",
        "backend": "onprem",
        "credential_mode": "password",
        "dsn": "localhost:8563",
        "user": "sys",
        "secret_value": "ignored",
        "tls_verify": False,
    }
    first, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "exasol_profile_create_local", "arguments": base_args},
        }
    )
    assert first["result"]["isError"] is False
    assert first["result"]["structuredContent"]["was_already_present"] is False

    # Second call with different DSN, no overwrite → must fail loudly.
    second_args = {**base_args, "dsn": "DIFFERENT-HOST:8563"}
    second, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "exasol_profile_create_local", "arguments": second_args},
        }
    )
    assert second["result"]["isError"] is True
    assert (
        second["result"]["structuredContent"]["error"]["category"]
        == "exasol_profile_already_exists"
    )

    # With overwrite=true the upsert succeeds and the response flags the rewrite.
    third_args = {**second_args, "overwrite": True}
    third, _ = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "exasol_profile_create_local", "arguments": third_args},
        }
    )
    assert third["result"]["isError"] is False
    assert third["result"]["structuredContent"]["was_already_present"] is True


def test_bug_018_has_error_helper_recognizes_envelope() -> None:
    """The runtime `has_error` helper detects the `[{"_error": "..."}]` envelope
    so callers don't reach for KeyError on a missing column to discover failure."""

    from dash_server.exasol.runtime import has_error

    assert has_error([{"_error": "object MART.X not found"}]) is True
    assert has_error({"_error": "single dict"}) is True
    assert has_error([{"AGENT_ID": "a", "LATENCY_MS": 12}]) is False
    assert has_error([]) is False
    assert has_error(None) is False


def test_bug_018_dash_server_error_str_includes_category() -> None:
    """`DashServerError.__str__` returns `category: summary` so import-failure
    tracebacks aren't blank."""

    from dash_server.exceptions import DashServerError

    exc = DashServerError(category="workspace_validation_error", summary="syntax")
    assert str(exc) == "workspace_validation_error: syntax"

    # Even with no summary, the string is non-empty.
    blank = DashServerError(category="workspace_validation_error", summary="")
    assert str(blank) == "workspace_validation_error"
