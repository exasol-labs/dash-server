"""Shared MCP resource-URI patterns and the unified resource routing table.

The two app-scoped resource URIs that carry an authorization capability were
written as raw regexes in both the server's read ladder and the blueprint's
transport gate. They live here once so the two cannot drift. This module also
owns ``ResourcesMixin``: one ``_resource_routes`` table from which both the read
dispatch and the ``resources/list`` definitions derive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import re
from typing import Any

from flask import current_app, has_request_context

from dash_server.dash_apps.factory import (
    app_authoring_guide,
    app_create_from_files_schema_help,
    app_create_schema_help,
)
from dash_server.exceptions import DashServerError

# `dash://apps/<app>/outputs` and `dash://exports/<job>` are the only resources
# gated by a capability (dashboard.export); both the server dispatch table and
# the blueprint transport gate match against these compiled patterns.
APP_OUTPUTS_RESOURCE_RE = re.compile(r"dash://apps/([a-z0-9-]+)/outputs")
EXPORT_RESOURCE_RE = re.compile(r"dash://exports/([0-9a-f-]+)")


@dataclass(frozen=True)
class _ServerResource:
    """``resources/list`` metadata for a concrete server-wide resource URI."""

    name: str
    title: str
    description: str


@dataclass(frozen=True)
class _AppResource:
    """``resources/list`` metadata template for a per-app resource.

    ``suffix`` is appended to ``dash://apps/{app}``; ``name``/``title`` are
    ``str.format(app=...)`` templates; ``description`` is literal.
    """

    suffix: str
    name: str
    title: str
    description: str


class ResourcesMixin:
    """Resource read dispatch and ``resources/list`` definitions."""

    def _resource_routes(self):
        """Single source for every resource: ``(matcher, handler, listing)``.

        ``matcher`` is an exact URI string or a compiled regex (drives dispatch);
        ``handler`` receives the regex match (or ``None`` for exact matches) and
        returns the payload; ``listing`` is a :class:`_ServerResource`,
        :class:`_AppResource`, or ``None`` (dispatch-only, e.g. deep-parameterized
        or dynamically listed resources). ``_resource_dispatch_table`` and
        ``_resource_definitions`` both derive from this list, so the transport gate,
        read ladder, and ``resources/list`` cannot drift. Patterns are anchored via
        ``fullmatch`` and mutually exclusive, so ordering is for readability only.
        """

        runtime = self.runtime_service
        return [
            (
                "dash://meta/app-create-schema",
                lambda _m: app_create_schema_help(),
                _ServerResource(
                    "app-create-schema",
                    "app_create bundle schema",
                    "Required bundle shape, common mistakes, and a working example for app_create.",
                ),
            ),
            (
                "dash://meta/app-create-from-files-schema",
                lambda _m: app_create_from_files_schema_help(),
                _ServerResource(
                    "app-create-from-files-schema",
                    "app_create_from_files schema",
                    "Required fields, common mistakes, and a working example for app_create_from_files.",
                ),
            ),
            (
                "dash://meta/app-authoring-guide",
                lambda _m: app_authoring_guide(),
                _ServerResource(
                    "app-authoring-guide",
                    "Dash app authoring guide",
                    "Recommended create_dash_app factory structure, prefix rules, and common mistakes.",
                ),
            ),
            (
                "dash://meta/workflows",
                lambda _m: self._workflow_resource(),
                _ServerResource(
                    "workflow-guide",
                    "Recommended MCP workflows",
                    "Canonical tool sequences for creating, editing, validating, and deploying hosted Dash apps.",
                ),
            ),
            (
                "dash://meta/session-channel-guide",
                lambda _m: self._session_channel_guide_payload(),
                _ServerResource(
                    "session-channel-guide",
                    "Browser session channel guide",
                    (
                        "The ctx helper reference, eval semantics, prop-access tiers, truncation "
                        "contract, and recipes for app_session_eval_js. Read this before running "
                        "JavaScript in a live dashboard tab."
                    ),
                ),
            ),
            (
                "dash://repo/status",
                lambda _m: self._repo_status_payload(),
                _ServerResource(
                    "repo-status",
                    "GitOps repository status",
                    "Read-only status for the local GitOps repository, including draft worktrees and current runtime-isolation settings.",
                ),
            ),
            (
                "dash://runtime/status",
                lambda _m: self._runtime_status_payload(),
                _ServerResource(
                    "runtime-status",
                    "Runtime isolation status",
                    (
                        "Current control-plane host/port, APP_DEPENDENCY_ISOLATION, "
                        "APP_RUNTIME_MODE, cache roots, and worker config knobs."
                    ),
                ),
            ),
            (
                "dash://runtime/workers",
                lambda _m: self._workers_payload(),
                _ServerResource(
                    "runtime-workers",
                    "Active runtime workers",
                    "Snapshot of out-of-process workers, baselines, RSS totals, and p50 cold-start time (isolated runtime mode).",
                ),
            ),
            (
                "dash://runtime/environments",
                lambda _m: self._environments_payload(),
                _ServerResource(
                    "runtime-environments",
                    "Per-app dependency environments",
                    "Inventory of materialized per-app envs, disk usage, and wheel-cache size (per_app dependency mode).",
                ),
            ),
            (
                "dash://runtime/logs/runtime.events",
                lambda _m: runtime.diagnostics_service.tail_logs(
                    "__runtime__", channel="runtime.events", limit=200
                ),
                _ServerResource(
                    "runtime-events",
                    "Runtime audit events",
                    "Server-wide audit log of operational decisions: env_evicted, wheel_cache_pruned, wheel_cache_gc_skipped, unsafe_override_warning.",
                ),
            ),
            (
                "dash://repo/desired-state",
                lambda _m: runtime.git_desired_state(),
                _ServerResource(
                    "repo-desired-state",
                    "Git desired state",
                    "Authoritative live and preview deployment intent parsed from the GitOps repository.",
                ),
            ),
            (
                "dash://repo/drift",
                lambda _m: runtime.git_drift_report(),
                _ServerResource(
                    "repo-drift",
                    "Git desired-state drift",
                    "Comparison between Git desired state and the observed runtime and cache state.",
                ),
            ),
            (
                "dash://exasol/help/connection-modes",
                lambda _m: self._exasol_service().connection_modes_help(),
                _ServerResource(
                    "exasol-connection-modes",
                    "Exasol connection modes",
                    "Phase 0 local Exasol connection modes, required fields, and the recommended dashboard workflow.",
                ),
            ),
            (
                "dash://exasol/help/dashboard-patterns",
                lambda _m: self._exasol_service().dashboard_patterns_help(),
                _ServerResource(
                    "exasol-dashboard-patterns",
                    "Exasol dashboard patterns",
                    "Built-in Exasol dashboard scaffold patterns and when to use them.",
                ),
            ),
            (
                "dash://exasol/help/agent-workflow",
                lambda _m: self._exasol_service().agent_workflow_help(),
                _ServerResource(
                    "exasol-agent-workflow",
                    "Exasol agent workflow",
                    "Recommended separation of responsibilities between dash-server and an external Exasol MCP server.",
                ),
            ),
            (
                "dash://exasol/help/sql-placeholders",
                lambda _m: self._exasol_service().sql_placeholders_help(),
                _ServerResource(
                    "exasol-sql-placeholders",
                    "Exasol SQL placeholder syntax",
                    "pyexasol placeholder grammar ({name!s}, {name!d}, etc.) for parameterized dashboard SQL. Replaces SQL-driver :name syntax which Exasol rejects.",
                ),
            ),
            (
                "dash://exasol/profiles",
                lambda _m: self._exasol_service().list_profiles(),
                _ServerResource(
                    "exasol-profiles",
                    "Exasol profiles",
                    "Git-tracked Exasol profile metadata without secrets.",
                ),
            ),
            (
                re.compile(r"dash://exasol/profiles/([a-z0-9-]+)"),
                lambda m: self._exasol_service().get_profile(m.group(1)),
                None,
            ),
            (
                "dash://apps",
                lambda _m: {"apps": runtime.list_apps()},
                _ServerResource(
                    "dash-apps",
                    "Hosted Dash apps",
                    "Inventory of the currently registered Dash apps.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)"),
                lambda m: runtime.get_app_overview(m.group(1)),
                _AppResource(
                    "",
                    "{app}-app",
                    "{app} app",
                    "Current app overview including exposure, runtime, and revision pointers.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/status"),
                lambda m: runtime.get_app_status(m.group(1)),
                _AppResource(
                    "/status",
                    "{app}-status",
                    "{app} status",
                    "Lifecycle state, runtime mount state, revision pointers, and draft workspace state.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/health"),
                lambda m: runtime.run_healthcheck(m.group(1), record=False),
                _AppResource(
                    "/health",
                    "{app}-health",
                    "{app} health",
                    "Structured health probe results for the live app route.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/routes"),
                lambda m: runtime.get_routes(m.group(1)),
                _AppResource(
                    "/routes",
                    "{app}-routes",
                    "{app} routes",
                    "Live and preview route bindings for the app.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/sessions"),
                lambda m: self._app_sessions_payload(m.group(1)),
                _AppResource(
                    "/sessions",
                    "{app}-sessions",
                    "{app} browser sessions",
                    (
                        "Browser tabs currently attached to the app, with liveness and the "
                        "prop-access tier each reported. Local mode only."
                    ),
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/permissions"),
                lambda m: runtime.get_permissions(m.group(1)),
                _AppResource(
                    "/permissions",
                    "{app}-permissions",
                    "{app} permissions",
                    "Declared filesystem, network, and env permissions for the app.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/sharing"),
                lambda m: self._share_payload(m.group(1)),
                _AppResource(
                    "/sharing",
                    "{app}-sharing",
                    "{app} sharing",
                    "Share policy, active ACL grants, revoked ACL grants, and warnings.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/manifest"),
                lambda m: runtime.get_manifest(m.group(1)),
                _AppResource(
                    "/manifest",
                    "{app}-manifest",
                    "{app} manifest",
                    "Current manifest for the app's live revision.",
                ),
            ),
            (
                APP_OUTPUTS_RESOURCE_RE,
                lambda m: self._consumption_service().list_outputs(
                    m.group(1), self._consumption_auth_context()
                ),
                _AppResource(
                    "/outputs",
                    "{app}-outputs",
                    "{app} registered outputs",
                    "Governed dataset and view outputs for the current live revision.",
                ),
            ),
            (
                EXPORT_RESOURCE_RE,
                lambda m: self._consumption_service().get_export(
                    m.group(1), self._consumption_auth_context()
                ),
                None,
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/revisions"),
                lambda m: runtime.list_revisions(m.group(1)),
                _AppResource(
                    "/revisions",
                    "{app}-revisions",
                    "{app} revisions",
                    "Immutable revisions for the app.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/revisions/([0-9]+)"),
                lambda m: runtime.get_revision_details(m.group(1), int(m.group(2))),
                None,
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/events"),
                lambda m: runtime.list_events(m.group(1)),
                _AppResource(
                    "/events",
                    "{app}-events",
                    "{app} events",
                    "Event log for revision build, preview, promote, rollback, and workspace edits.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/logs/latest"),
                lambda m: runtime.tail_logs(m.group(1), channel="latest"),
                _AppResource(
                    "/logs/latest",
                    "{app}-logs-latest",
                    "{app} latest logs",
                    "Recent log entries aggregated across runtime, build, and health channels.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/logs/runtime"),
                lambda m: runtime.tail_logs(m.group(1), channel="runtime"),
                _AppResource(
                    "/logs/runtime",
                    "{app}-logs-runtime",
                    "{app} runtime logs",
                    "Recent runtime mount and lifecycle log entries.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/logs/build"),
                lambda m: runtime.tail_logs(m.group(1), channel="build"),
                _AppResource(
                    "/logs/build",
                    "{app}-logs-build",
                    "{app} build logs",
                    "Recent build, validation, and workspace-edit log entries.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/errors"),
                lambda m: runtime.get_errors(m.group(1)),
                _AppResource(
                    "/errors",
                    "{app}-errors",
                    "{app} errors",
                    "Structured build and runtime errors captured for the app.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/callback-failures"),
                lambda m: runtime.get_callback_failures(m.group(1)),
                _AppResource(
                    "/callback-failures",
                    "{app}-callback-failures",
                    "{app} callback failures",
                    "Structured callback error records captured for the app.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/dependency-report"),
                lambda m: runtime.get_dependency_report(m.group(1)),
                _AppResource(
                    "/dependency-report",
                    "{app}-dependency-report",
                    "{app} dependency report",
                    "Declared requirements, invalid requirement entries, and install-plan notes for the draft workspace.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/files"),
                lambda m: runtime.list_workspace_files(m.group(1)),
                _AppResource(
                    "/files",
                    "{app}-files",
                    "{app} files",
                    "List of editable draft files in the app workspace.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/files/(.+)"),
                lambda m: runtime.read_workspace_file(m.group(1), m.group(2)),
                None,
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/diff/current\.\.\.draft"),
                lambda m: runtime.diff_workspace(m.group(1)),
                _AppResource(
                    "/diff/current...draft",
                    "{app}-diff",
                    "{app} live-to-draft diff",
                    "Unified diff between the current live revision artifact and the draft workspace.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/artifacts/latest/files"),
                lambda m: runtime.get_latest_artifact_files(m.group(1)),
                _AppResource(
                    "/artifacts/latest/files",
                    "{app}-artifact-files-latest",
                    "{app} latest artifact files",
                    "List of source files present in the latest built artifact revision.",
                ),
            ),
            (
                re.compile(r"dash://apps/([a-z0-9-]+)/diff/latest-build\.\.\.draft"),
                lambda m: runtime.diff_workspace_against_artifact(m.group(1)),
                _AppResource(
                    "/diff/latest-build...draft",
                    "{app}-diff-latest-build",
                    "{app} latest-build-to-draft diff",
                    "Unified diff and per-file comparison between the latest built artifact and the draft workspace.",
                ),
            ),
        ]

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_uri = params.get("uri")
        if not isinstance(raw_uri, str):
            raise DashServerError(
                category="invalid_resource_uri",
                summary="`uri` must be a string.",
                details={"received_type": type(raw_uri).__name__},
            )
        uri: str = raw_uri
        for matcher, handler in self._resource_dispatch_table():
            if isinstance(matcher, str):
                if uri == matcher:
                    return self._resource_contents(uri, handler(None))
            else:
                match = matcher.fullmatch(uri)
                if match is not None:
                    return self._resource_contents(uri, handler(match))

        raise DashServerError(
            category="resource_not_found",
            summary="Unknown resource.",
            details={"uri": uri},
        )


    def _resource_dispatch_table(
        self,
    ) -> list[tuple[str | re.Pattern[str], Callable[[Any], dict[str, Any]]]]:
        """Ordered ``(matcher, handler)`` dispatch, projected from ``_resource_routes``."""

        return [(matcher, handler) for matcher, handler, _listing in self._resource_routes()]

    def _resource_contents(self, uri: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._attach_absolute_urls(payload)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, indent=2),
                }
            ]
        }


    def _runtime_isolation_snapshot(self) -> dict[str, Any]:
        """Read-only view of runtime-isolation config and cache roots."""

        if not has_request_context():
            return {}
        config = current_app.config
        return {
            "control_plane_host": config.get("DASH_SERVER_HOST"),
            "control_plane_port": config.get("DASH_SERVER_PORT"),
            "dependency_isolation": config.get("APP_DEPENDENCY_ISOLATION", "shared"),
            "runtime_mode": config.get("APP_RUNTIME_MODE", "in_process"),
            "app_environments_root": config.get("APP_ENVIRONMENTS_ROOT"),
            "wheel_cache_root": config.get("APP_WHEEL_CACHE_ROOT"),
            "pycache_root": config.get("APP_PYCACHE_ROOT"),
            "environments_disk_cap_gb": config.get("APP_ENVIRONMENTS_DISK_CAP_GB"),
            "wheel_cache_disk_cap_gb": config.get("APP_WHEEL_CACHE_DISK_CAP_GB"),
            "worker_host": config.get("APP_WORKER_HOST", "127.0.0.1"),
            "worker_port_range": config.get("APP_WORKER_PORT_RANGE"),
            "worker_prewarm_pool_size": config.get("APP_WORKER_PREWARM_POOL_SIZE"),
            "worker_idle_stop_seconds": config.get("APP_WORKER_IDLE_STOP_SECONDS"),
        }


    def _repo_status_payload(self) -> dict[str, Any]:
        payload = self.git_repo_service.status()
        payload["runtime_isolation"] = self._runtime_isolation_snapshot()
        return payload


    def _session_channel_guide_payload(self) -> dict[str, Any]:
        """The ctx/eval reference, with this server's live channel settings folded in.

        Readable even when the channel is disabled: an agent should be able to learn
        why it cannot use the channel from the same place it learns how to.
        """

        from dash_server.session_channel.guide import session_channel_guide

        service = getattr(self, "session_channel_service", None)
        return session_channel_guide(service.status() if service is not None else None)


    def _app_sessions_payload(self, app_name: str) -> dict[str, Any]:
        service = getattr(self, "session_channel_service", None)
        if service is None:
            return {"app": app_name, "sessions": [], "live_count": 0, "channel": {"enabled": False}}
        payload = service.list_sessions(app_name=app_name)
        payload["app"] = app_name
        return payload


    def _runtime_status_payload(self) -> dict[str, Any]:
        payload = {
            "resource": "dash://runtime/status",
            "summary": (
                "Runtime isolation settings for hosted app dependency installs and serving. "
                "See plans/app-runtime-isolation-and-dependency-environments-plan.md."
            ),
            **self._runtime_isolation_snapshot(),
        }
        if self.consumption_service is not None:
            payload["consumption_coordinator"] = self.consumption_service.coordinator_status()
        return payload


    def _resource_definitions(self) -> list[dict[str, Any]]:
        routes = self._resource_routes()
        resources: list[dict[str, Any]] = []
        for matcher, _handler, listing in routes:
            if isinstance(listing, _ServerResource):
                resources.append(
                    {
                        "uri": matcher,
                        "name": listing.name,
                        "title": listing.title,
                        "description": listing.description,
                        "mimeType": "application/json",
                    }
                )
        for app in self.runtime_service.list_apps():
            app_name = app["name"]
            for _matcher, _handler, listing in routes:
                if isinstance(listing, _AppResource):
                    resources.append(
                        {
                            "uri": f"dash://apps/{app_name}{listing.suffix}",
                            "name": listing.name.format(app=app_name),
                            "title": listing.title.format(app=app_name),
                            "description": listing.description,
                            "mimeType": "application/json",
                        }
                    )
        try:
            exports = self._consumption_service().list_exports(
                self._consumption_auth_context()
            )
        except DashServerError:
            exports = {"jobs": []}
        for export in exports["jobs"]:
            job = export["job"]
            resources.append(
                {
                    "uri": f"dash://exports/{job['id']}",
                    "name": f"export-{job['id']}",
                    "title": f"Export {job['id']}",
                    "description": f"Principal-bound {job['status']} export for {job['app_name']}.",
                    "mimeType": "application/json",
                }
            )
        return resources

    def _workers_payload(self) -> dict[str, Any]:
        manager = self._worker_manager_or_error()
        workers = manager.list_workers()
        idle_count = sum(1 for w in workers if w.get("status") == "stopped_idle")
        return {
            "workers": workers,
            "baselines": manager.baseline_status(),
            "worker_count": len(workers),
            "idle_count": idle_count,
            "rss_bytes_total": manager.total_rss_bytes(),
            "last_start_ms_p50": manager.start_time_ms_p50(),
        }


    def _environments_payload(self) -> dict[str, Any]:
        env_service = self._dep_env_service_or_error()
        environments = env_service.list_environments()
        return {
            "environments": environments,
            "environment_count": len(environments),
            "bytes_on_disk_total": env_service.total_bytes_on_disk(),
            "wheel_cache_bytes": env_service.wheel_cache_bytes(),
        }


    def _workflow_resource(self) -> dict[str, Any]:
        return {
            "resource": "dash://meta/workflows",
            "summary": "Canonical tool sequences for the most common hosted-app workflows.",
            "workflows": [
                {
                    "name": "create_starter_app",
                    "steps": ["app_create", "app_validate", "app_deploy_draft"],
                },
                {
                    "name": "create_from_files",
                    "steps": ["app_create_from_files", "app_validate", "app_deploy_draft"],
                },
                {
                    "name": "create_exasol_dashboard",
                    "steps": ["exasol_profile_create_local", "exasol_profile_validate", "app_create_exasol_dashboard"],
                },
                {
                    "name": "create_exasol_dashboard_with_external_mcp",
                    "steps": ["Read dash://exasol/help/agent-workflow", "Use external Exasol MCP for schema discovery and SQL prototyping", "exasol_profile_validate", "app_create_exasol_dashboard", "app_validate", "app_deploy_draft"],
                },
                {
                    "name": "edit_existing_app",
                    "steps": ["app_put_files", "app_patch_file", "app_validate", "app_deploy_draft"],
                },
                {
                    "name": "manual_revision_control",
                    "steps": ["app_validate", "app_build", "app_start_preview", "app_promote_revision"],
                },
                {
                    "name": "diagnose_failure",
                    "steps": ["app_collect_diagnostics", "app_tail_logs", "app_inspect_traceback", "app_patch_file", "app_validate"],
                },
                {
                    "name": "apply_direct_git_changes",
                    "steps": ["repo_reconcile", "app_get_status", "app_run_healthcheck"],
                },
            ],
        }


__all__ = ["APP_OUTPUTS_RESOURCE_RE", "EXPORT_RESOURCE_RE"]
