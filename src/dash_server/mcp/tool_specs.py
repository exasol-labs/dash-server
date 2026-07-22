"""One declaration per MCP tool.

Each tool used to be declared in several parallel places (the handler dict, the
transport capability map in the blueprint, the job-scoped set, plus the
definitions/guidance builders). ``TOOL_SPECS`` is the single source for a tool's
*wiring*: which handler method serves it, and — for app-scoped tools — which
capability governs it and where that capability is enforced.

- ``handler`` names an ``MCPServer`` method; the server binds it into its
  handler dict from this list.
- ``app_capability`` is the capability the ``/mcp`` transport gate checks so a
  grant-only principal can reach the tool (the blueprint's app-scoped map is
  derived from it). ``None`` means the tool is control-plane-global (gated only
  by the coarse role check).
- ``job_scoped`` marks the export tools keyed by ``job_id`` (their app is
  resolved via the consumption service); the blueprint's job-scoped set derives
  from this.
- ``enforce_in_handler`` makes the server re-check ``app_capability`` in the
  handler path, so the transport map is defense-in-depth rather than the sole
  gate. It is set for the app-scoped tools that have no downstream service-layer
  authorization of their own (sharing/invitation management, draft file listing,
  app deletion). The consumption/export tools authorize inside
  ``ConsumptionService`` and are intentionally left ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass

from dash_server.auth.capabilities import (
    DASHBOARD_DELETE,
    DASHBOARD_EDIT_DRAFT,
    DASHBOARD_EXPORT,
    DASHBOARD_MANAGE_CONSUMPTION,
    DASHBOARD_MANAGE_SHARING,
)


@dataclass(frozen=True)
class ToolSpec:
    """Wiring for one MCP tool: handler plus optional app-capability governance."""

    name: str
    handler: str
    app_capability: str | None = None
    job_scoped: bool = False
    enforce_in_handler: bool = False


TOOL_SPECS: tuple[ToolSpec, ...] = (
    # Control-plane-global tools (no per-app capability; coarse role gate only).
    ToolSpec("apps_list", "_tool_apps_list"),
    ToolSpec("repo_reconcile", "_tool_repo_reconcile"),
    ToolSpec("exasol_profiles_list", "_tool_exasol_profiles_list"),
    ToolSpec("exasol_profile_create_local", "_tool_exasol_profile_create_local"),
    ToolSpec("exasol_profile_validate", "_tool_exasol_profile_validate"),
    ToolSpec("app_create", "_tool_app_create"),
    ToolSpec("app_create_from_files", "_tool_app_create_from_files"),
    ToolSpec("app_create_exasol_dashboard", "_tool_app_create_exasol_dashboard"),
    ToolSpec("app_scaffold_from_schema", "_tool_app_scaffold_from_schema"),
    ToolSpec("app_start", "_tool_app_start"),
    ToolSpec("app_stop", "_tool_app_stop"),
    ToolSpec("app_restart", "_tool_app_restart"),
    ToolSpec("app_get_status", "_tool_app_get_status"),
    ToolSpec("app_build", "_tool_app_build"),
    ToolSpec("app_deploy_draft", "_tool_app_deploy_draft"),
    ToolSpec("app_start_preview", "_tool_app_start_preview"),
    ToolSpec("app_promote_revision", "_tool_app_promote_revision"),
    ToolSpec("app_rollback", "_tool_app_rollback"),
    ToolSpec("app_put_files", "_tool_app_put_files"),
    ToolSpec("app_read_file", "_tool_app_read_file"),
    ToolSpec("app_diff_draft_vs_artifact", "_tool_app_diff_draft_vs_artifact"),
    ToolSpec("app_patch_file", "_tool_app_patch_file"),
    ToolSpec("app_delete_file", "_tool_app_delete_file"),
    ToolSpec("app_validate", "_tool_app_validate"),
    ToolSpec("app_collect_diagnostics", "_tool_app_collect_diagnostics"),
    ToolSpec("app_inspect_traceback", "_tool_app_inspect_traceback"),
    ToolSpec("app_tail_logs", "_tool_app_tail_logs"),
    ToolSpec("app_run_healthcheck", "_tool_app_run_healthcheck"),
    ToolSpec("app_runtime_workers_list", "_tool_app_runtime_workers_list"),
    ToolSpec("app_runtime_workers_restart", "_tool_app_runtime_workers_restart"),
    ToolSpec("app_environment_invalidate", "_tool_app_environment_invalidate"),
    ToolSpec("app_acknowledge_data_layer_errors", "_tool_app_acknowledge_data_layer_errors"),
    # App-scoped tools with no downstream service check: enforced in the handler.
    ToolSpec("app_list_files", "_tool_app_list_files", DASHBOARD_EDIT_DRAFT, enforce_in_handler=True),
    ToolSpec("app_delete", "_tool_app_delete", DASHBOARD_DELETE, enforce_in_handler=True),
    ToolSpec("app_share_get", "_tool_app_share_get", DASHBOARD_MANAGE_SHARING, enforce_in_handler=True),
    ToolSpec("app_share_grant", "_tool_app_share_grant", DASHBOARD_MANAGE_SHARING, enforce_in_handler=True),
    ToolSpec("app_share_revoke", "_tool_app_share_revoke", DASHBOARD_MANAGE_SHARING, enforce_in_handler=True),
    ToolSpec(
        "app_share_set_link_scope",
        "_tool_app_share_set_link_scope",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    ToolSpec(
        "app_share_explain_access",
        "_tool_app_share_explain_access",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    ToolSpec(
        "app_share_create_one_time_link",
        "_tool_app_share_create_one_time_link",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    ToolSpec(
        "app_share_revoke_one_time_link",
        "_tool_app_share_revoke_one_time_link",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    ToolSpec(
        "app_invite_external_user",
        "_tool_app_invite_external_user",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    ToolSpec(
        "app_revoke_external_invitation",
        "_tool_app_revoke_external_invitation",
        DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
    ),
    # App-scoped tools authorized inside ConsumptionService (transport map only).
    ToolSpec("app_outputs_list", "_tool_app_outputs_list", DASHBOARD_EXPORT),
    ToolSpec("app_output_get", "_tool_app_output_get", DASHBOARD_EXPORT),
    ToolSpec("app_export_create", "_tool_app_export_create", DASHBOARD_EXPORT),
    ToolSpec("app_exports_list", "_tool_app_exports_list", DASHBOARD_EXPORT),
    ToolSpec("app_exports_admin_list", "_tool_app_exports_admin_list", DASHBOARD_MANAGE_CONSUMPTION),
    # Job-scoped export tools (app resolved from job_id by the consumption service).
    ToolSpec("export_get", "_tool_export_get", DASHBOARD_EXPORT, job_scoped=True),
    ToolSpec("export_cancel", "_tool_export_cancel", DASHBOARD_EXPORT, job_scoped=True),
    ToolSpec(
        "export_download_link_create",
        "_tool_export_download_link_create",
        DASHBOARD_EXPORT,
        job_scoped=True,
    ),
)


TOOL_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# Derived views consumed by the blueprint transport gate.
APP_SCOPED_TOOL_CAPABILITIES: dict[str, str] = {
    spec.name: spec.app_capability
    for spec in TOOL_SPECS
    if spec.app_capability is not None and not spec.job_scoped
}
JOB_SCOPED_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_SPECS if spec.job_scoped
)


__all__ = [
    "APP_SCOPED_TOOL_CAPABILITIES",
    "JOB_SCOPED_TOOLS",
    "TOOL_SPECS",
    "TOOL_SPECS_BY_NAME",
    "ToolSpec",
]
