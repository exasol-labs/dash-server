"""MCP server for the hosted Dash control plane."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
import re
import secrets
from typing import Any
from collections.abc import Callable

import jsonschema
from flask import current_app, has_request_context, request

from dash_server.auth import AuthContext, Principal, current_auth_context
from dash_server.dash_apps.factory import (
    app_authoring_guide,
    app_create_example_bundle,
    app_create_from_files_example,
    app_create_from_files_schema_help,
    app_create_schema_help,
)
from dash_server.exasol import ExasolDashboardService
from dash_server.exceptions import DashServerError
from dash_server.gitops import GitRepoService
from dash_server.mailer import InvitationEmailSender
from dash_server.runtime.service import AppRuntimeService

LOGGER = logging.getLogger(__name__)


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    """Drop duplicates from a string list while preserving first-seen order.

    Used by the guidance builder so error responses don't list the same URI twice
    (e.g. when a tool's `help_resource` happens to equal the default fallback).
    """

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _validation_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Build the top-level `{valid, error_count, warning_count}` summary for app_validate.

    Walks the nested `validation.*` sub-sections and counts entries flagged as errors
    or warnings. Used by `_tool_app_validate` so agents have a flat field to branch on
    rather than reasoning over the full nested payload.
    """

    def _len(node: Any, key: str) -> int:
        return len(node[key]) if isinstance(node, dict) and isinstance(node.get(key), list) else 0

    error_count = (
        _len(report.get("syntax"), "errors")
        + _len(report.get("requirements"), "invalid")
        + _len(report.get("exasol"), "errors")
        + _len(report.get("credential_safety"), "errors")
        + _len(report.get("callbacks"), "errors")
    )
    warning_count = (
        _len(report.get("lint"), "warnings")
        + _len(report.get("exasol"), "warnings")
        + _len(report.get("callbacks"), "warnings")
    )
    return {
        "valid": bool(report.get("is_valid")),
        "error_count": error_count,
        "warning_count": warning_count,
    }


class MCPServer:
    """MCP implementation for hosted Dash control-plane tools and resources."""

    protocol_version = "2025-06-18"
    # Phase 3.5d added "worker" + "worker.events" for isolated-mode workers.
    _log_channels = ("latest", "build", "runtime", "health", "worker", "worker.events")

    def __init__(
        self,
        runtime_service: AppRuntimeService,
        git_repo_service: GitRepoService,
        exasol_dashboard_service: ExasolDashboardService | None = None,
        email_sender: InvitationEmailSender | None = None,
    ) -> None:
        self.runtime_service = runtime_service
        self.git_repo_service = git_repo_service
        self.exasol_dashboard_service = exasol_dashboard_service
        self.email_sender = email_sender
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "apps_list": self._tool_apps_list,
            "repo_reconcile": self._tool_repo_reconcile,
            "exasol_profiles_list": self._tool_exasol_profiles_list,
            "exasol_profile_create_local": self._tool_exasol_profile_create_local,
            "exasol_profile_validate": self._tool_exasol_profile_validate,
            "app_create": self._tool_app_create,
            "app_create_from_files": self._tool_app_create_from_files,
            "app_create_exasol_dashboard": self._tool_app_create_exasol_dashboard,
            "app_scaffold_from_schema": self._tool_app_scaffold_from_schema,
            "app_start": self._tool_app_start,
            "app_stop": self._tool_app_stop,
            "app_restart": self._tool_app_restart,
            "app_get_status": self._tool_app_get_status,
            "app_build": self._tool_app_build,
            "app_deploy_draft": self._tool_app_deploy_draft,
            "app_start_preview": self._tool_app_start_preview,
            "app_promote_revision": self._tool_app_promote_revision,
            "app_rollback": self._tool_app_rollback,
            "app_put_files": self._tool_app_put_files,
            "app_read_file": self._tool_app_read_file,
            "app_diff_draft_vs_artifact": self._tool_app_diff_draft_vs_artifact,
            "app_patch_file": self._tool_app_patch_file,
            "app_delete_file": self._tool_app_delete_file,
            "app_validate": self._tool_app_validate,
            "app_collect_diagnostics": self._tool_app_collect_diagnostics,
            "app_inspect_traceback": self._tool_app_inspect_traceback,
            "app_tail_logs": self._tool_app_tail_logs,
            "app_run_healthcheck": self._tool_app_run_healthcheck,
            "app_share_get": self._tool_app_share_get,
            "app_share_grant": self._tool_app_share_grant,
            "app_share_revoke": self._tool_app_share_revoke,
            "app_share_set_link_scope": self._tool_app_share_set_link_scope,
            "app_share_explain_access": self._tool_app_share_explain_access,
            "app_share_create_one_time_link": self._tool_app_share_create_one_time_link,
            "app_share_revoke_one_time_link": self._tool_app_share_revoke_one_time_link,
            "app_invite_external_user": self._tool_app_invite_external_user,
            "app_revoke_external_invitation": self._tool_app_revoke_external_invitation,
            # Phase 4f: runtime / environment introspection + control.
            "app_runtime_workers_list": self._tool_app_runtime_workers_list,
            "app_runtime_workers_restart": self._tool_app_runtime_workers_restart,
            "app_environment_invalidate": self._tool_app_environment_invalidate,
            "app_acknowledge_data_layer_errors": self._tool_app_acknowledge_data_layer_errors,
        }

    def sse_ready_event(self) -> str:
        event = {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": "info",
                "data": "dash-server MCP endpoint ready",
            },
        }
        return f"event: message\ndata: {json.dumps(event)}\n\n"

    def handle_jsonrpc(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params", {})

        if payload.get("jsonrpc") != "2.0":
            return self._error_response(
                request_id, {"code": -32600, "message": "Only JSON-RPC 2.0 is supported."}, 200
            )
        if not isinstance(method, str):
            return self._error_response(
                request_id, {"code": -32600, "message": "A string method is required."}, 200
            )
        if not isinstance(params, dict):
            return self._error_response(
                request_id, {"code": -32602, "message": "Params must be an object."}, 200
            )

        try:
            if method == "initialize":
                return self._success_response(request_id, self._initialize_result())
            if method == "ping":
                return self._success_response(request_id, {})
            if method == "tools/list":
                return self._success_response(request_id, {"tools": self._tool_definitions()})
            if method == "tools/call":
                return self._success_response(request_id, self._call_tool(params))
            if method == "resources/list":
                return self._success_response(request_id, {"resources": self._resource_definitions()})
            if method == "resources/read":
                return self._success_response(request_id, self._read_resource(params))
            if method == "notifications/initialized":
                return self._success_response(request_id, {})
        except DashServerError as exc:
            self._log_mcp_error(method, params, exc)
            return self._error_response(request_id, exc.to_error_object(), 200)

        return self._error_response(
            request_id, {"code": -32601, "message": f"Method not found: {method}"}, 200
        )

    def _initialize_result(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "serverInfo": {"name": "dash-server", "version": "0.5.0"},
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if not isinstance(name, str):
                raise DashServerError(
                    category="tool_validation_error",
                    summary="Tool `name` must be a string.",
                    details={"received_type": type(name).__name__},
                    jsonrpc_code=-32602,
                )
            if not isinstance(arguments, dict):
                raise DashServerError(
                    category="tool_validation_error",
                    summary="Tool arguments must be an object.",
                    details={"tool": name},
                    jsonrpc_code=-32602,
                )
            handler = self._tool_handlers.get(name)
            if handler is None:
                raise DashServerError(
                    category="tool_not_found",
                    summary="Unknown tool.",
                    details={"tool": name},
                    jsonrpc_code=-32602,
                )
            self._validate_tool_arguments(str(name), arguments)
            return handler(arguments)
        except DashServerError as exc:
            self._log_mcp_error("tools/call", params, exc)
            return self._tool_error_result(str(name), exc)

    def _validate_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        schema = self._tool_input_schema(tool_name)
        if schema is None:
            return
        try:
            jsonschema.validate(arguments, schema)
        except jsonschema.ValidationError as exc:
            path = list(exc.absolute_path)
            validator = exc.validator
            extra_provided: list[str] = []
            if validator == "additionalProperties":
                allowed_props = self._resolve_subschema_properties(schema, path)
                provided = self._resolve_subobject(arguments, path)
                extra_provided = (
                    sorted(set(provided) - set(allowed_props)) if isinstance(provided, dict) else []
                )
                summary = (
                    f"Unknown argument(s) for {tool_name}: {', '.join(extra_provided) or '?'}. "
                    f"Allowed: {', '.join(sorted(allowed_props)) or '(none)'}."
                )
            elif validator == "required":
                required = self._resolve_subschema_required(schema, path)
                provided = self._resolve_subobject(arguments, path)
                missing = sorted(set(required) - set(provided)) if isinstance(provided, dict) else list(required)
                summary = (
                    f"Missing required argument(s) for {tool_name}: {', '.join(missing) or '?'}."
                )
            elif validator == "anyOf":
                summary = (
                    f"Arguments for {tool_name} did not match any allowed shape. "
                    "See the schema-help resource for accepted shapes."
                )
            else:
                summary = f"Invalid arguments for {tool_name}: {exc.message}"

            category, extra_details = self._schema_error_context(tool_name, path, extra_provided)
            details: dict[str, Any] = {
                "tool": tool_name,
                "path": path,
                "schema_path": [str(part) for part in exc.absolute_schema_path],
                **extra_details,
            }
            raise DashServerError(
                category=category,
                summary=summary,
                details=details,
                jsonrpc_code=-32602,
            ) from exc

    @staticmethod
    def _resolve_subobject(arguments: dict[str, Any], path: list[Any]) -> Any:
        current: Any = arguments
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return current
        return current

    @staticmethod
    def _resolve_subschema_properties(schema: dict[str, Any], path: list[Any]) -> list[str]:
        current: Any = schema
        for key in path:
            if isinstance(current, dict) and "properties" in current and key in current["properties"]:
                current = current["properties"][key]
                continue
            return []
        if isinstance(current, dict):
            return sorted((current.get("properties") or {}).keys())
        return []

    @staticmethod
    def _resolve_subschema_required(schema: dict[str, Any], path: list[Any]) -> list[str]:
        current: Any = schema
        for key in path:
            if isinstance(current, dict) and "properties" in current and key in current["properties"]:
                current = current["properties"][key]
                continue
            return list(current.get("required", []) if isinstance(current, dict) else [])
        return list(current.get("required", []) if isinstance(current, dict) else [])

    def _schema_error_context(
        self, tool_name: str, path: list[Any], extra_provided: list[str]
    ) -> tuple[str, dict[str, Any]]:
        """Return (category, extra_details) for a tool argument schema violation."""

        if tool_name == "app_create":
            # Special case: someone tried to pass `files` to app_create. Point them
            # to app_create_from_files instead of dwelling on the schema mismatch.
            if "files" in extra_provided:
                redirect = app_create_from_files_schema_help()
                return "tool_validation_error", {
                    "help_resource": redirect.get("help_resource"),
                    "common_mistakes": redirect.get("common_mistakes", []),
                    "example": redirect.get("example", {}),
                }
            help_payload = app_create_schema_help()
            details = {
                "help_resource": help_payload.get("help_resource"),
                "common_mistakes": help_payload.get("common_mistakes", []),
                "example": help_payload.get("example", {}),
            }
            if path and path[0] == "bundle":
                if len(path) >= 2 and path[1] == "manifest":
                    return "manifest_validation_error", details
                return "bundle_validation_error", details
            return "tool_validation_error", details
        if tool_name == "app_create_from_files":
            help_payload = app_create_from_files_schema_help()
            details = {
                "help_resource": help_payload.get("help_resource"),
                "common_mistakes": help_payload.get("common_mistakes", []),
                "example": help_payload.get("example", {}),
            }
            return "tool_validation_error", details
        return "tool_validation_error", {}

    def _tool_input_schema(self, tool_name: str) -> dict[str, Any] | None:
        if not hasattr(self, "_tool_schemas_cache"):
            self._tool_schemas_cache = {
                tool["name"]: tool.get("inputSchema")
                for tool in self._tool_definitions()
                if isinstance(tool.get("inputSchema"), dict)
            }
        return self._tool_schemas_cache.get(tool_name)

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_uri = params.get("uri")
        if not isinstance(raw_uri, str):
            raise DashServerError(
                category="invalid_resource_uri",
                summary="`uri` must be a string.",
                details={"received_type": type(raw_uri).__name__},
                jsonrpc_code=-32602,
            )
        uri: str = raw_uri
        if uri == "dash://meta/app-create-schema":
            return self._resource_contents(uri, app_create_schema_help())
        if uri == "dash://meta/app-create-from-files-schema":
            return self._resource_contents(uri, app_create_from_files_schema_help())
        if uri == "dash://meta/app-authoring-guide":
            return self._resource_contents(uri, app_authoring_guide())
        if uri == "dash://meta/workflows":
            return self._resource_contents(uri, self._workflow_resource())
        if uri == "dash://repo/status":
            return self._resource_contents(uri, self._repo_status_payload())
        if uri == "dash://runtime/status":
            return self._resource_contents(uri, self._runtime_status_payload())
        if uri == "dash://runtime/workers":
            return self._resource_contents(uri, self._workers_payload())
        if uri == "dash://runtime/environments":
            return self._resource_contents(uri, self._environments_payload())
        if uri == "dash://runtime/logs/runtime.events":
            # Phase 5c: server-wide audit trail for GC + override events.
            return self._resource_contents(
                uri,
                self.runtime_service.diagnostics_service.tail_logs(
                    "__runtime__", channel="runtime.events", limit=200
                ),
            )
        if uri == "dash://repo/desired-state":
            return self._resource_contents(uri, self.runtime_service.git_desired_state())
        if uri == "dash://repo/drift":
            return self._resource_contents(uri, self.runtime_service.git_drift_report())
        if uri == "dash://exasol/help/connection-modes":
            return self._resource_contents(uri, self._exasol_service().connection_modes_help())
        if uri == "dash://exasol/help/dashboard-patterns":
            return self._resource_contents(uri, self._exasol_service().dashboard_patterns_help())
        if uri == "dash://exasol/help/agent-workflow":
            return self._resource_contents(uri, self._exasol_service().agent_workflow_help())
        if uri == "dash://exasol/help/sql-placeholders":
            return self._resource_contents(uri, self._exasol_service().sql_placeholders_help())
        if uri == "dash://exasol/profiles":
            return self._resource_contents(uri, self._exasol_service().list_profiles())

        match = re.fullmatch(r"dash://exasol/profiles/([a-z0-9-]+)", str(uri))
        if match:
            return self._resource_contents(uri, self._exasol_service().get_profile(match.group(1)))

        if uri == "dash://apps":
            return self._resource_contents(uri, {"apps": self.runtime_service.list_apps()})

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_app_overview(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/status", str(uri))
        if match:
            return self._resource_contents(
                uri, self.runtime_service.get_app_status(match.group(1))
            )

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/health", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.run_healthcheck(match.group(1), record=False))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/routes", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_routes(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/permissions", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_permissions(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/sharing", str(uri))
        if match:
            return self._resource_contents(uri, self._share_payload(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/manifest", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_manifest(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/revisions", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.list_revisions(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/revisions/([0-9]+)", str(uri))
        if match:
            return self._resource_contents(
                uri,
                self.runtime_service.get_revision_details(match.group(1), int(match.group(2))),
            )

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/events", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.list_events(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/logs/latest", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.tail_logs(match.group(1), channel="latest"))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/logs/runtime", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.tail_logs(match.group(1), channel="runtime"))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/logs/build", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.tail_logs(match.group(1), channel="build"))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/errors", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_errors(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/callback-failures", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_callback_failures(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/dependency-report", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_dependency_report(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/files", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.list_workspace_files(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/files/(.+)", str(uri))
        if match:
            app_name = match.group(1)
            relative_path = match.group(2)
            return self._resource_contents(
                uri, self.runtime_service.read_workspace_file(app_name, relative_path)
            )

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/diff/current\.\.\.draft", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.diff_workspace(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/artifacts/latest/files", str(uri))
        if match:
            return self._resource_contents(uri, self.runtime_service.get_latest_artifact_files(match.group(1)))

        match = re.fullmatch(r"dash://apps/([a-z0-9-]+)/diff/latest-build\.\.\.draft", str(uri))
        if match:
            return self._resource_contents(
                uri,
                self.runtime_service.diff_workspace_against_artifact(match.group(1)),
            )

        raise DashServerError(
            category="resource_not_found",
            summary="Unknown resource.",
            details={"uri": uri},
            jsonrpc_code=-32602,
        )

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
            "control_plane_host": config.get("DASH_SERVER_HOST", "127.0.0.1"),
            "control_plane_port": config.get("DASH_SERVER_PORT", 5100),
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

    def _runtime_status_payload(self) -> dict[str, Any]:
        return {
            "resource": "dash://runtime/status",
            "summary": (
                "Runtime isolation settings for hosted app dependency installs and serving. "
                "See plans/app-runtime-isolation-and-dependency-environments-plan.md."
            ),
            **self._runtime_isolation_snapshot(),
        }

    def _tool_apps_list(self, _: dict[str, Any]) -> dict[str, Any]:
        apps = self.runtime_service.list_apps()
        app_names = ", ".join(app["name"] for app in apps) if apps else "none"
        return self._tool_result(
            "apps_list",
            text=f"Listed {len(apps)} hosted app(s): {app_names}.",
            structured_content={"apps": apps},
        )

    def _tool_repo_reconcile(self, _: dict[str, Any]) -> dict[str, Any]:
        reconciled = self.runtime_service.reconcile_git_desired_state()
        return self._tool_result(
            "repo_reconcile",
            text="Reconciled runtime state from the Git desired-state manifests.",
            structured_content=reconciled,
        )

    def _tool_exasol_profiles_list(self, _: dict[str, Any]) -> dict[str, Any]:
        payload = self._exasol_service().list_profiles()
        profile_names = ", ".join(profile["name"] for profile in payload["profiles"]) if payload["profiles"] else "none"
        return self._tool_result(
            "exasol_profiles_list",
            text=f"Listed {len(payload['profiles'])} Exasol profile(s): {profile_names}.",
            structured_content=payload,
        )

    def _tool_exasol_profile_create_local(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        backend = self._require_choice(
            arguments.get("backend"),
            field_name="backend",
            allowed={"onprem", "saas"},
            tool_name="exasol_profile_create_local",
        )
        credential_mode = self._require_choice(
            arguments.get("credential_mode"),
            field_name="credential_mode",
            allowed={"password", "access_token", "refresh_token", "saas_pat"},
            tool_name="exasol_profile_create_local",
        )
        dsn = self._require_non_empty_string(
            arguments.get("dsn"),
            field_name="dsn",
            tool_name="exasol_profile_create_local",
        )
        user = self._require_non_empty_string(
            arguments.get("user"),
            field_name="user",
            tool_name="exasol_profile_create_local",
        )
        tls_verify = arguments.get("tls_verify", True)
        if not isinstance(tls_verify, bool):
            raise self._field_error("exasol_profile_create_local", "tls_verify", "must be a boolean.")
        secret_value = arguments.get("secret_value")
        if secret_value is not None and not isinstance(secret_value, str):
            raise self._field_error("exasol_profile_create_local", "secret_value", "must be a string.")
        secret_env_var = arguments.get("secret_env_var")
        if secret_env_var is not None and not isinstance(secret_env_var, str):
            raise self._field_error("exasol_profile_create_local", "secret_env_var", "must be a string.")
        # BUG-011 fix: opt-in upsert. Without this, calling the tool twice with the
        # same name silently rewrites the profile (different DSN → silent clobber).
        overwrite = arguments.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise self._field_error("exasol_profile_create_local", "overwrite", "must be a boolean.")

        existing = self._exasol_service().profile_store.profile_exists(name)
        if existing and not overwrite:
            raise DashServerError(
                category="exasol_profile_already_exists",
                summary=(
                    f"Exasol profile {name} already exists. Pass overwrite=true to rewrite it, "
                    "or call exasol_profile_validate to inspect the current configuration."
                ),
                details={"profile": name, "overwrite": False},
                jsonrpc_code=-32013,
                http_status=409,
            )

        payload = self._exasol_service().create_local_profile(
            name=name,
            backend=backend,
            credential_mode=credential_mode,
            dsn=dsn,
            user=user,
            description=arguments.get("description") if isinstance(arguments.get("description"), str) else None,
            tls_verify=tls_verify,
            secret_value=secret_value,
            secret_env_var=secret_env_var,
            statement_timeout_seconds=self._optional_positive_int(
                arguments.get("statement_timeout_seconds"),
                tool_name="exasol_profile_create_local",
                field_name="statement_timeout_seconds",
            ),
            row_limit=self._optional_positive_int(
                arguments.get("row_limit"),
                tool_name="exasol_profile_create_local",
                field_name="row_limit",
            ),
        )
        payload["was_already_present"] = existing
        verb = "Rewrote" if existing else "Created"
        return self._tool_result(
            "exasol_profile_create_local",
            text=f"{verb} local Exasol profile {payload['profile']['name']}.",
            structured_content=payload,
        )

    def _tool_exasol_profile_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = self._exasol_service().validate_profile(self._require_name(arguments))
        validation_state = "passed" if payload["is_valid"] else "failed"
        return self._tool_result(
            "exasol_profile_validate",
            text=f"Exasol profile validation {validation_state} for {payload['profile']['name']}.",
            structured_content=payload,
        )

    def _tool_app_create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start_immediately = arguments.get("start_immediately", True)
        if not isinstance(start_immediately, bool):
            raise self._field_error("app_create", "start_immediately", "must be a boolean.")
        if "files" in arguments:
            raise DashServerError(
                category="tool_validation_error",
                summary="app_create does not accept files. Use app_create_from_files instead.",
                details={
                    "tool": "app_create",
                    "field": "files",
                    "help_resource": "dash://meta/app-create-from-files-schema",
                    "suggested_tools": ["app_create_from_files"],
                },
                jsonrpc_code=-32602,
            )
        bundle = arguments.get("bundle")
        if isinstance(bundle, dict) and "files" in bundle:
            raise DashServerError(
                category="tool_validation_error",
                summary="app_create does not accept files inside bundle. Use app_create_from_files instead.",
                details={
                    "tool": "app_create",
                    "field": "bundle.files",
                    "help_resource": "dash://meta/app-create-from-files-schema",
                    "suggested_tools": ["app_create_from_files"],
                },
                jsonrpc_code=-32602,
            )
        if bundle is None and "name" in arguments:
            bundle = self._bundle_from_top_level_arguments(arguments)
        created = self.runtime_service.create_app(
            bundle, start_immediately=start_immediately
        )
        browser_url = self._absolute_url(created["app"]["route"])
        return self._tool_result(
            "app_create",
            text=f"Created app {created['app']['name']} at {browser_url}.",
            structured_content=created,
        )

    def _tool_app_create_from_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start_immediately = arguments.get("start_immediately", True)
        if not isinstance(start_immediately, bool):
            raise self._field_error("app_create_from_files", "start_immediately", "must be a boolean.")
        files = arguments.get("files")
        if not isinstance(files, list):
            raise self._field_error("app_create_from_files", "files", "must be an array of {path, content} objects.")

        notes: list[str] = []
        template = arguments.get("template")
        if template == "exasol-analytics":
            has_helper = any(
                isinstance(entry, dict) and entry.get("path") == "dash_server_exasol.py"
                for entry in files
            )
            if not has_helper:
                from dash_server.exasol.scaffold import render_exasol_helper_py

                files = [
                    *files,
                    {
                        "path": "dash_server_exasol.py",
                        "content": render_exasol_helper_py(),
                    },
                ]
                notes.append(
                    "Auto-injected dash_server_exasol.py because template=exasol-analytics "
                    "requires the scaffold helper module."
                )

        bundle = {
            "name": self._require_name(arguments),
            "files": files,
        }
        # BUG-010 fix: `data_sources` was advertised in the schema but the handler
        # forgot to forward it, so manifests created via this path always had
        # `data_sources: null` and the bound profile was never wired. Persona 3
        # spent ~10 minutes on a 500 that was actually a control-plane drop.
        for field_name in (
            "title",
            "route",
            "description",
            "template",
            "headline",
            "summary",
            "metrics",
            "data_sources",
        ):
            if field_name in arguments:
                bundle[field_name] = arguments[field_name]
        created = self.runtime_service.create_app(bundle, start_immediately=start_immediately)
        if notes:
            created = {**created, "notes": notes}
        browser_url = self._absolute_url(created["app"]["route"])
        return self._tool_result(
            "app_create_from_files",
            text=f"Created app {created['app']['name']} from files at {browser_url}.",
            structured_content=created,
        )

    def _tool_app_create_exasol_dashboard(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start_immediately = arguments.get("start_immediately", True)
        if not isinstance(start_immediately, bool):
            raise self._field_error("app_create_exasol_dashboard", "start_immediately", "must be a boolean.")
        app_name = self._require_name(arguments)
        profile_name = self._require_non_empty_string(
            arguments.get("profile_name"),
            field_name="profile_name",
            tool_name="app_create_exasol_dashboard",
        )
        profile_validation = self._exasol_service().validate_profile(profile_name)
        if not profile_validation["is_valid"]:
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"Exasol profile {profile_name} did not validate successfully.",
                details={
                    "profile": profile_name,
                    "validation": profile_validation,
                    "help_resource": "dash://exasol/help/connection-modes",
                },
                jsonrpc_code=-32012,
            )
        title_arg = arguments.get("title")
        route_arg = arguments.get("route")
        description_arg = arguments.get("description")
        pattern_arg = arguments.get("pattern")
        bundle = self._exasol_service().build_dashboard_bundle(
            app_name=app_name,
            profile_name=profile_name,
            title=title_arg if isinstance(title_arg, str) else None,
            route=route_arg if isinstance(route_arg, str) else None,
            description=description_arg if isinstance(description_arg, str) else None,
            pattern=pattern_arg if isinstance(pattern_arg, str) else "analytics-hub",
        )
        created = self.runtime_service.create_app(bundle, start_immediately=start_immediately)
        browser_url = self._absolute_url(created["app"]["route"])
        # BUG-008 fix: tell the caller when they picked a demo-only pattern so they
        # don't ship `SELECT 'Mon' AS LABEL, 120 AS "VALUE" FROM DUAL` to production
        # thinking it queries their schema.
        notes: list[str] = []
        chosen_pattern = pattern_arg if isinstance(pattern_arg, str) else "analytics-hub"
        if chosen_pattern in {"kpi-trend", "overview"}:
            notes.append(
                f"Pattern `{chosen_pattern}` ships `SELECT … FROM DUAL` placeholder SQL — "
                "no catalog-backed queries. For a schema-bound scaffold, call "
                "`app_scaffold_from_schema` instead. See `dash://exasol/help/dashboard-patterns`."
            )
        structured: dict[str, Any] = {
            **created,
            "exasol_profile": profile_validation["profile"],
            "pattern": chosen_pattern,
        }
        if notes:
            structured["notes"] = notes
        return self._tool_result(
            "app_create_exasol_dashboard",
            text=f"Created Exasol dashboard {created['app']['name']} at {browser_url}.",
            structured_content=structured,
        )

    def _tool_app_scaffold_from_schema(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start_immediately = arguments.get("start_immediately", True)
        if not isinstance(start_immediately, bool):
            raise self._field_error("app_scaffold_from_schema", "start_immediately", "must be a boolean.")
        schema_name = arguments.get("schema_name")
        if schema_name is not None and (not isinstance(schema_name, str) or not schema_name.strip()):
            raise self._field_error("app_scaffold_from_schema", "schema_name", "must be a non-empty string.")
        table_name = arguments.get("table_name")
        if table_name is not None and (not isinstance(table_name, str) or not table_name.strip()):
            raise self._field_error("app_scaffold_from_schema", "table_name", "must be a non-empty string.")

        app_name = self._require_name(arguments)
        profile_name = self._require_non_empty_string(
            arguments.get("profile_name"),
            field_name="profile_name",
            tool_name="app_scaffold_from_schema",
        )
        profile_validation = self._exasol_service().validate_profile(profile_name)
        if not profile_validation["is_valid"]:
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"Exasol profile {profile_name} did not validate successfully.",
                details={
                    "profile": profile_name,
                    "validation": profile_validation,
                    "help_resource": "dash://exasol/help/connection-modes",
                },
                jsonrpc_code=-32012,
            )
        bundle = self._exasol_service().build_schema_scaffold_bundle(
            app_name=app_name,
            profile_name=profile_name,
            schema_name=schema_name.strip() if isinstance(schema_name, str) else None,
            table_name=table_name.strip() if isinstance(table_name, str) else None,
            title=arguments.get("title") if isinstance(arguments.get("title"), str) else None,
            route=arguments.get("route") if isinstance(arguments.get("route"), str) else None,
            description=arguments.get("description") if isinstance(arguments.get("description"), str) else None,
        )
        created = self.runtime_service.create_app(bundle, start_immediately=start_immediately)
        browser_url = self._absolute_url(created["app"]["route"])
        return self._tool_result(
            "app_scaffold_from_schema",
            text=f"Created schema-tailored Exasol scaffold {created['app']['name']} at {browser_url}.",
            structured_content={
                **created,
                "exasol_profile": profile_validation["profile"],
                "schema_blueprint": bundle["schema_blueprint"],
            },
        )

    def _tool_app_build(self, arguments: dict[str, Any]) -> dict[str, Any]:
        force_clean = arguments.get("force_clean", False)
        if not isinstance(force_clean, bool):
            raise self._field_error("app_build", "force_clean", "must be a boolean.")
        built = self.runtime_service.build_revision(
            self._require_name(arguments),
            arguments.get("bundle"),
            force_clean=force_clean,
        )
        preflight = built.get("preflight")
        if isinstance(preflight, dict) and preflight.get("status") != "passed":
            revision = built.get("revision", {})
            revision_number = revision.get("revision_number")
            exc = DashServerError(
                category="artifact_preflight_failed",
                summary=(
                    f"Built revision {revision_number} but artifact preflight failed; "
                    "fix the runtime issue before promoting it live."
                ),
                details={
                    "app": built["app"]["name"],
                    "revision_number": revision_number,
                    "preflight": preflight,
                },
                jsonrpc_code=-32010,
                http_status=409,
            )
            return self._tool_error_result(
                "app_build",
                exc,
                extra_payload=built,
            )
        return self._tool_result(
            "app_build",
            text=f"Built revision {built['revision']['revision_number']} for app {built['app']['name']}.",
            structured_content=built,
        )

    def _tool_app_read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise self._field_error("app_read_file", "path", "must be a non-empty string.")
        payload = self.runtime_service.read_workspace_file(
            self._require_name(arguments),
            path,
        )
        return self._tool_result(
            "app_read_file",
            text=f"Read draft file {path} for app {payload['app']['name']}.",
            structured_content=payload,
        )

    def _tool_app_diff_draft_vs_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        revision_number = arguments.get("revision_number")
        if revision_number is not None and (
            not isinstance(revision_number, int) or isinstance(revision_number, bool) or revision_number <= 0
        ):
            raise self._field_error(
                "app_diff_draft_vs_artifact",
                "revision_number",
                "must be a positive integer.",
            )
        payload = self.runtime_service.diff_workspace_against_artifact(
            self._require_name(arguments),
            revision_number=revision_number,
        )
        target_text = (
            f"revision {revision_number}"
            if isinstance(revision_number, int)
            else f"latest built artifact revision {payload['artifact']['revision']['revision_number']}"
        )
        return self._tool_result(
            "app_diff_draft_vs_artifact",
            text=f"Compared the draft workspace for app {payload['app']['name']} against {target_text}.",
            structured_content=payload,
        )

    def _tool_app_deploy_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        deployment_target = arguments.get("deployment_target", "live")
        if deployment_target not in {"live", "preview"}:
            raise self._field_error("app_deploy_draft", "deployment_target", "must be live or preview.")
        force_clean = arguments.get("force_clean", False)
        if not isinstance(force_clean, bool):
            raise self._field_error(
                "app_deploy_draft",
                "force_clean",
                "must be a boolean.",
            )
        auto_rollback_on_health_failure = arguments.get("auto_rollback_on_health_failure", False)
        if not isinstance(auto_rollback_on_health_failure, bool):
            raise self._field_error(
                "app_deploy_draft",
                "auto_rollback_on_health_failure",
                "must be a boolean.",
            )
        validation = self.runtime_service.validate_workspace(name, force_clean=force_clean)
        if not validation["validation"]["is_valid"]:
            diagnostics = self.runtime_service.collect_diagnostics(name)
            exc = DashServerError(
                category="workspace_validation_error",
                summary="Draft validation failed; fix the draft before deployment.",
                details={
                    "app": name,
                    "validation": validation["validation"],
                    "diagnostics": diagnostics,
                    "force_clean": force_clean,
                },
                jsonrpc_code=-32007,
                http_status=409,
            )
            return self._tool_error_result(
                "app_deploy_draft",
                exc,
                extra_payload={
                    "force_clean": force_clean,
                    "validation": validation,
                    "diagnostics": diagnostics,
                },
            )

        try:
            built = self.runtime_service.build_revision(name)
            preflight = built.get("preflight")
            if (
                deployment_target == "live"
                and isinstance(preflight, dict)
                and preflight.get("status") != "passed"
            ):
                exc = DashServerError(
                    category="artifact_preflight_failed",
                    summary=(
                        f"Built revision {built['revision']['revision_number']} for app {name}, "
                        "but artifact preflight failed so live promotion was blocked."
                    ),
                    details={
                        "app": name,
                        "deployment_target": deployment_target,
                        "revision_number": built["revision"]["revision_number"],
                        "preflight": preflight,
                    },
                    jsonrpc_code=-32010,
                    http_status=409,
                )
                return self._tool_error_result(
                    "app_deploy_draft",
                    exc,
                    extra_payload={
                        "force_clean": force_clean,
                        "deployment_target": deployment_target,
                        "validation": validation,
                        "build": built,
                    },
                )
            if deployment_target == "preview":
                deployed = self.runtime_service.start_preview(
                    name,
                    built["revision"]["revision_number"],
                )
            else:
                deployed = self.runtime_service.promote_revision(
                    name,
                    built["revision"]["revision_number"],
                )
            health = self.runtime_service.run_healthcheck(name, target=deployment_target)
        except DashServerError as exc:
            diagnostics = self.runtime_service.collect_diagnostics(name)
            return self._tool_error_result(
                "app_deploy_draft",
                exc,
                extra_payload={"force_clean": force_clean, "diagnostics": diagnostics},
            )

        if (
            deployment_target == "live"
            and auto_rollback_on_health_failure
            and health["health"]["status"] != "healthy"
        ):
            rollback = None
            rollback_health = None
            try:
                rollback = self.runtime_service.rollback(name)
                rollback_health = self.runtime_service.run_healthcheck(name, target="live", record=False)
            except DashServerError as rollback_exc:
                return self._tool_error_result(
                    "app_deploy_draft",
                    rollback_exc,
                    extra_payload={
                        "force_clean": force_clean,
                        "deployment_target": deployment_target,
                        "validation": validation,
                        "build": built,
                        "deployment": deployed,
                        "health": health,
                        "rollback": rollback,
                        "rollback_health": rollback_health,
                    },
                )

            unhealthy_error = DashServerError(
                category="deployment_healthcheck_failed",
                summary=f"Live deployment for app {name} failed health checks and was rolled back.",
                details={
                    "app": name,
                    "deployment_target": deployment_target,
                    "health_status": health["health"]["status"],
                    "auto_rollback_on_health_failure": True,
                },
                jsonrpc_code=-32010,
                http_status=409,
            )
            return self._tool_error_result(
                "app_deploy_draft",
                unhealthy_error,
                extra_payload={
                    "force_clean": force_clean,
                    "deployment_target": deployment_target,
                    "validation": validation,
                    "build": built,
                    "deployment": deployed,
                    "health": health,
                    "rollback": rollback,
                    "rollback_health": rollback_health,
                },
            )

        return self._tool_result(
            "app_deploy_draft",
            text=(
                f"Deployed draft preview for app {name} as revision {built['revision']['revision_number']} "
                f"at {self._absolute_url(deployed['app'].get('preview_path') or '')}."
                if deployment_target == "preview"
                else (
                    f"Deployed draft for app {name} as revision "
                    f"{built['revision']['revision_number']} at {self._absolute_url(deployed['app']['route'])}."
                )
            ),
            structured_content={
                "force_clean": force_clean,
                "deployment_target": deployment_target,
                "app": deployed["app"],
                "validation": validation,
                "build": built,
                "deployment": deployed,
                "health": health,
            },
        )

    def _tool_app_start_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        previewed = self.runtime_service.start_preview(
            self._require_name(arguments),
            self._require_revision_number(arguments, "revision_number"),
        )
        preview_path = previewed["app"].get("preview_path")
        preview_url = self._absolute_url(preview_path) if isinstance(preview_path, str) else preview_path
        return self._tool_result(
            "app_start_preview",
            text=f"Started preview for app {previewed['app']['name']} at {preview_url}.",
            structured_content=previewed,
        )

    def _tool_app_promote_revision(self, arguments: dict[str, Any]) -> dict[str, Any]:
        promoted = self.runtime_service.promote_revision(
            self._require_name(arguments),
            self._require_revision_number(arguments, "revision_number"),
        )
        return self._tool_result(
            "app_promote_revision",
            text=(
                f"Promoted revision {promoted['current_revision']['revision_number']} for app "
                f"{promoted['app']['name']} to {self._absolute_url(promoted['app']['route'])}."
            ),
            structured_content=promoted,
        )

    def _tool_app_rollback(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rolled_back = self.runtime_service.rollback(self._require_name(arguments))
        return self._tool_result(
            "app_rollback",
            text=f"Rolled back app {rolled_back['app']['name']} to revision {rolled_back['current_revision']['revision_number']}.",
            structured_content=rolled_back,
        )

    def _tool_app_put_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        files = arguments.get("files")
        if not isinstance(files, list):
            raise DashServerError(
                category="tool_validation_error",
                summary="`files` must be a list.",
                details={"received_type": type(files).__name__},
                jsonrpc_code=-32602,
            )
        updated = self.runtime_service.put_files(
            self._require_name(arguments),
            files,
        )
        return self._tool_result(
            "app_put_files",
            text=f"Updated draft files for app {updated['app']['name']}.",
            structured_content=updated,
        )

    def _tool_app_patch_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        patched = self.runtime_service.patch_file(
            self._require_name(arguments),
            self._require_string(arguments.get("path"), "path"),
            self._require_string(arguments.get("search"), "search"),
            self._require_string(arguments.get("replace"), "replace", allow_empty=True),
            replace_all=bool(arguments.get("replace_all", False)),
        )
        return self._tool_result(
            "app_patch_file",
            text=f"Patched file for app {patched['app']['name']}.",
            structured_content=patched,
        )

    def _tool_app_delete_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        deleted = self.runtime_service.delete_file(
            self._require_name(arguments),
            self._require_string(arguments.get("path"), "path"),
        )
        return self._tool_result(
            "app_delete_file",
            text=f"Deleted draft file for app {deleted['app']['name']}.",
            structured_content=deleted,
        )

    def _tool_app_validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validation = self.runtime_service.validate_workspace(self._require_name(arguments))
        report = validation["validation"]
        valid = bool(report.get("is_valid"))
        summary = _validation_summary(report)
        # BUG-014 fix: top-level pass/fail signal so agents don't have to walk the
        # nested `validation.*` payload.
        validation["validation_summary"] = summary
        status = "passed" if valid else "failed"
        details = self._validation_summary_lines(report)
        text = f"Validation {status} for app {validation['app']['name']}."
        # BUG-015 fix: surface warning count in the visible text so an agent following
        # `guidance.next_step` doesn't ship warnings unnoticed.
        if summary["warning_count"]:
            text += (
                f"\nWarnings: {summary['warning_count']}"
                " (see structured payload `validation` for full list)."
            )
        if details:
            text = text + "\n" + "\n".join(details)
        # Build the structured response the same way for pass and fail so the
        # validate-specific guidance (cross-module symbols, etc.) keeps applying.
        # BUG-013 fix is just the envelope flip below — clients that route on
        # `isError` now correctly classify the failure case.
        response = self._tool_result(
            "app_validate",
            text=text,
            structured_content=validation,
        )
        if not valid:
            response["isError"] = True
            first_detail = details[0] if details else "Validation failed."
            response["structuredContent"] = {
                **response["structuredContent"],
                "error": {
                    "tool": "app_validate",
                    "category": "workspace_validation_error",
                    "summary": f"Validation failed for app {validation['app']['name']}. {first_detail}",
                    "details": {"app": validation["app"]["name"], "validation_summary": summary},
                },
            }
        return response

    def _tool_app_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        started = self.runtime_service.start_app(self._require_name(arguments))
        return self._tool_result(
            "app_start",
            text=f"Started app {started['app']['name']}.",
            structured_content=started,
        )

    def _tool_app_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        stopped = self.runtime_service.stop_app(self._require_name(arguments))
        return self._tool_result(
            "app_stop",
            text=f"Stopped app {stopped['app']['name']}.",
            structured_content=stopped,
        )

    def _tool_app_restart(self, arguments: dict[str, Any]) -> dict[str, Any]:
        restarted = self.runtime_service.restart_app(self._require_name(arguments))
        return self._tool_result(
            "app_restart",
            text=f"Restarted app {restarted['app']['name']}.",
            structured_content=restarted,
        )

    def _tool_app_get_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        status = self.runtime_service.get_app_status(self._require_name(arguments))
        return self._tool_result(
            "app_get_status",
            text=f"Fetched status for app {status['app']['name']}.",
            structured_content=status,
        )

    def _tool_app_collect_diagnostics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        diagnostics = self.runtime_service.collect_diagnostics(self._require_name(arguments))
        return self._tool_result(
            "app_collect_diagnostics",
            text=f"Collected diagnostics for app {diagnostics['app']['name']}.",
            structured_content=diagnostics,
        )

    def _tool_app_inspect_traceback(self, arguments: dict[str, Any]) -> dict[str, Any]:
        traceback_text = arguments.get("traceback_text")
        if traceback_text is not None and not isinstance(traceback_text, str):
            raise self._field_error("app_inspect_traceback", "traceback_text", "must be a string.")
        inspected = self.runtime_service.inspect_traceback(
            self._require_name(arguments),
            traceback_text,
        )
        return self._tool_result(
            "app_inspect_traceback",
            text=f"Inspected traceback for app {inspected['app']['name']}.",
            structured_content=inspected,
        )

    def _tool_app_tail_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        channel = arguments.get("channel", "latest")
        if not isinstance(channel, str) or not channel:
            raise self._field_error("app_tail_logs", "channel", "must be a non-empty string.")
        if channel not in self._log_channels:
            raise self._field_error(
                "app_tail_logs",
                "channel",
                f"must be one of: {', '.join(self._log_channels)}.",
            )
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise self._field_error("app_tail_logs", "limit", "must be a positive integer.")
        logs = self.runtime_service.tail_logs(self._require_name(arguments), channel=channel, limit=limit)
        return self._tool_result(
            "app_tail_logs",
            text=f"Fetched {len(logs['logs']['entries'])} log entries for app {logs['app']['name']}.",
            structured_content=logs,
        )

    def _tool_app_run_healthcheck(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = arguments.get("target", "live")
        if target not in {"live", "preview"}:
            raise self._field_error("app_run_healthcheck", "target", "must be live or preview.")
        health = self.runtime_service.run_healthcheck(self._require_name(arguments), target=target)
        return self._tool_result(
            "app_run_healthcheck",
            text=f"Ran {target} health check for app {health['app']['name']}.",
            structured_content=health,
        )

    def _tool_app_share_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        payload = self._share_payload(name)
        return self._tool_result(
            "app_share_get",
            text=f"Returned sharing policy and {len(payload['grants'])} active grant(s) for app {name}.",
            structured_content=payload,
        )

    def _tool_app_share_grant(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        principal_type = self._require_choice(
            arguments.get("principal_type"),
            field_name="principal_type",
            allowed={"user", "group", "domain", "organization", "public"},
            tool_name="app_share_grant",
        )
        principal_id = self._require_non_empty_string(
            arguments.get("principal_id"),
            field_name="principal_id",
            tool_name="app_share_grant",
        )
        role = self._require_choice(
            arguments.get("role"),
            field_name="role",
            allowed={"viewer", "preview_viewer", "editor", "owner"},
            tool_name="app_share_grant",
        )
        scope = self._require_choice(
            arguments.get("scope", "live"),
            field_name="scope",
            allowed={"live", "preview", "manage", "all"},
            tool_name="app_share_grant",
        )
        if principal_type == "domain":
            principal_id = principal_id.lower()
        if principal_type == "group":
            self.runtime_service.registry.upsert_group(
                external_id=principal_id,
                display_name=arguments.get("display_name") if isinstance(arguments.get("display_name"), str) else None,
                source="local",
            )
        app = self._require_existing_app(name, tool_name="app_share_grant")
        grant = self.runtime_service.registry.grant_app_access(
            name,
            principal_type=principal_type,
            principal_id=principal_id,
            role=role,
            scope=scope,
            created_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.append_event(
            name,
            "share_grant_created",
            data={
                "grant_id": grant["id"],
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": role,
                "scope": scope,
            },
        )
        return self._tool_result(
            "app_share_grant",
            text=f"Granted {role} {scope} access on app {name} to {principal_type}:{principal_id}.",
            structured_content={"app": app.to_dict(), "grant": grant},
        )

    def _tool_app_share_revoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        grant_id = arguments.get("grant_id")
        if grant_id is not None and (not isinstance(grant_id, int) or isinstance(grant_id, bool) or grant_id <= 0):
            raise self._field_error("app_share_revoke", "grant_id", "must be a positive integer.")
        principal_type = arguments.get("principal_type")
        principal_id = arguments.get("principal_id")
        if principal_type is not None:
            principal_type = self._require_choice(
                principal_type,
                field_name="principal_type",
                allowed={"user", "group", "domain", "organization", "public"},
                tool_name="app_share_revoke",
            )
        if principal_id is not None:
            principal_id = self._require_non_empty_string(
                principal_id,
                field_name="principal_id",
                tool_name="app_share_revoke",
            )
        if grant_id is None and (principal_type is None or principal_id is None):
            raise self._field_error(
                "app_share_revoke",
                "grant_id",
                "or principal_type plus principal_id is required.",
            )
        app = self._require_existing_app(name, tool_name="app_share_revoke")
        revoked = self.runtime_service.registry.revoke_app_access(
            name,
            grant_id=grant_id,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        for grant in revoked:
            self.runtime_service.registry.append_event(
                name,
                "share_grant_revoked",
                data={
                    "grant_id": grant["id"],
                    "principal_type": grant["principal_type"],
                    "principal_id": grant["principal_id"],
                },
            )
        return self._tool_result(
            "app_share_revoke",
            text=f"Revoked {len(revoked)} sharing grant(s) for app {name}.",
            structured_content={"app": app.to_dict(), "revoked_grants": revoked},
        )

    def _tool_app_share_set_link_scope(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        link_scope = self._require_choice(
            arguments.get("link_scope"),
            field_name="link_scope",
            allowed={"restricted", "organization", "domain", "anyone_with_link", "public"},
            tool_name="app_share_set_link_scope",
        )
        default_link_role = self._require_choice(
            arguments.get("default_link_role", "viewer"),
            field_name="default_link_role",
            allowed={"viewer", "preview_viewer"},
            tool_name="app_share_set_link_scope",
        )
        public_catalog_visible = arguments.get("public_catalog_visible", link_scope == "public")
        external_sharing_enabled = arguments.get("external_sharing_enabled", False)
        allow_preview_link = arguments.get("allow_preview_link", False)
        for field_name, value in (
            ("public_catalog_visible", public_catalog_visible),
            ("external_sharing_enabled", external_sharing_enabled),
            ("allow_preview_link", allow_preview_link),
        ):
            if not isinstance(value, bool):
                raise self._field_error("app_share_set_link_scope", field_name, "must be a boolean.")
        app = self._require_existing_app(name, tool_name="app_share_set_link_scope")
        policy = self.runtime_service.registry.upsert_share_policy(
            name,
            link_scope=link_scope,
            default_link_role=default_link_role,
            allow_preview_link=allow_preview_link,
            public_catalog_visible=public_catalog_visible,
            external_sharing_enabled=external_sharing_enabled,
            updated_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.append_event(
            name,
            "share_policy_updated",
            data={
                "link_scope": link_scope,
                "default_link_role": default_link_role,
                "public_catalog_visible": public_catalog_visible,
                "external_sharing_enabled": external_sharing_enabled,
                "allow_preview_link": allow_preview_link,
            },
        )
        return self._tool_result(
            "app_share_set_link_scope",
            text=f"Updated sharing link scope for app {name} to {link_scope}.",
            structured_content={"app": app.to_dict(), "share_policy": policy},
        )

    def _tool_app_share_explain_access(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        target = self._require_choice(
            arguments.get("target", "live"),
            field_name="target",
            allowed={"live", "preview"},
            tool_name="app_share_explain_access",
        )
        principal = self._principal_for_access_explanation(arguments)
        auth_context = AuthContext(
            mode="hosted",
            auth_enabled=True,
            provider="explain",
            principal=principal,
        )
        app = self._require_existing_app(name, tool_name="app_share_explain_access")
        if target == "preview":
            preview_path = self.runtime_service.preview_path(name, app.preview_revision_number or 0)
            route_target = self._authorization_service().classify_path(preview_path, mount_prefix=preview_path)
        else:
            route_target = self._authorization_service().classify_path(app.route, mount_prefix=app.route)
        decision = self._authorization_service().authorize(auth_context, route_target)
        return self._tool_result(
            "app_share_explain_access",
            text=f"Access for {principal.principal_id} on app {name} {target}: {decision.reason}.",
            structured_content={
                "app": app.to_dict(),
                "principal": principal.to_dict(),
                "target": target,
                "decision": decision.to_dict(),
            },
        )

    def _tool_app_share_create_one_time_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        role = self._require_choice(
            arguments.get("role", "viewer"),
            field_name="role",
            allowed={"viewer", "preview_viewer"},
            tool_name="app_share_create_one_time_link",
        )
        scope = self._require_choice(
            arguments.get("scope", "live"),
            field_name="scope",
            allowed={"live", "preview"},
            tool_name="app_share_create_one_time_link",
        )
        ttl_hours = arguments.get("ttl_hours", 168)
        if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool) or ttl_hours <= 0:
            raise self._field_error("app_share_create_one_time_link", "ttl_hours", "must be a positive integer.")
        recipient_email = arguments.get("recipient_email")
        if recipient_email is not None and not isinstance(recipient_email, str):
            raise self._field_error("app_share_create_one_time_link", "recipient_email", "must be a string.")
        recipient_note = arguments.get("recipient_note")
        if recipient_note is not None and not isinstance(recipient_note, str):
            raise self._field_error("app_share_create_one_time_link", "recipient_note", "must be a string.")
        app = self._require_existing_app(name, tool_name="app_share_create_one_time_link")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
        link = self.runtime_service.registry.create_share_link(
            name,
            token_hash=token_hash,
            scope=scope,
            role=role,
            expires_at=expires_at,
            max_uses=1,
            recipient_email=recipient_email,
            recipient_note=recipient_note,
            created_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.upsert_share_policy(
            name,
            link_scope="anyone_with_link",
            default_link_role=role,
            allow_preview_link=scope == "preview",
            public_catalog_visible=False,
            external_sharing_enabled=True,
            updated_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.append_event(
            name,
            "one_time_link_created",
            data={
                "link_id": link["id"],
                "scope": scope,
                "role": role,
                "expires_at": expires_at,
                "recipient_email": recipient_email,
            },
        )
        redeem_path = f"/share/links/{raw_token}"
        payload = {
            "app": app.to_dict(),
            "one_time_link": {
                **(self._sanitize_share_link(link) or {}),
                "url": self._absolute_url(redeem_path),
                "raw_token": raw_token,
                "display_once": True,
            },
            "guidance_note": "The raw token is returned only in this response. Store only the URL with the intended recipient.",
        }
        return self._tool_result(
            "app_share_create_one_time_link",
            text=f"Created a one-time {scope} sharing link for app {name}. The raw token is shown only once.",
            structured_content=payload,
        )

    def _tool_app_share_revoke_one_time_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        link_id = arguments.get("link_id")
        if not isinstance(link_id, int) or isinstance(link_id, bool) or link_id <= 0:
            raise self._field_error("app_share_revoke_one_time_link", "link_id", "must be a positive integer.")
        app = self._require_existing_app(name, tool_name="app_share_revoke_one_time_link")
        link = self.runtime_service.registry.get_share_link(link_id)
        if link is None or link["app_name"] != name:
            raise DashServerError(
                category="share_link_not_found",
                summary=f"One-time sharing link {link_id} does not exist for app {name}.",
                details={"tool": "app_share_revoke_one_time_link", "app": name, "link_id": link_id},
                jsonrpc_code=-32602,
                http_status=404,
            )
        revoked = self.runtime_service.registry.revoke_share_link(link_id)
        self.runtime_service.registry.append_event(
            name,
            "one_time_link_revoked",
            data={"link_id": link_id},
        )
        return self._tool_result(
            "app_share_revoke_one_time_link",
            text=f"Revoked one-time sharing link {link_id} for app {name}.",
            structured_content={"app": app.to_dict(), "one_time_link": self._sanitize_share_link(revoked)},
        )

    def _tool_app_invite_external_user(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        recipient_email = self._normalize_email_argument(
            arguments.get("recipient_email"),
            tool_name="app_invite_external_user",
        )
        role = self._require_choice(
            arguments.get("role", "viewer"),
            field_name="role",
            allowed={"viewer", "preview_viewer"},
            tool_name="app_invite_external_user",
        )
        scope = self._require_choice(
            arguments.get("scope", "live"),
            field_name="scope",
            allowed={"live", "preview"},
            tool_name="app_invite_external_user",
        )
        ttl_hours = arguments.get("ttl_hours", 168)
        if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool) or ttl_hours <= 0:
            raise self._field_error("app_invite_external_user", "ttl_hours", "must be a positive integer.")
        message = arguments.get("message")
        if message is not None and not isinstance(message, str):
            raise self._field_error("app_invite_external_user", "message", "must be a string.")
        app = self._require_existing_app(name, tool_name="app_invite_external_user")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
        invitation = self.runtime_service.registry.create_invitation(
            name,
            token_hash=token_hash,
            recipient_email=recipient_email,
            email_normalized=recipient_email.lower(),
            scope=scope,
            role=role,
            message=message,
            expires_at=expires_at,
            delivery_status="pending_manual_delivery",
            created_by_principal_id=self._current_principal_id(),
        )
        existing_policy = self.runtime_service.registry.get_share_policy(name)
        self.runtime_service.registry.upsert_share_policy(
            name,
            link_scope=existing_policy["link_scope"],
            default_link_role=existing_policy["default_link_role"],
            allow_preview_link=bool(existing_policy["allow_preview_link"] or scope == "preview"),
            public_catalog_visible=bool(existing_policy["public_catalog_visible"]),
            external_sharing_enabled=True,
            updated_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_created",
            data={
                "invitation_id": invitation["id"],
                "recipient_email": recipient_email,
                "scope": scope,
                "role": role,
                "expires_at": expires_at,
                "delivery_status": invitation["delivery_status"],
            },
        )
        accept_path = f"/share/invitations/{raw_token}"
        accept_url = self._absolute_url(accept_path)
        delivery_result = self._email_sender().send_invitation(
            app_title=app.title,
            recipient_email=recipient_email,
            accept_url=accept_url,
            role=role,
            scope=scope,
            expires_at=expires_at,
            inviter_display_name=self._current_principal().display_name,
            message=message,
        )
        invitation = self.runtime_service.registry.update_invitation_delivery(
            int(invitation["id"]),
            delivery_status=delivery_result.status,
            delivery_provider=delivery_result.provider,
            delivery_message_id=delivery_result.message_id,
            delivery_error=delivery_result.error,
        ) or invitation
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_delivery_updated",
            data={
                "invitation_id": invitation["id"],
                "delivery_status": invitation["delivery_status"],
                "delivery_provider": invitation["delivery_provider"],
                "delivery_error": invitation["delivery_error"],
            },
        )
        payload = {
            "app": app.to_dict(),
            "invitation": {
                **(self._sanitize_invitation(invitation) or {}),
                "accept_url": accept_url,
                "raw_token": raw_token,
                "display_once": True,
            },
            "delivery": {
                "status": invitation["delivery_status"],
                "provider": invitation["delivery_provider"],
                "message_id": invitation["delivery_message_id"],
                "error": invitation["delivery_error"],
                "mode": "manual" if invitation["delivery_status"] == "pending_manual_delivery" else "email",
                "recipient_email": recipient_email,
                "note": self._invitation_delivery_note(invitation),
            },
            "guidance_note": "The raw invitation token is returned only in this response. Only a token hash is stored.",
        }
        return self._tool_result(
            "app_invite_external_user",
            text=f"Created an invitation for {recipient_email} to access app {name}.",
            structured_content=payload,
        )

    def _tool_app_revoke_external_invitation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        invitation_id = arguments.get("invitation_id")
        if not isinstance(invitation_id, int) or isinstance(invitation_id, bool) or invitation_id <= 0:
            raise self._field_error("app_revoke_external_invitation", "invitation_id", "must be a positive integer.")
        app = self._require_existing_app(name, tool_name="app_revoke_external_invitation")
        invitation = self.runtime_service.registry.get_invitation(invitation_id)
        if invitation is None or invitation["app_name"] != name:
            raise DashServerError(
                category="invitation_not_found",
                summary=f"External invitation {invitation_id} does not exist for app {name}.",
                details={"tool": "app_revoke_external_invitation", "app": name, "invitation_id": invitation_id},
                jsonrpc_code=-32602,
                http_status=404,
            )
        revoked = self.runtime_service.registry.revoke_invitation(invitation_id)
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_revoked",
            data={
                "invitation_id": invitation_id,
                "grant_id": invitation.get("grant_id"),
                "recipient_email": invitation.get("recipient_email"),
            },
        )
        return self._tool_result(
            "app_revoke_external_invitation",
            text=f"Revoked external invitation {invitation_id} for app {name}.",
            structured_content={"app": app.to_dict(), "invitation": self._sanitize_invitation(revoked)},
        )

    # ---- Phase 4f: runtime / environment tools -------------------------------------

    def _tool_app_runtime_workers_list(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        payload = self._workers_payload()
        text = (
            f"Listed {payload['worker_count']} worker(s); rss_bytes_total={payload['rss_bytes_total']}."
        )
        return self._tool_result(
            "app_runtime_workers_list", text=text, structured_content=payload
        )

    def _tool_app_runtime_workers_restart(self, arguments: dict[str, Any]) -> dict[str, Any]:
        manager = self._worker_manager_or_error()
        mount_path = arguments.get("mount_path")
        if not isinstance(mount_path, str) or not mount_path.startswith("/"):
            raise DashServerError(
                category="tool_validation_error",
                summary="mount_path must be an absolute path starting with /.",
                details={"tool": "app_runtime_workers_restart", "field": "mount_path"},
                jsonrpc_code=-32602,
            )
        # The Phase 3.5b re-spawn path: stop preserves the spec, ensure_running re-spawns it.
        manager.stop(mount_path, idle=True)
        record = manager.ensure_running(mount_path)
        if record is None:
            raise DashServerError(
                category="runtime_mount_error",
                summary=f"Failed to restart worker for {mount_path} — no persisted spec on disk.",
                details={"mount_path": mount_path},
                jsonrpc_code=-32008,
            )
        return self._tool_result(
            "app_runtime_workers_restart",
            text=f"Restarted worker for {mount_path}; new pid={record.pid}.",
            structured_content={
                "mount_path": mount_path,
                "pid": record.pid,
                "port": record.port,
                "status": record.status,
            },
        )

    def _tool_app_environment_invalidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        env_service = self._dep_env_service_or_error()
        env_id = arguments.get("environment_id")
        if not isinstance(env_id, str) or not env_id:
            raise DashServerError(
                category="tool_validation_error",
                summary="environment_id must be a non-empty string.",
                details={"tool": "app_environment_invalidate", "field": "environment_id"},
                jsonrpc_code=-32602,
            )
        invalidated = env_service.invalidate(env_id)
        return self._tool_result(
            "app_environment_invalidate",
            text=(
                f"Marked environment {env_id} for removal on next GC pass."
                if invalidated
                else f"No environment found for id {env_id}; nothing invalidated."
            ),
            structured_content={"environment_id": env_id, "invalidated": invalidated},
        )

    def _tool_app_acknowledge_data_layer_errors(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Reset the `data_layer` healthcheck probe by acknowledging current errors.

        Use this after fixing SQL in-place (without promoting a new revision) so the
        probe and `dash://apps/{name}/errors` stop reporting stale failures. The
        underlying ledger is preserved — operators can still inspect history through
        the resource — but the probe and the canonical diagnostic tool both filter
        past the watermark.
        """

        name = self._require_name(arguments)
        # Confirm the app exists before touching the diagnostics ledger.
        self.runtime_service.registry.get_app(name)
        watermark = self.runtime_service.diagnostics_service.acknowledge_data_layer_errors(name)
        return self._tool_result(
            "app_acknowledge_data_layer_errors",
            text=(
                f"Acknowledged data-layer errors for {name}. The data_layer probe "
                f"will report passed until a new error is recorded after "
                f"{watermark['acknowledged_until']}."
            ),
            structured_content={"app": name, "watermark": watermark},
        )

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

    def _worker_manager_or_error(self) -> Any:
        if not has_request_context():
            raise DashServerError(
                category="runtime_state_error",
                summary="Runtime worker tools require a request context.",
                details={},
                jsonrpc_code=-32603,
            )
        manager = current_app.extensions.get("worker_manager")
        if manager is None:
            raise DashServerError(
                category="runtime_mode_error",
                summary=(
                    "No worker manager is configured. Set APP_RUNTIME_MODE=isolated to "
                    "enable out-of-process workers."
                ),
                details={"runtime_mode": current_app.config.get("APP_RUNTIME_MODE")},
                jsonrpc_code=-32603,
            )
        return manager

    def _dep_env_service_or_error(self) -> Any:
        if not has_request_context():
            raise DashServerError(
                category="runtime_state_error",
                summary="Environment tools require a request context.",
                details={},
                jsonrpc_code=-32603,
            )
        service = current_app.extensions.get("dependency_environment_service")
        if service is None:
            raise DashServerError(
                category="runtime_mode_error",
                summary=(
                    "No per-app dependency-environment service is configured. Set "
                    "APP_DEPENDENCY_ISOLATION=per_app to enable per-app envs."
                ),
                details={
                    "app_dependency_isolation": current_app.config.get("APP_DEPENDENCY_ISOLATION")
                },
                jsonrpc_code=-32603,
            )
        return service

    def _tool_result(
        self,
        tool_name: str,
        text: str,
        structured_content: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._attach_absolute_urls(structured_content)
        payload = self._attach_guidance(
            tool_name,
            payload,
            is_error=False,
        )
        summary_text = self._append_guidance_to_text(text, payload.get("guidance"))
        visible_text = self._render_visible_tool_text(summary_text, payload)
        return {
            "content": [{"type": "text", "text": visible_text}],
            "structuredContent": payload,
            "isError": False,
        }

    def _tool_error_result(
        self,
        tool_name: str,
        exc: DashServerError,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lines = [f"{tool_name} failed: {exc.summary}"]
        lines.extend(self._diagnostic_lines_for_error(exc))
        if exc.details.get("help_resource"):
            lines.append(f"Help resource: {exc.details['help_resource']}")
        payload = {
            "error": {
                "tool": tool_name,
                "category": exc.category,
                "summary": exc.summary,
                "details": exc.details,
            }
        }
        if extra_payload:
            payload.update(extra_payload)
        payload = self._attach_absolute_urls(payload)
        payload = self._attach_guidance(
            tool_name,
            payload,
            is_error=True,
            exc=exc,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": self._render_visible_tool_text(
                        self._append_guidance_to_text("\n".join(lines), payload.get("guidance")),
                        payload,
                    ),
                }
            ],
            "structuredContent": payload,
            "isError": True,
        }

    def _tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "apps_list",
                "title": "List hosted Dash apps",
                "description": "Return the current hosted app inventory from the SQLite registry.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "repo_reconcile",
                "title": "Reconcile from Git desired state",
                "description": "Read desired-state manifests from the GitOps repository and apply them to the observed runtime and cache state.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "exasol_profiles_list",
                "title": "List Exasol profiles",
                "description": "Return Git-tracked Exasol profile metadata without secret values.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "exasol_profile_create_local",
                "title": "Create a local Exasol profile",
                "description": "Create one local Exasol profile for a single-user workflow. Provide either secret_value or secret_env_var so secrets stay outside Git.",
                "inputSchema": self._exasol_profile_create_local_schema(),
            },
            {
                "name": "exasol_profile_validate",
                "title": "Validate an Exasol profile",
                "description": "Resolve the configured secret, load pyexasol, and run a connection test.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_create",
                "title": "Create a hosted Dash app",
                "description": (
                    "Create a starter hosted app from metadata only. Use template=metric-cards for a generic static starter, "
                    "or template=exasol-analytics only when you are intentionally creating a profile-bound Exasol scaffold. "
                    "If you have source files, use app_create_from_files."
                ),
                "inputSchema": self._app_create_schema(),
            },
            {
                "name": "app_create_from_files",
                "title": "Create a hosted Dash app from files",
                "description": (
                    "Create a hosted app and seed its draft workspace from explicit files. "
                    "Use this when you already have app.py, requirements.txt, or assets. "
                    "template=metric-cards means a generic starter manifest; template=exasol-analytics means the files should follow the Exasol SQL-helper scaffold shape. "
                    "Do not embed Exasol credentials or direct pyexasol.connect(...) code in uploaded files; "
                    "use server-side Exasol profiles instead."
                ),
                "inputSchema": self._app_create_from_files_schema(),
            },
            {
                "name": "app_create_exasol_dashboard",
                "title": "Create an Exasol dashboard",
                "description": (
                    "Generate an Exasol-backed exasol-analytics scaffold from a validated profile and create it as a hosted app. "
                    "This is the preferred Exasol path because the hosted app only stores a profile reference and the server supplies credentials. "
                    "The default analytics-hub pattern creates a multi-tab app with system health, query history, and a business analytics placeholder."
                ),
                "inputSchema": self._app_create_exasol_dashboard_schema(),
            },
            {
                "name": "app_scaffold_from_schema",
                "title": "Create a schema-tailored Exasol dashboard",
                "description": (
                    "Introspect Exasol catalog metadata for a profile, choose analytically useful columns and relationship hints, "
                    "and generate a tailored exasol-analytics scaffold with business SQL wired to the selected schema and table."
                ),
                "inputSchema": self._app_scaffold_from_schema_schema(),
            },
            {
                "name": "app_put_files",
                "title": "Write draft files",
                "description": "Create or replace one or more files in the app draft workspace. Use this before app_validate.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Hosted app name.",
                        },
                        "files": {
                            "type": "array",
                            "description": "Draft files to create or replace.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "description": "Workspace-relative file path such as app.py or assets/theme.css.",
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": "Entire file content to write.",
                                    },
                                },
                                "required": ["path", "content"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["name", "files"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_read_file",
                "title": "Read a draft file",
                "description": "Return the current content of one draft workspace file. Use this to inspect app.py, requirements.txt, or other uploaded files before patching.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Hosted app name."},
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative file path such as app.py or dash-app.json.",
                        },
                    },
                    "required": ["name", "path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_diff_draft_vs_artifact",
                "title": "Compare draft against a built artifact",
                "description": (
                    "Show what differs between the current draft workspace and a built artifact. "
                    "When revision_number is omitted, the tool compares against the latest built revision."
                ),
                "inputSchema": self._app_diff_draft_vs_artifact_schema(),
            },
            {
                "name": "app_patch_file",
                "title": "Patch a draft file",
                "description": (
                    "Apply a search/replace patch to one file in the app draft workspace "
                    "and return a compact line-context preview of the updated file."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Hosted app name."},
                        "path": {"type": "string", "description": "Workspace-relative file path to patch."},
                        "search": {"type": "string", "description": "Exact text to search for."},
                        "replace": {"type": "string", "description": "Replacement text."},
                        "replace_all": {"type": "boolean", "description": "Replace every match when true. Defaults to false."},
                    },
                    "required": ["name", "path", "search", "replace"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_delete_file",
                "title": "Delete a draft file",
                "description": "Delete a non-required file from the app draft workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["name", "path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_validate",
                "title": "Validate a draft workspace",
                "description": (
                    "Run manifest, dependency, lint, syntax, import, callback, and credential-safety validation on the current draft workspace. "
                    "Use this before app_build or app_deploy_draft."
                ),
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_deploy_draft",
                "title": "Validate, build, and promote the draft",
                "description": (
                    "Run validate -> build -> deploy in one tool call. deployment_target=live promotes the revision to /apps/{name}; "
                    "deployment_target=preview mounts it under /preview/{name}/{revision}. "
                    "Optionally auto-rollback a live deployment if post-deploy health checks fail. "
                    "force_clean only bypasses cached dependency-install state; it does not change source snapshotting."
                ),
                "inputSchema": self._app_deploy_draft_schema(),
            },
            {
                "name": "app_collect_diagnostics",
                "title": "Collect diagnostics",
                "description": "Return lifecycle, health, logs, latest errors, validation results, and recovery suggestions.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_inspect_traceback",
                "title": "Inspect a traceback",
                "description": "Parse and classify a provided traceback, or inspect the app's latest captured traceback.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "traceback_text": {"type": "string"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_tail_logs",
                "title": "Tail app logs",
                "description": "Return recent log entries from the latest, build, runtime, or health log channels.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "channel": {
                            "type": "string",
                            "enum": list(self._log_channels),
                            "description": "Log channel. Use build for validation/build workflow logs.",
                        },
                        "limit": {"type": "integer"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_run_healthcheck",
                "title": "Run app health checks",
                "description": "Probe the mounted live or preview route, layout endpoint, dependencies endpoint, and static assets.",
                "inputSchema": self._app_healthcheck_schema(),
            },
            {
                "name": "app_share_get",
                "title": "Get app sharing policy",
                "description": "[hosted-mode] Return the app share policy, active grants, revoked grants, and sharing warnings.",
                "inputSchema": self._name_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_grant",
                "title": "Grant app access",
                "description": "[hosted-mode] Grant viewer, preview_viewer, editor, or owner access to a user, group, domain, organization, or public principal.",
                "inputSchema": self._app_share_grant_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_revoke",
                "title": "Revoke app access",
                "description": "[hosted-mode] Revoke one sharing grant by grant_id, or revoke active grants matching a principal.",
                "inputSchema": self._app_share_revoke_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_set_link_scope",
                "title": "Set app link scope",
                "description": "[hosted-mode] Set the app-level sharing policy to restricted, organization, domain, anyone_with_link, or public. Public anonymous access also requires server tenant policy.",
                "inputSchema": self._app_share_set_link_scope_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_explain_access",
                "title": "Explain app access",
                "description": "[hosted-mode] Explain whether a current or specified principal can access the live or preview dashboard and which grant or policy matched.",
                "inputSchema": self._app_share_explain_access_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_create_one_time_link",
                "title": "Create a one-time sharing link",
                "description": "[hosted-mode] Create a single-use, manually shared dashboard access link. The raw token is returned only in the tool response and only a hash is stored.",
                "inputSchema": self._app_share_create_one_time_link_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_share_revoke_one_time_link",
                "title": "Revoke a one-time sharing link",
                "description": "[hosted-mode] Revoke a manually shared one-time link and any link-derived ACL grant created by redemption.",
                "inputSchema": self._app_share_revoke_one_time_link_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_invite_external_user",
                "title": "Invite an external user",
                "description": "[hosted-mode] Create a hashed-token email invitation for an external user. The raw accept token is returned only once; manual email delivery is used until a sender integration is configured.",
                "inputSchema": self._app_invite_external_user_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_revoke_external_invitation",
                "title": "Revoke an external invitation",
                "description": "[hosted-mode] Revoke a pending or accepted external invitation and revoke the accepted grant when present.",
                "inputSchema": self._app_revoke_external_invitation_schema(),
                "_meta": {"availability": "hosted"},
            },
            {
                "name": "app_build",
                "title": "Build a new immutable revision",
                "description": (
                    "Validate the draft workspace and create a new immutable revision with a stored source artifact. "
                    "Use app_start_preview or app_promote_revision after this. force_clean only bypasses cached "
                    "dependency-install state; it does not change source snapshotting."
                ),
                "inputSchema": self._app_build_schema(),
            },
            {
                "name": "app_start_preview",
                "title": "Start a preview revision",
                "description": "Mount a revision under /preview/{app}/{revision}.",
                "inputSchema": self._revision_schema(),
            },
            {
                "name": "app_promote_revision",
                "title": "Promote a revision to live",
                "description": "Switch the live route to a built revision and retain the previous live revision for rollback. If the app runtime is currently stopped, call app_start afterwards to remount the live route.",
                "inputSchema": self._revision_schema(),
            },
            {
                "name": "app_rollback",
                "title": "Rollback the live revision",
                "description": "Revert the live route to the retained rollback target.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_start",
                "title": "Start an app runtime",
                "description": "Mount the current live revision for a hosted app.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_stop",
                "title": "Stop an app runtime",
                "description": "Unmount the live route without deleting revisions.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_restart",
                "title": "Restart an app runtime",
                "description": "Remount the current live revision for a hosted app.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_get_status",
                "title": "Get app status",
                "description": "Return lifecycle state, revision pointers, and draft workspace state for a hosted app.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_runtime_workers_list",
                "title": "List runtime workers and baselines",
                "description": (
                    "Return the in-process snapshot of out-of-process workers and forkserver baselines, "
                    "including aggregate RSS and p50 cold-start time. Available in isolated runtime mode."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_runtime_workers_restart",
                "title": "Restart a runtime worker",
                "description": (
                    "Stop the worker at mount_path and re-spawn it from the persisted spec. "
                    "Available in isolated runtime mode."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "mount_path": {
                            "type": "string",
                            "description": "Absolute mount path (e.g. /apps/sales).",
                        }
                    },
                    "required": ["mount_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_environment_invalidate",
                "title": "Invalidate a per-app environment",
                "description": (
                    "Mark a per-app dependency environment for removal on the next GC pass. "
                    "Available in per_app dependency-isolation mode."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "environment_id": {
                            "type": "string",
                            "description": "Environment id (sha256:…) from dash://runtime/environments.",
                        }
                    },
                    "required": ["environment_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_acknowledge_data_layer_errors",
                "title": "Acknowledge data-layer errors",
                "description": (
                    "Reset the `data_layer` healthcheck probe by acknowledging all currently "
                    "recorded Exasol query failures. Use after fixing SQL in-place without "
                    "promoting a new revision; the underlying `dash://apps/{name}/errors` "
                    "ledger is preserved, but the probe and `app_collect_diagnostics` both "
                    "filter past the new watermark."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The hosted app to acknowledge errors for.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        ]

    def _resource_definitions(self) -> list[dict[str, Any]]:
        resources = [
            {
                "uri": "dash://meta/app-create-schema",
                "name": "app-create-schema",
                "title": "app_create bundle schema",
                "description": "Required bundle shape, common mistakes, and a working example for app_create.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://meta/app-create-from-files-schema",
                "name": "app-create-from-files-schema",
                "title": "app_create_from_files schema",
                "description": "Required fields, common mistakes, and a working example for app_create_from_files.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://meta/app-authoring-guide",
                "name": "app-authoring-guide",
                "title": "Dash app authoring guide",
                "description": "Recommended create_dash_app factory structure, prefix rules, and common mistakes.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://meta/workflows",
                "name": "workflow-guide",
                "title": "Recommended MCP workflows",
                "description": "Canonical tool sequences for creating, editing, validating, and deploying hosted Dash apps.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://repo/status",
                "name": "repo-status",
                "title": "GitOps repository status",
                "description": "Read-only status for the local GitOps repository, including draft worktrees and current runtime-isolation settings.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://runtime/status",
                "name": "runtime-status",
                "title": "Runtime isolation status",
                "description": (
                    "Current control-plane host/port, APP_DEPENDENCY_ISOLATION, "
                    "APP_RUNTIME_MODE, cache roots, and worker config knobs."
                ),
                "mimeType": "application/json",
            },
            {
                "uri": "dash://runtime/workers",
                "name": "runtime-workers",
                "title": "Active runtime workers",
                "description": "Snapshot of out-of-process workers, baselines, RSS totals, and p50 cold-start time (isolated runtime mode).",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://runtime/environments",
                "name": "runtime-environments",
                "title": "Per-app dependency environments",
                "description": "Inventory of materialized per-app envs, disk usage, and wheel-cache size (per_app dependency mode).",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://runtime/logs/runtime.events",
                "name": "runtime-events",
                "title": "Runtime audit events",
                "description": "Server-wide audit log of operational decisions: env_evicted, wheel_cache_pruned, wheel_cache_gc_skipped, unsafe_override_warning.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://repo/desired-state",
                "name": "repo-desired-state",
                "title": "Git desired state",
                "description": "Authoritative live and preview deployment intent parsed from the GitOps repository.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://repo/drift",
                "name": "repo-drift",
                "title": "Git desired-state drift",
                "description": "Comparison between Git desired state and the observed runtime and cache state.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://exasol/help/connection-modes",
                "name": "exasol-connection-modes",
                "title": "Exasol connection modes",
                "description": "Phase 0 local Exasol connection modes, required fields, and the recommended dashboard workflow.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://exasol/help/dashboard-patterns",
                "name": "exasol-dashboard-patterns",
                "title": "Exasol dashboard patterns",
                "description": "Built-in Exasol dashboard scaffold patterns and when to use them.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://exasol/help/agent-workflow",
                "name": "exasol-agent-workflow",
                "title": "Exasol agent workflow",
                "description": "Recommended separation of responsibilities between dash-server and an external Exasol MCP server.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://exasol/help/sql-placeholders",
                "name": "exasol-sql-placeholders",
                "title": "Exasol SQL placeholder syntax",
                "description": "pyexasol placeholder grammar ({name!s}, {name!d}, etc.) for parameterized dashboard SQL. Replaces SQL-driver :name syntax which Exasol rejects.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://exasol/profiles",
                "name": "exasol-profiles",
                "title": "Exasol profiles",
                "description": "Git-tracked Exasol profile metadata without secrets.",
                "mimeType": "application/json",
            },
            {
                "uri": "dash://apps",
                "name": "dash-apps",
                "title": "Hosted Dash apps",
                "description": "Inventory of the currently registered Dash apps.",
                "mimeType": "application/json",
            }
        ]
        for app in self.runtime_service.list_apps():
            app_name = app["name"]
            resources.extend(
                [
                    {
                        "uri": f"dash://apps/{app_name}",
                        "name": f"{app_name}-app",
                        "title": f"{app_name} app",
                        "description": "Current app overview including exposure, runtime, and revision pointers.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/status",
                        "name": f"{app_name}-status",
                        "title": f"{app_name} status",
                        "description": "Lifecycle state, runtime mount state, revision pointers, and draft workspace state.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/health",
                        "name": f"{app_name}-health",
                        "title": f"{app_name} health",
                        "description": "Structured health probe results for the live app route.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/routes",
                        "name": f"{app_name}-routes",
                        "title": f"{app_name} routes",
                        "description": "Live and preview route bindings for the app.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/permissions",
                        "name": f"{app_name}-permissions",
                        "title": f"{app_name} permissions",
                        "description": "Declared filesystem, network, and env permissions for the app.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/sharing",
                        "name": f"{app_name}-sharing",
                        "title": f"{app_name} sharing",
                        "description": "Share policy, active ACL grants, revoked ACL grants, and warnings.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/manifest",
                        "name": f"{app_name}-manifest",
                        "title": f"{app_name} manifest",
                        "description": "Current manifest for the app's live revision.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/revisions",
                        "name": f"{app_name}-revisions",
                        "title": f"{app_name} revisions",
                        "description": "Immutable revisions for the app.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/events",
                        "name": f"{app_name}-events",
                        "title": f"{app_name} events",
                        "description": "Event log for revision build, preview, promote, rollback, and workspace edits.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/logs/latest",
                        "name": f"{app_name}-logs-latest",
                        "title": f"{app_name} latest logs",
                        "description": "Recent log entries aggregated across runtime, build, and health channels.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/logs/runtime",
                        "name": f"{app_name}-logs-runtime",
                        "title": f"{app_name} runtime logs",
                        "description": "Recent runtime mount and lifecycle log entries.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/logs/build",
                        "name": f"{app_name}-logs-build",
                        "title": f"{app_name} build logs",
                        "description": "Recent build, validation, and workspace-edit log entries.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/errors",
                        "name": f"{app_name}-errors",
                        "title": f"{app_name} errors",
                        "description": "Structured build and runtime errors captured for the app.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/callback-failures",
                        "name": f"{app_name}-callback-failures",
                        "title": f"{app_name} callback failures",
                        "description": "Structured callback error records captured for the app.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/dependency-report",
                        "name": f"{app_name}-dependency-report",
                        "title": f"{app_name} dependency report",
                        "description": "Declared requirements, invalid requirement entries, and install-plan notes for the draft workspace.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/files",
                        "name": f"{app_name}-files",
                        "title": f"{app_name} files",
                        "description": "List of editable draft files in the app workspace.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/artifacts/latest/files",
                        "name": f"{app_name}-artifact-files-latest",
                        "title": f"{app_name} latest artifact files",
                        "description": "List of source files present in the latest built artifact revision.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/diff/current...draft",
                        "name": f"{app_name}-diff",
                        "title": f"{app_name} live-to-draft diff",
                        "description": "Unified diff between the current live revision artifact and the draft workspace.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"dash://apps/{app_name}/diff/latest-build...draft",
                        "name": f"{app_name}-diff-latest-build",
                        "title": f"{app_name} latest-build-to-draft diff",
                        "description": "Unified diff and per-file comparison between the latest built artifact and the draft workspace.",
                        "mimeType": "application/json",
                    },
                ]
            )
        return resources

    def _exasol_service(self) -> ExasolDashboardService:
        if self.exasol_dashboard_service is None:
            raise DashServerError(
                category="exasol_not_configured",
                summary="Exasol dashboard features are not configured on this server.",
                details={},
                jsonrpc_code=-32012,
                http_status=500,
            )
        return self.exasol_dashboard_service

    def _require_name(self, arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        raise DashServerError(
            category="tool_validation_error",
            summary="Tool argument name must be a non-empty string.",
            details={"field": "name"},
            jsonrpc_code=-32602,
        )

    def _require_revision_number(self, arguments: dict[str, Any], field_name: str) -> int:
        value = arguments.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        raise self._field_error("revision_tool", field_name, "must be a positive integer.")

    def _require_existing_app(self, name: str, *, tool_name: str):
        app = self.runtime_service.registry.get_app(name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {name} does not exist.",
                details={"tool": tool_name, "app": name},
                jsonrpc_code=-32602,
                http_status=404,
            )
        return app

    def _share_payload(self, name: str) -> dict[str, Any]:
        app = self._require_existing_app(name, tool_name="app_share_get")
        all_grants = self.runtime_service.registry.list_acl_entries(
            name,
            include_revoked=True,
        )
        return {
            "app": app.to_dict(),
            "share_policy": self.runtime_service.registry.get_share_policy(name),
            "grants": self.runtime_service.registry.list_acl_entries(name),
            "revoked_grants": [grant for grant in all_grants if grant["revoked_at"] is not None],
            "one_time_links": [
                self._sanitize_share_link(link)
                for link in self.runtime_service.registry.list_share_links(name, include_revoked=True)
            ],
            "invitations": [
                self._sanitize_invitation(invitation)
                for invitation in self.runtime_service.registry.list_invitations(name, include_revoked=True)
            ],
            "warnings": self._sharing_warnings(name),
        }

    def _sanitize_share_link(self, link: dict[str, Any] | None) -> dict[str, Any] | None:
        if link is None:
            return None
        return {
            key: value
            for key, value in link.items()
            if key != "token_hash"
        }

    def _sanitize_invitation(self, invitation: dict[str, Any] | None) -> dict[str, Any] | None:
        if invitation is None:
            return None
        return {
            key: value
            for key, value in invitation.items()
            if key != "token_hash"
        }

    def _sharing_warnings(self, name: str) -> list[dict[str, str]]:
        app = self.runtime_service.registry.get_app(name)
        policy = self.runtime_service.registry.get_share_policy(name)
        warnings = []
        if app is not None and app.visibility == "public" and policy["link_scope"] != "public":
            warnings.append(
                {
                    "code": "public_visibility_without_public_policy",
                    "message": "The app visibility is public, but the share policy is not public.",
                }
            )
        if app is not None and app.auth_policy == "required" and policy["link_scope"] == "public":
            warnings.append(
                {
                    "code": "public_policy_requires_auth",
                    "message": "The share policy is public, but auth_policy=required still blocks anonymous access.",
                }
            )
        return warnings

    def _principal_for_access_explanation(self, arguments: dict[str, Any]) -> Principal:
        principal_id = arguments.get("principal_id")
        if principal_id is None:
            return current_auth_context().principal if has_request_context() else Principal.local_admin()
        principal_id = self._require_non_empty_string(
            principal_id,
            field_name="principal_id",
            tool_name="app_share_explain_access",
        )
        if principal_id.startswith("share_link:"):
            try:
                link_id = int(principal_id.split(":", 1)[1])
            except ValueError as exc:
                raise self._field_error(
                    "app_share_explain_access", "principal_id", "must contain a numeric share_link id."
                ) from exc
            link = self.runtime_service.registry.get_share_link(link_id)
            if link is not None:
                return Principal.link_access(
                    link_id=link_id,
                    app_name=link["app_name"],
                    role=link["role"],
                    scope=link["scope"],
                    email=link["recipient_email"],
                )
        user = self.runtime_service.registry.get_user_by_principal_id(principal_id)
        groups = arguments.get("groups", [])
        if groups is None:
            groups = []
        if not isinstance(groups, list) or any(not isinstance(item, str) for item in groups):
            raise self._field_error("app_share_explain_access", "groups", "must be an array of strings.")
        if user is not None:
            return Principal.authenticated_user(
                issuer=user["issuer"],
                subject=user["subject"],
                email=user["email"],
                display_name=user["display_name"],
                groups=tuple(groups),
                roles=(),
                email_verified=bool(user["email_verified"]),
                tenant_id=user["tenant_id"],
            )
        if ":" in principal_id:
            issuer, subject = principal_id.split(":", 1)
        else:
            issuer, subject = "explain", principal_id
        email = arguments.get("email")
        tenant_id = arguments.get("tenant_id")
        roles = arguments.get("roles", [])
        if email is not None and not isinstance(email, str):
            raise self._field_error("app_share_explain_access", "email", "must be a string.")
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise self._field_error("app_share_explain_access", "tenant_id", "must be a string.")
        if not isinstance(roles, list) or any(not isinstance(item, str) for item in roles):
            raise self._field_error("app_share_explain_access", "roles", "must be an array of strings.")
        return Principal.authenticated_user(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=principal_id,
            groups=tuple(groups),
            roles=tuple(roles),
            email_verified=bool(email),
            tenant_id=tenant_id,
        )

    def _current_principal_id(self) -> str:
        if has_request_context():
            return current_auth_context().principal.principal_id
        return "system"

    def _current_principal(self) -> Principal:
        if has_request_context():
            return current_auth_context().principal
        return Principal.local_admin()

    def _normalize_email_argument(self, value: Any, *, tool_name: str) -> str:
        if not isinstance(value, str):
            raise self._field_error(tool_name, "recipient_email", "must be a string.")
        email = value.strip()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise self._field_error(tool_name, "recipient_email", "must be an email address.")
        return email.lower()

    def _email_sender(self) -> InvitationEmailSender:
        if self.email_sender is not None:
            return self.email_sender
        if has_request_context():
            return current_app.extensions["email_sender"]
        return InvitationEmailSender({"DASH_SERVER_EMAIL_PROVIDER": "manual"})

    def _invitation_delivery_note(self, invitation: dict[str, Any]) -> str:
        status = invitation.get("delivery_status")
        if status == "sent":
            return "The invitation email was handed to the configured provider. The raw token remains display-once."
        if status == "failed":
            return "Email delivery failed. Use the display-once accept_url for manual recovery or fix the provider configuration and create a new invitation."
        return "Email sender integration is manual/disabled; deliver the accept_url to the recipient through an approved channel."

    def _authorization_service(self):
        return current_app.extensions["authorization_service"]

    def _require_string(self, value: Any, field_name: str, *, allow_empty: bool = False) -> str:
        if isinstance(value, str) and (allow_empty or value):
            return value
        raise self._field_error("string_tool", field_name, "must be a string.")

    def _field_error(self, tool_name: str, field_name: str, detail: str) -> DashServerError:
        return DashServerError(
            category="tool_validation_error",
            summary=f"{field_name} {detail}",
            details={"tool": tool_name, "field": field_name},
            jsonrpc_code=-32602,
        )

    def _require_non_empty_string(self, value: Any, *, field_name: str, tool_name: str) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise self._field_error(tool_name, field_name, "must be a non-empty string.")

    def _require_choice(
        self,
        value: Any,
        *,
        field_name: str,
        allowed: set[str],
        tool_name: str,
    ) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        raise self._field_error(
            tool_name,
            field_name,
            f"must be one of {', '.join(sorted(allowed))}.",
        )

    def _optional_positive_int(self, value: Any, *, tool_name: str, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        raise self._field_error(tool_name, field_name, "must be a positive integer.")

    def _name_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Hosted app name.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _exasol_profile_create_local_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Profile identifier stored under profiles/exasol/{name}.json.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["onprem", "saas"],
                    "description": "Backend type. onprem supports password/access/refresh token; saas supports saas_pat.",
                },
                "credential_mode": {
                    "type": "string",
                    "enum": ["password", "access_token", "refresh_token", "saas_pat"],
                    "description": "Credential mode for the bound secret.",
                },
                "dsn": {
                    "type": "string",
                    "description": "Exasol DSN or database endpoint used by pyexasol.",
                },
                "user": {
                    "type": "string",
                    "description": "Database username for the profile.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional human-readable description.",
                },
                "tls_verify": {
                    "type": "boolean",
                    "description": "Enable TLS certificate validation. Defaults to true.",
                    "default": True,
                },
                "secret_value": {
                    "type": "string",
                    "description": "Secret to persist in the local secret store. Provide exactly one of secret_value or secret_env_var.",
                },
                "secret_env_var": {
                    "type": "string",
                "description": "Environment variable containing the secret. Provide exactly one of secret_value or secret_env_var.",
                },
                "statement_timeout_seconds": {"type": "integer", "minimum": 1},
                "row_limit": {"type": "integer", "minimum": 1},
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "When false (default), the call fails with `exasol_profile_already_exists` "
                        "if a profile with this name is already on disk. Pass `true` to rewrite "
                        "the metadata (the response will set `was_already_present: true`). This "
                        "matches the persona-1 expectation that the same name isn't silently clobbered."
                    ),
                    "default": False,
                },
            },
            "required": ["name", "backend", "credential_mode", "dsn", "user"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "analytics-prod",
                    "backend": "onprem",
                    "credential_mode": "password",
                    "dsn": "demodb.exasol.com:8563",
                    "user": "sys",
                    "secret_env_var": "EXA_PASSWORD",
                    "tls_verify": True,
                }
            ],
        }

    def _app_create_exasol_dashboard_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "Create a hosted Exasol dashboard from a server-side profile. "
                "Use this instead of writing pyexasol.connect(...) or embedding DSN/user/password/token values in app code."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Hosted app name for the generated Exasol dashboard.",
                },
                "profile_name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Existing Exasol profile to bind into dash-app.json data_sources.primary.profile.",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["analytics-hub", "overview", "kpi-trend", "ops-monitor"],
                    "description": (
                        "Scaffold pattern. analytics-hub is the default exasol-analytics template: "
                        "system health tab, query history tab, and a business analytics placeholder."
                    ),
                    "default": "analytics-hub",
                },
                "title": {"type": "string", "description": "Optional dashboard title."},
                "route": {"type": "string", "description": "Optional route. Defaults to /apps/{name}."},
                "description": {"type": "string", "description": "Optional dashboard description."},
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the generated dashboard immediately.",
                    "default": True,
                },
            },
            "required": ["name", "profile_name"],
            "additionalProperties": False,
            "examples": [
                {"name": "sales-overview", "profile_name": "analytics-prod", "pattern": "analytics-hub", "start_immediately": True}
            ],
        }

    def _app_scaffold_from_schema_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "Create a schema-tailored Exasol scaffold by introspecting visible Exasol tables and columns. "
                "The generated app uses the exasol-analytics template with system tabs plus business SQL seeded from the selected schema."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Hosted app name for the generated schema scaffold.",
                },
                "profile_name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Existing Exasol profile to bind into dash-app.json data_sources.primary.profile.",
                },
                "schema_name": {
                    "type": "string",
                    "description": "Optional schema to prioritize during introspection. When omitted, the tool picks from visible non-system schemas.",
                },
                "table_name": {
                    "type": "string",
                    "description": "Optional specific table inside schema_name to base the scaffold on. When omitted, the highest-scoring table in the schema is picked automatically. schema_blueprint.table_candidates lists alternatives.",
                },
                "title": {"type": "string", "description": "Optional dashboard title."},
                "route": {"type": "string", "description": "Optional route. Defaults to /apps/{name}."},
                "description": {"type": "string", "description": "Optional dashboard description."},
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the generated dashboard immediately.",
                    "default": True,
                },
            },
            "required": ["name", "profile_name"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "sales-orders",
                    "profile_name": "analytics-prod",
                    "schema_name": "SALES",
                    "start_immediately": True,
                }
            ],
        }

    def _app_create_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bundle": self._bundle_schema(),
                "name": {
                    "type": "string",
                    "description": (
                        "Compatibility shorthand. If bundle is omitted, app_create treats "
                        "top-level name/title/route fields as starter-app metadata."
                    ),
                },
                "title": {"type": "string"},
                "route": {"type": "string", "description": "Optional live route for shorthand creation."},
                "description": {"type": "string", "description": "Optional app description for shorthand creation."},
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Optional scaffold template for shorthand creation. metric-cards is the generic starter; exasol-analytics is the profile-bound Exasol scaffold shape.",
                },
                "data_sources": {
                    "type": "object",
                    "description": "Optional datasource bindings for shorthand creation.",
                },
                "headline": {"type": "string", "description": "Optional starter dashboard headline for shorthand creation."},
                "summary": {"type": "string", "description": "Optional starter dashboard summary for shorthand creation."},
                "metrics": {
                    "type": "array",
                    "description": "Optional starter metric cards for shorthand creation.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the initial revision immediately at the live route.",
                    "default": True,
                },
            },
            "anyOf": [{"required": ["bundle"]}, {"required": ["name"]}],
            "additionalProperties": False,
            "examples": [
                {
                    "bundle": app_create_example_bundle(),
                    "start_immediately": True,
                },
                {
                    "name": "markets-dashboard",
                    "start_immediately": True,
                }
            ],
        }

    def _app_create_from_files_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Required app identifier.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title. Defaults to a humanized form of name.",
                },
                "route": {
                    "type": "string",
                    "description": "Optional live route. Defaults to /apps/{name}.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional app description.",
                },
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Optional scaffold template used for generated metadata defaults. metric-cards is generic; exasol-analytics means the uploaded files should match the Exasol SQL-helper layout.",
                },
                "data_sources": {
                    "type": "object",
                    "description": (
                        "Optional data-source bindings. For an Exasol-bound app use "
                        "`{\"primary\": {\"kind\": \"exasol\", \"profile\": \"<profile-name>\"}}`. "
                        "Without this, the runtime helper can't resolve a profile and the first "
                        "callback will 500."
                    ),
                    "properties": {
                        "primary": {
                            "type": "object",
                            "description": "Primary data source. For Exasol: `{kind: 'exasol', profile: 'name'}`.",
                            "properties": {
                                "kind": {"type": "string"},
                                "profile": {"type": "string"},
                            },
                            "required": ["kind"],
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": True,
                },
                "headline": {"type": "string", "description": "Optional starter dashboard headline used for generated metadata defaults."},
                "summary": {"type": "string", "description": "Optional starter dashboard summary used for generated metadata defaults."},
                "metrics": {
                    "type": "array",
                    "description": "Optional starter metric cards used for generated metadata defaults.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                    "description": "Workspace files to seed into the draft after app creation.",
                },
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the initial revision immediately at the live route.",
                    "default": True,
                },
            },
            "required": ["name", "files"],
            "additionalProperties": False,
            "examples": [app_create_from_files_example()],
        }

    def _bundle_schema(self) -> dict[str, Any]:
        manifest_props = self._manifest_schema()["properties"]
        dashboard_props = self._dashboard_schema()["properties"]
        return {
            "type": "object",
            "description": (
                "Canonical metadata bundle with top-level manifest and dashboard objects. "
                "app_create only accepts metadata here. Do not include source files in bundle; "
                "use app_create_from_files for name + files bootstrap. Shorthand: manifest "
                "and dashboard fields may also appear directly at the bundle root."
            ),
            "properties": {
                "manifest": self._manifest_schema(),
                "dashboard": self._dashboard_schema(),
                **manifest_props,
                **dashboard_props,
            },
            "anyOf": [
                {"required": ["manifest"]},
                {"required": ["name"]},
            ],
            "additionalProperties": False,
            "examples": [app_create_example_bundle()],
        }

    def _manifest_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": "Metadata for the hosted app.",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Lowercase app identifier used in routes and registry records.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for the app.",
                },
                "route": {
                    "type": "string",
                    "description": "Live route. Must start with /apps/. Defaults to /apps/{name}.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional app description.",
                },
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Supported starter templates. metric-cards is the generic dashboard starter; exasol-analytics is the Exasol SQL-helper scaffold.",
                },
                "data_sources": {
                    "type": "object",
                    "description": "Optional datasource bindings such as data_sources.primary.profile for Exasol-backed apps.",
                },
            },
            "required": ["name", "title"],
            "additionalProperties": False,
        }

    def _dashboard_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": "Initial dashboard content for the metric-cards template. Exasol scaffolds primarily use generated SQL files instead of these starter metrics.",
            "properties": {
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                    "description": "At least one metric card to render.",
                },
            },
            "additionalProperties": False,
        }

    def _revision_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "revision_number": {"type": "integer", "description": "Numeric revision to preview or promote."},
            },
            "required": ["name", "revision_number"],
            "additionalProperties": False,
        }

    def _app_diff_draft_vs_artifact_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "revision_number": {
                    "type": "integer",
                    "description": "Optional built revision to compare against. Defaults to the latest built revision.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_build_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "bundle": {
                    **self._bundle_schema(),
                    "description": (
                        "Optional replacement bundle to load into the draft before "
                        "building. Must match the same shape as app_create.bundle."
                    ),
                },
                "force_clean": {
                    "type": "boolean",
                    "description": "Bypass cached dependency-install state before validation/build. Does not change source snapshotting.",
                    "default": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_deploy_draft_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "deployment_target": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "description": "Where to mount the newly built revision. live publishes at /apps/{name}; preview mounts at /preview/{name}/{revision}.",
                    "default": "live",
                },
                "auto_rollback_on_health_failure": {
                    "type": "boolean",
                    "description": "When deploying live, automatically roll back to the previous live revision if post-deploy health checks fail.",
                    "default": False,
                },
                "force_clean": {
                    "type": "boolean",
                    "description": "Bypass cached dependency-install state before validation/build. Does not change source snapshotting.",
                    "default": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_healthcheck_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "target": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "description": "Which mounted route to probe.",
                    "default": "live",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_share_grant_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "principal_type": {
                    "type": "string",
                    "enum": ["user", "group", "domain", "organization", "public"],
                },
                "principal_id": {
                    "type": "string",
                    "description": "Stable principal identifier, such as issuer:subject for users or external_id for groups.",
                },
                "display_name": {
                    "type": "string",
                    "description": "Optional display name when creating a local group grant.",
                },
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer", "editor", "owner"],
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview", "manage", "all"],
                    "default": "live",
                },
            },
            "required": ["name", "principal_type", "principal_id", "role"],
            "additionalProperties": False,
        }

    def _app_share_revoke_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "grant_id": {"type": "integer", "description": "Grant id returned by app_share_get."},
                "principal_type": {
                    "type": "string",
                    "enum": ["user", "group", "domain", "organization", "public"],
                },
                "principal_id": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_share_set_link_scope_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "link_scope": {
                    "type": "string",
                    "enum": ["restricted", "organization", "domain", "anyone_with_link", "public"],
                },
                "default_link_role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "allow_preview_link": {"type": "boolean", "default": False},
                "public_catalog_visible": {"type": "boolean"},
                "external_sharing_enabled": {"type": "boolean", "default": False},
            },
            "required": ["name", "link_scope"],
            "additionalProperties": False,
        }

    def _app_share_explain_access_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "target": {"type": "string", "enum": ["live", "preview"], "default": "live"},
                "principal_id": {
                    "type": "string",
                    "description": "Optional principal to explain. Defaults to the current request principal.",
                },
                "email": {"type": "string"},
                "tenant_id": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "roles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_share_create_one_time_link_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "default": "live",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": "How long the link can be redeemed. Defaults to 168 hours.",
                    "default": 168,
                },
                "recipient_email": {
                    "type": "string",
                    "description": "Optional intended recipient email for operator context. It is not a verified identity by itself.",
                },
                "recipient_note": {
                    "type": "string",
                    "description": "Optional note for the owner/admin. Do not place secrets here.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def _app_share_revoke_one_time_link_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "link_id": {"type": "integer", "description": "One-time link id returned by app_share_create_one_time_link."},
            },
            "required": ["name", "link_id"],
            "additionalProperties": False,
        }

    def _app_invite_external_user_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "recipient_email": {
                    "type": "string",
                    "description": "Email address that will own the accepted external user grant.",
                },
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "default": "live",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": "How long the invitation can be accepted. Defaults to 168 hours.",
                    "default": 168,
                },
                "message": {
                    "type": "string",
                    "description": "Optional owner/admin note for the invitation record. Do not place secrets here.",
                },
            },
            "required": ["name", "recipient_email"],
            "additionalProperties": False,
        }

    def _app_revoke_external_invitation_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "invitation_id": {
                    "type": "integer",
                    "description": "Invitation id returned by app_invite_external_user.",
                },
            },
            "required": ["name", "invitation_id"],
            "additionalProperties": False,
        }

    def _success_response(self, request_id: Any, result: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, 200

    def _error_response(
        self, request_id: Any, error_object: dict[str, Any], status_code: int
    ) -> tuple[dict[str, Any], int]:
        return {"jsonrpc": "2.0", "id": request_id, "error": error_object}, status_code

    def _log_mcp_error(self, method: str, params: dict[str, Any], exc: DashServerError) -> None:
        tool_name = params.get("name") if method == "tools/call" else None
        resource_uri = params.get("uri") if method == "resources/read" else None
        param_summary = self._summarize_params(method, params)
        LOGGER.warning(
            "MCP request failed: method=%s tool=%s uri=%s category=%s summary=%s params=%s details=%s",
            method,
            tool_name,
            resource_uri,
            exc.category,
            exc.summary,
            param_summary,
            exc.details,
        )

    def _summarize_params(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "tools/call":
            arguments = params.get("arguments")
            summary: dict[str, Any] = {"argument_keys": []}
            if isinstance(arguments, dict):
                summary["argument_keys"] = sorted(arguments.keys())
                bundle = arguments.get("bundle")
                if isinstance(bundle, dict):
                    summary["bundle_keys"] = sorted(bundle.keys())
                    manifest = bundle.get("manifest")
                    if isinstance(manifest, dict):
                        summary["manifest_keys"] = sorted(manifest.keys())
                    dashboard = bundle.get("dashboard")
                    if isinstance(dashboard, dict):
                        summary["dashboard_keys"] = sorted(dashboard.keys())
            return summary
        if method == "resources/read":
            return {"uri": params.get("uri")}
        return {}

    def _diagnostic_lines_for_error(self, exc: DashServerError) -> list[str]:
        details = exc.details
        if exc.category == "workspace_validation_error":
            validation = details.get("validation")
            if isinstance(validation, dict):
                return self._validation_summary_lines(validation)
        if exc.category == "artifact_preflight_failed":
            preflight = details.get("preflight")
            if isinstance(preflight, dict):
                lines: list[str] = []
                captured_errors = preflight.get("captured_errors")
                if isinstance(captured_errors, list) and captured_errors:
                    latest_error = captured_errors[-1]
                    if isinstance(latest_error, dict):
                        summary = latest_error.get("summary")
                        if isinstance(summary, str) and summary:
                            lines.append(summary)
                        parsed = latest_error.get("parsed_traceback")
                        if isinstance(parsed, dict):
                            tb_summary = parsed.get("summary")
                            if isinstance(tb_summary, str) and tb_summary:
                                lines.append(tb_summary)
                failed_probe = next(
                    (
                        probe
                        for probe in preflight.get("probes", [])
                        if isinstance(probe, dict) and probe.get("status") == "failed"
                    ),
                    None,
                )
                if isinstance(failed_probe, dict):
                    probe_name = failed_probe.get("name")
                    probe_details = failed_probe.get("details")
                    if isinstance(probe_name, str):
                        lines.append(f"Failed probe: {probe_name}")
                    if isinstance(probe_details, dict):
                        message = probe_details.get("message")
                        if isinstance(message, str) and message:
                            lines.append(message)
                return lines
        if exc.category in {"manifest_validation_error", "bundle_validation_error", "tool_validation_error"}:
            field = details.get("field")
            if isinstance(field, str):
                return [f"Field: {field}"]
        return []

    def _validation_summary_lines(self, validation: dict[str, Any]) -> list[str]:
        lines: list[str] = []

        dependency_install = validation.get("dependency_install")
        if isinstance(dependency_install, dict) and dependency_install.get("status") == "failed":
            notes = dependency_install.get("notes") or "Dependency install failed."
            lines.append(f"Dependency install failed: {notes}")
            stderr_tail = dependency_install.get("stderr_tail")
            if isinstance(stderr_tail, str) and stderr_tail.strip():
                lines.append(f"pip stderr: {self._first_non_empty_line(stderr_tail)}")

        syntax = validation.get("syntax")
        if isinstance(syntax, dict) and syntax.get("status") == "failed":
            errors = syntax.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                lines.append(
                    f"Syntax error in {first.get('path', 'unknown')} line {first.get('line', '?')}: {first.get('message', 'unknown error')}"
                )

        imports = validation.get("imports")
        if isinstance(imports, dict) and imports.get("status") == "failed":
            if imports.get("category") == "route_misconfiguration":
                details = imports.get("details")
                if isinstance(details, dict):
                    path = details.get("path")
                    status_code = details.get("status_code")
                    if isinstance(path, str) and status_code is not None:
                        lines.append(
                            f"Mounted route verification failed at {path}: expected a working Dash endpoint but got HTTP {status_code}."
                        )
            if imports.get("category") == "environment_missing_dependency":
                missing = imports.get("missing_dependency")
                if isinstance(missing, str) and missing:
                    lines.append(f"Missing environment dependency during import smoke check: {missing}")
            error = imports.get("error")
            if isinstance(error, str) and error:
                lines.append(f"Import smoke check failed: {self._first_non_empty_line(error)}")

        requirements = validation.get("requirements")
        if isinstance(requirements, dict):
            invalid = requirements.get("invalid")
            if isinstance(invalid, list) and invalid:
                lines.append(f"Invalid requirements: {', '.join(str(item) for item in invalid)}")

        lint = validation.get("lint")
        if isinstance(lint, dict):
            warnings = lint.get("warnings")
            if isinstance(warnings, list) and warnings:
                first = warnings[0]
                lines.append(
                    f"Lint warning in {first.get('path', 'unknown')} line {first.get('line', '?')}: {first.get('message', 'warning')}"
                )

        cross_module_symbols = validation.get("cross_module_symbols")
        if isinstance(cross_module_symbols, dict):
            issues = cross_module_symbols.get("issues")
            warnings = cross_module_symbols.get("warnings")
            if isinstance(issues, list) and issues:
                first = issues[0]
                lines.append(
                    "Cross-module symbol validation failed in "
                    f"{first.get('path', 'unknown')} line {first.get('line', '?')}: "
                    f"{first.get('message', 'missing local symbol')}"
                )
            elif isinstance(warnings, list) and warnings:
                first = warnings[0]
                lines.append(
                    "Cross-module symbol warning in "
                    f"{first.get('path', 'unknown')} line {first.get('line', '?')}: "
                    f"{first.get('message', 'warning')}"
                )

        credential_safety = validation.get("credential_safety")
        if isinstance(credential_safety, dict) and credential_safety.get("status") == "failed":
            findings = credential_safety.get("findings")
            if isinstance(findings, list) and findings:
                first = findings[0]
                lines.append(
                    f"Credential safety failed in {first.get('path', 'unknown')}: {first.get('message', 'credential safety failure')}"
                )
            else:
                lines.append("Credential safety validation failed for the current draft.")

        exasol_validation = validation.get("exasol")
        if isinstance(exasol_validation, dict):
            issues = exasol_validation.get("issues")
            if isinstance(issues, list) and issues:
                first = issues[0]
                prefix = "Exasol validation failed" if exasol_validation.get("status") == "failed" else "Exasol validation warning"
                lines.append(
                    f"{prefix} in {first.get('path', 'unknown')}: {first.get('message', 'Exasol validation issue')}"
                )

        callbacks = validation.get("callbacks")
        if isinstance(callbacks, dict):
            if callbacks.get("status") == "failed":
                missing_ids = callbacks.get("missing_layout_ids") or []
                if isinstance(missing_ids, list) and missing_ids:
                    lines.append(
                        "Callback validation failed: callbacks reference missing layout ids "
                        + ", ".join(str(item) for item in missing_ids)
                    )
                else:
                    lines.append("Callback validation failed for the current draft.")
            elif callbacks.get("count", 0):
                lines.append(f"Registered callbacks: {callbacks.get('count', 0)}")

        return lines

    def _first_non_empty_line(self, text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                return line.strip()
        return text.strip()

    def _render_visible_tool_text(self, summary_text: str, structured_content: dict[str, Any]) -> str:
        # `default=repr` lets us serialize objects the validator captured from the user's
        # app (Dash `Wildcard` sentinels, plotly Figures, etc.) without dropping the
        # whole response. This is the visible-text rendering path — the structured JSON
        # response is built separately via `_make_json_safe`.
        payload_text = json.dumps(structured_content, indent=2, ensure_ascii=False, default=repr)
        return f"{summary_text}\n\nResult:\n{payload_text}"

    def _attach_absolute_urls(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            enriched = {key: self._attach_absolute_urls(value) for key, value in payload.items()}
            route = enriched.get("route")
            if isinstance(route, str) and route.startswith("/"):
                enriched.setdefault("browser_url", self._absolute_url(route))
            preview_path = enriched.get("preview_path")
            if isinstance(preview_path, str) and preview_path.startswith("/"):
                enriched.setdefault("preview_url", self._absolute_url(preview_path))
            mount_path = enriched.get("mount_path")
            if isinstance(mount_path, str) and mount_path.startswith("/"):
                enriched.setdefault("browser_url", self._absolute_url(mount_path))
            if "live" in enriched and isinstance(enriched["live"], dict):
                live_mount = enriched["live"].get("mount_path")
                if isinstance(live_mount, str) and live_mount.startswith("/"):
                    enriched["live"].setdefault("browser_url", self._absolute_url(live_mount))
            if "preview" in enriched and isinstance(enriched["preview"], dict):
                preview_mount = enriched["preview"].get("mount_path")
                if isinstance(preview_mount, str) and preview_mount.startswith("/"):
                    enriched["preview"].setdefault("browser_url", self._absolute_url(preview_mount))
            return enriched
        if isinstance(payload, list):
            return [self._attach_absolute_urls(item) for item in payload]
        return payload

    def _absolute_url(self, path: str) -> str:
        if not path.startswith("/"):
            return path
        if has_request_context():
            public_base_url = current_app.config.get("DASH_SERVER_PUBLIC_BASE_URL")
            if isinstance(public_base_url, str) and public_base_url:
                return public_base_url.rstrip("/") + path
            return request.host_url.rstrip("/") + path
        return path

    def _append_guidance_to_text(
        self,
        text: str,
        guidance: dict[str, Any] | None,
    ) -> str:
        if not isinstance(guidance, dict):
            return text
        lines = [text]
        next_step = guidance.get("next_step")
        if isinstance(next_step, str) and next_step:
            lines.append(f"Next step: {next_step}")
        suggested_tools = guidance.get("suggested_tools")
        if isinstance(suggested_tools, list) and suggested_tools:
            lines.append(f"Suggested tools: {', '.join(str(tool) for tool in suggested_tools)}")
        related_resources = guidance.get("related_resources")
        if isinstance(related_resources, list) and related_resources:
            lines.append(
                f"Related resources: {', '.join(str(resource) for resource in related_resources)}"
            )
        return "\n".join(lines)

    def _attach_guidance(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        is_error: bool,
        exc: DashServerError | None = None,
    ) -> dict[str, Any]:
        enriched = dict(payload)
        guidance = self._guidance_for_tool(
            tool_name,
            payload,
            is_error=is_error,
            exc=exc,
        )
        # Defensive: dedupe related_resources at the boundary so callers can compose
        # lists freely without worrying about duplicates (e.g. tool-specific + default).
        related = guidance.get("related_resources")
        if isinstance(related, list):
            guidance["related_resources"] = _dedupe_preserving_order(related)
        enriched["guidance"] = guidance
        return enriched

    def _guidance_for_tool(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        is_error: bool,
        exc: DashServerError | None = None,
    ) -> dict[str, Any]:
        if is_error:
            return self._error_guidance(tool_name, exc)

        validation = payload.get("validation")
        if tool_name == "app_validate" and isinstance(validation, dict):
            if validation.get("is_valid"):
                # BUG-015 fix: when the validate run produced warnings, mention them in
                # the next-step text so an agent following guidance can't ship them
                # unnoticed. Reads the same `validation_summary` the handler attached.
                summary = payload.get("validation_summary") or {}
                warning_count = int(summary.get("warning_count") or 0)
                if warning_count:
                    return {
                        "next_step": (
                            f"Validation passed with {warning_count} warning(s). Review "
                            "`validation` in the structured payload; consider patching "
                            "before promoting to live."
                        ),
                        "suggested_tools": ["app_deploy_draft", "app_build", "app_patch_file"],
                        "related_resources": [
                            "dash://meta/workflows",
                            "dash://meta/app-authoring-guide",
                        ],
                    }
                return {
                    "next_step": "Build and deploy the validated draft.",
                    "suggested_tools": ["app_deploy_draft", "app_build"],
                    "related_resources": ["dash://meta/workflows", "dash://meta/app-authoring-guide"],
                }
            cross_module_symbols = validation.get("cross_module_symbols")
            if (
                isinstance(cross_module_symbols, dict)
                and cross_module_symbols.get("status") == "failed"
            ):
                return {
                    "next_step": "Patch the missing local symbol or import path, then validate the draft again.",
                    "suggested_tools": ["app_patch_file", "app_put_files", "app_validate"],
                    "related_resources": ["dash://meta/app-authoring-guide"],
                }
            return {
                "next_step": "Inspect the validation failures, patch the draft, and validate again.",
                "suggested_tools": ["app_collect_diagnostics", "app_put_files", "app_patch_file"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            }

        if tool_name == "app_deploy_draft" and payload.get("deployment_target") == "preview":
            return {
                "next_step": "Open the preview URL, run preview health checks if needed, and promote the reviewed revision when it is ready.",
                "suggested_tools": ["app_run_healthcheck", "app_promote_revision", "app_get_status"],
                "related_resources": ["dash://meta/workflows"],
            }

        if tool_name == "app_run_healthcheck" and payload.get("target") == "preview":
            return {
                "next_step": "If the preview is healthy, review it in the browser and promote the revision when approved.",
                "suggested_tools": ["app_promote_revision", "app_get_status", "app_tail_logs"],
                "related_resources": ["dash://meta/workflows"],
            }

        guidance_map: dict[str, dict[str, Any]] = {
            "apps_list": {
                "next_step": "Pick an app to inspect or create a new hosted app.",
                "suggested_tools": ["app_get_status", "app_create", "app_create_from_files", "app_create_exasol_dashboard"],
                "related_resources": ["dash://meta/workflows"],
            },
            "repo_reconcile": {
                "next_step": "Inspect drift or app status after applying Git desired state.",
                "suggested_tools": ["app_get_status", "app_run_healthcheck", "app_collect_diagnostics"],
                "related_resources": ["dash://repo/desired-state", "dash://repo/drift", "dash://meta/workflows"],
            },
            "exasol_profiles_list": {
                "next_step": "Validate a profile or create a new one, then generate a dashboard from it.",
                "suggested_tools": ["exasol_profile_validate", "exasol_profile_create_local", "app_create_exasol_dashboard"],
                "related_resources": ["dash://exasol/profiles", "dash://exasol/help/connection-modes", "dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow"],
            },
            "exasol_profile_create_local": {
                "next_step": "Validate the profile before generating a live Exasol dashboard. If an external Exasol MCP server is available, use it for schema discovery only and keep hosted runtime access on the validated dash-server profile.",
                "suggested_tools": ["exasol_profile_validate", "app_create_exasol_dashboard"],
                "related_resources": ["dash://exasol/help/connection-modes", "dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow", "dash://exasol/profiles"],
            },
            "exasol_profile_validate": {
                "next_step": "If validation passed, create a dashboard scaffold. Use an external Exasol MCP server only for discovery and SQL authoring; do not write pyexasol.connect(...) or Exasol credentials into app.py.",
                "suggested_tools": ["app_create_exasol_dashboard", "exasol_profile_create_local"],
                "related_resources": ["dash://exasol/help/connection-modes", "dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow", "dash://exasol/profiles"],
            },
            "app_create": {
                "next_step": "Edit the draft or validate it before building a new revision.",
                "suggested_tools": ["app_read_file", "app_put_files", "app_validate"],
                "related_resources": ["dash://meta/workflows", "dash://meta/app-authoring-guide"],
            },
            "app_create_from_files": {
                "next_step": "Validate the uploaded draft before building or deploying it.",
                "suggested_tools": ["app_read_file", "app_validate", "app_deploy_draft"],
                "related_resources": ["dash://meta/app-authoring-guide", "dash://meta/workflows"],
            },
            "app_create_exasol_dashboard": {
                "next_step": "Open the browser URL, then refine the generated SQL and Dash files within the scaffold pattern. Keep Exasol credentials in the server-side profile; use any external Exasol MCP server only for discovery and SQL design, not for runtime connection code.",
                "suggested_tools": ["app_read_file", "app_put_files", "app_validate", "app_run_healthcheck"],
                "related_resources": ["dash://exasol/help/connection-modes", "dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow", "dash://meta/app-authoring-guide", "dash://meta/workflows"],
            },
            "app_scaffold_from_schema": {
                "next_step": "Inspect SCHEMA_NOTES.md and the generated business SQL, then preview the revision before promoting it live.",
                "suggested_tools": ["app_read_file", "app_validate", "app_deploy_draft", "app_run_healthcheck"],
                "related_resources": ["dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow", "dash://meta/app-authoring-guide", "dash://meta/workflows"],
            },
            "app_put_files": {
                "next_step": "Validate the updated draft workspace.",
                "suggested_tools": ["app_read_file", "app_validate", "app_patch_file"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_read_file": {
                "next_step": "Patch the file or validate the draft after inspecting its contents.",
                "suggested_tools": ["app_patch_file", "app_put_files", "app_validate"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_diff_draft_vs_artifact": {
                "next_step": "Inspect the changed files, then rebuild or patch the draft based on what differs from the built artifact.",
                "suggested_tools": ["app_read_file", "app_build", "app_patch_file"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_patch_file": {
                "next_step": "Review the patch preview, then validate the updated draft workspace.",
                "suggested_tools": ["app_validate", "app_patch_file", "app_put_files"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_delete_file": {
                "next_step": "Validate the updated draft workspace.",
                "suggested_tools": ["app_validate", "app_put_files", "app_deploy_draft"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_build": {
                "next_step": "Preview or promote the built revision.",
                "suggested_tools": ["app_start_preview", "app_promote_revision", "app_run_healthcheck"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_deploy_draft": {
                "next_step": (
                    "If the app is mounted, check the live app and health. "
                    "If it is still stopped, call app_start first."
                ),
                "suggested_tools": ["app_get_status", "app_start", "app_run_healthcheck"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_start_preview": {
                "next_step": "Probe the preview and then promote it if it looks correct.",
                "suggested_tools": ["app_run_healthcheck", "app_promote_revision", "app_tail_logs"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_promote_revision": {
                "next_step": (
                    "If the app runtime is running, run health checks on the live route. "
                    "If the app is stopped, call app_start to remount it first."
                ),
                "suggested_tools": ["app_get_status", "app_start", "app_run_healthcheck"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_rollback": {
                "next_step": "Confirm the rolled back live app is healthy.",
                "suggested_tools": ["app_run_healthcheck", "app_get_status", "app_collect_diagnostics"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_start": {
                "next_step": "Check the live route and run health checks.",
                "suggested_tools": ["app_run_healthcheck", "app_get_status"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_stop": {
                "next_step": "Restart the app when you are ready to republish it.",
                "suggested_tools": ["app_start", "app_get_status"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_restart": {
                "next_step": "Verify the live route is healthy after restart.",
                "suggested_tools": ["app_run_healthcheck", "app_get_status", "app_tail_logs"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_get_status": {
                "next_step": "Use the status to decide whether to edit, deploy, or diagnose the app.",
                "suggested_tools": ["app_validate", "app_run_healthcheck", "app_collect_diagnostics"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_collect_diagnostics": {
                "next_step": "Use the latest error and validation report to decide the next patch.",
                "suggested_tools": ["app_patch_file", "app_put_files", "app_validate"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_inspect_traceback": {
                "next_step": "Patch the failing code path and validate again.",
                "suggested_tools": ["app_patch_file", "app_validate", "app_collect_diagnostics"],
                "related_resources": ["dash://meta/app-authoring-guide"],
            },
            "app_tail_logs": {
                "next_step": "Use the logs to decide whether to patch the app or inspect diagnostics.",
                "suggested_tools": ["app_collect_diagnostics", "app_patch_file", "app_validate"],
                "related_resources": ["dash://meta/workflows"],
            },
            "app_run_healthcheck": {
                "next_step": "Inspect any failed probes before changing the live revision.",
                "suggested_tools": ["app_collect_diagnostics", "app_tail_logs", "app_get_status"],
                "related_resources": ["dash://meta/workflows"],
            },
        }
        return guidance_map.get(
            tool_name,
            {
                "next_step": "Inspect the returned payload and continue with the next workflow step.",
                "suggested_tools": ["app_get_status", "app_collect_diagnostics"],
                "related_resources": ["dash://meta/workflows"],
            },
        )

    def _error_guidance(self, tool_name: str, exc: DashServerError | None) -> dict[str, Any]:
        category = exc.category if exc is not None else "unknown"
        if category in {"tool_validation_error", "manifest_validation_error", "bundle_validation_error"}:
            help_resource = "dash://meta/workflows"
            if exc is not None:
                candidate = exc.details.get("help_resource")
                if isinstance(candidate, str) and candidate:
                    help_resource = candidate
            return {
                "next_step": "Read the referenced schema/help resource and retry with the documented input shape.",
                "suggested_tools": ["apps_list", "app_validate", "exasol_profile_validate"],
                "related_resources": _dedupe_preserving_order(
                    [help_resource, "dash://meta/workflows"]
                ),
            }
        if category == "workspace_validation_error":
            return {
                "next_step": "Fix the draft workspace issues, then run validation again before rebuilding.",
                "suggested_tools": ["app_collect_diagnostics", "app_validate", "app_patch_file", "app_put_files"],
                "related_resources": ["dash://meta/app-authoring-guide", "dash://meta/workflows"],
            }
        if category == "runtime_mount_error":
            return {
                "next_step": "Inspect diagnostics and the authoring guide, then patch the Dash factory to mount cleanly.",
                "suggested_tools": ["app_collect_diagnostics", "app_inspect_traceback", "app_patch_file"],
                "related_resources": ["dash://meta/app-authoring-guide", "dash://meta/workflows"],
            }
        if category == "artifact_preflight_failed":
            return {
                "next_step": "Inspect the failed preflight probes and traceback, patch the draft, then rebuild before any live promotion.",
                "suggested_tools": ["app_collect_diagnostics", "app_inspect_traceback", "app_patch_file", "app_diff_draft_vs_artifact"],
                "related_resources": ["dash://meta/app-authoring-guide", "dash://meta/workflows"],
            }
        if category == "app_conflict":
            return {
                "next_step": "Choose a different app name or inspect the existing app before retrying.",
                "suggested_tools": ["apps_list", "app_get_status"],
                "related_resources": ["dash://meta/workflows"],
            }
        if category.startswith("exasol_"):
            return {
                "next_step": "Inspect the Exasol profile metadata and connection help, fix the profile or secret reference, then retry.",
                "suggested_tools": ["exasol_profiles_list", "exasol_profile_create_local", "exasol_profile_validate"],
                "related_resources": ["dash://exasol/profiles", "dash://exasol/help/connection-modes", "dash://exasol/help/dashboard-patterns", "dash://exasol/help/agent-workflow"],
            }
        return {
            "next_step": "Inspect the returned error details and recent diagnostics before retrying.",
            "suggested_tools": ["app_collect_diagnostics", "app_tail_logs"],
            "related_resources": ["dash://meta/workflows"],
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

    def _bundle_from_top_level_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        bundle: dict[str, Any] = {}
        for field_name in (
            "name",
            "title",
            "route",
            "description",
            "template",
            "data_sources",
            "headline",
            "summary",
            "metrics",
        ):
            if field_name in arguments:
                bundle[field_name] = arguments[field_name]
        return bundle


Stage4MCPServer = MCPServer
Stage3MCPServer = MCPServer
