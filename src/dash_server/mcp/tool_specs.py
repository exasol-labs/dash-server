"""One declaration per MCP tool.

Each tool used to live in several parallel places (the handler dict, the
transport capability map in the blueprint, the job-scoped set, the ``tools/list``
definition, and the static guidance map). ``TOOL_SPECS`` is now the single source
for a tool's *wiring* **and** its declaration:

- ``handler`` names an ``MCPServer`` method; the server binds it into its handler
  dict from this list.
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
- ``title``/``description``/``input_schema``/``meta`` fold the ``tools/list``
  definition onto the spec (P2.2). ``input_schema`` is either an inline JSON-schema
  ``dict`` or the *name* of a builder method on ``MCPServer`` (see ``mcp/schemas.py``);
  ``MCPServer._tool_definitions`` derives ``tools/list`` from these fields.
- ``guidance`` is the tool's static agent-guidance entry; ``_guidance_for_tool``
  falls back to it after its dynamic (payload-dependent) cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dash_server.auth.capabilities import (
    DASHBOARD_DELETE,
    DASHBOARD_EDIT_DRAFT,
    DASHBOARD_EXPORT,
    DASHBOARD_MANAGE_CONSUMPTION,
    DASHBOARD_MANAGE_SHARING,
)


@dataclass(frozen=True)
class ToolSpec:
    """Wiring plus declaration for one MCP tool."""

    name: str
    handler: str
    app_capability: str | None = None
    job_scoped: bool = False
    enforce_in_handler: bool = False
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] | str | None = None
    meta: dict[str, Any] | None = None
    guidance: dict[str, Any] | None = None


TOOL_SPECS: tuple[ToolSpec, ...] = (

    ToolSpec(
        name='apps_list',
        handler='_tool_apps_list',
        title='List hosted Dash apps',
        description='Return the current hosted app inventory from the SQLite registry.',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        guidance={'next_step': 'Pick an app to inspect or create a new hosted app.',
         'suggested_tools': ['app_get_status',
                             'app_create',
                             'app_create_from_files',
                             'app_create_exasol_dashboard'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='repo_reconcile',
        handler='_tool_repo_reconcile',
        title='Reconcile from Git desired state',
        description='Read desired-state manifests from the GitOps repository and apply them to the observed runtime and cache state.',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        guidance={'next_step': 'Inspect drift or app status after applying Git desired state.',
         'suggested_tools': ['app_get_status', 'app_run_healthcheck', 'app_collect_diagnostics'],
         'related_resources': ['dash://repo/desired-state',
                               'dash://repo/drift',
                               'dash://meta/workflows']},
    ),
    ToolSpec(
        name='exasol_profiles_list',
        handler='_tool_exasol_profiles_list',
        title='List Exasol profiles',
        description='Return Git-tracked Exasol profile metadata without secret values.',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        guidance={'next_step': 'Validate a profile or create a new one, then generate a dashboard from it.',
         'suggested_tools': ['exasol_profile_validate',
                             'exasol_profile_create_local',
                             'app_create_exasol_dashboard'],
         'related_resources': ['dash://exasol/profiles',
                               'dash://exasol/help/connection-modes',
                               'dash://exasol/help/dashboard-patterns',
                               'dash://exasol/help/agent-workflow']},
    ),
    ToolSpec(
        name='exasol_profile_create_local',
        handler='_tool_exasol_profile_create_local',
        title='Create a local Exasol profile',
        description='Create one local Exasol profile for a single-user workflow. Provide either secret_value or secret_env_var so secrets stay outside Git.',
        input_schema='_exasol_profile_create_local_schema',
        guidance={'next_step': 'Validate the profile before generating a live Exasol dashboard. If an '
                      'external Exasol MCP server is available, use it for schema discovery only '
                      'and keep hosted runtime access on the validated dash-server profile.',
         'suggested_tools': ['exasol_profile_validate', 'app_create_exasol_dashboard'],
         'related_resources': ['dash://exasol/help/connection-modes',
                               'dash://exasol/help/dashboard-patterns',
                               'dash://exasol/help/agent-workflow',
                               'dash://exasol/profiles']},
    ),
    ToolSpec(
        name='exasol_profile_validate',
        handler='_tool_exasol_profile_validate',
        title='Validate an Exasol profile',
        description='Resolve the configured secret, load pyexasol, and run a connection test.',
        input_schema='_name_schema',
        guidance={'next_step': 'If validation passed, create a dashboard scaffold. Use an external Exasol '
                      'MCP server only for discovery and SQL authoring; do not write '
                      'pyexasol.connect(...) or Exasol credentials into app.py.',
         'suggested_tools': ['app_create_exasol_dashboard', 'exasol_profile_create_local'],
         'related_resources': ['dash://exasol/help/connection-modes',
                               'dash://exasol/help/dashboard-patterns',
                               'dash://exasol/help/agent-workflow',
                               'dash://exasol/profiles']},
    ),
    ToolSpec(
        name='app_create',
        handler='_tool_app_create',
        title='Create a hosted Dash app',
        description='Create a starter hosted app from metadata only. Use template=metric-cards for a generic static starter, or template=exasol-analytics only when you are intentionally creating a profile-bound Exasol scaffold. If you have source files, use app_create_from_files.',
        input_schema='_app_create_schema',
        guidance={'next_step': 'Edit the draft or validate it before building a new revision.',
         'suggested_tools': ['app_read_file', 'app_put_files', 'app_validate'],
         'related_resources': ['dash://meta/workflows', 'dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_create_from_files',
        handler='_tool_app_create_from_files',
        title='Create a hosted Dash app from files',
        description='Create a hosted app and seed its draft workspace from explicit files. Use this when you already have app.py, requirements.txt, or assets. template=metric-cards means a generic starter manifest; template=exasol-analytics means the files should follow the Exasol SQL-helper scaffold shape. Do not embed Exasol credentials or direct pyexasol.connect(...) code in uploaded files; use server-side Exasol profiles instead.',
        input_schema='_app_create_from_files_schema',
        guidance={'next_step': 'Validate the uploaded draft before building or deploying it.',
         'suggested_tools': ['app_read_file', 'app_validate', 'app_deploy_draft'],
         'related_resources': ['dash://meta/app-authoring-guide', 'dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_create_exasol_dashboard',
        handler='_tool_app_create_exasol_dashboard',
        title='Create an Exasol dashboard',
        description='Generate an Exasol-backed exasol-analytics scaffold from a validated profile and create it as a hosted app. This is the preferred Exasol path because the hosted app only stores a profile reference and the server supplies credentials. The default analytics-hub pattern creates a multi-tab app with system health, query history, and a business analytics placeholder.',
        input_schema='_app_create_exasol_dashboard_schema',
        guidance={'next_step': 'Open the browser URL, then refine the generated SQL and Dash files within '
                      'the scaffold pattern. Keep Exasol credentials in the server-side profile; '
                      'use any external Exasol MCP server only for discovery and SQL design, not '
                      'for runtime connection code.',
         'suggested_tools': ['app_read_file',
                             'app_put_files',
                             'app_validate',
                             'app_run_healthcheck'],
         'related_resources': ['dash://exasol/help/connection-modes',
                               'dash://exasol/help/dashboard-patterns',
                               'dash://exasol/help/agent-workflow',
                               'dash://meta/app-authoring-guide',
                               'dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_scaffold_from_schema',
        handler='_tool_app_scaffold_from_schema',
        title='Create a schema-tailored Exasol dashboard',
        description='Introspect Exasol catalog metadata for a profile, choose analytically useful columns and relationship hints, and generate a tailored exasol-analytics scaffold with business SQL wired to the selected schema and table.',
        input_schema='_app_scaffold_from_schema_schema',
        guidance={'next_step': 'Inspect SCHEMA_NOTES.md and the generated business SQL, then preview the '
                      'revision before promoting it live.',
         'suggested_tools': ['app_read_file',
                             'app_validate',
                             'app_deploy_draft',
                             'app_run_healthcheck'],
         'related_resources': ['dash://exasol/help/dashboard-patterns',
                               'dash://exasol/help/agent-workflow',
                               'dash://meta/app-authoring-guide',
                               'dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_start',
        handler='_tool_app_start',
        title='Start an app runtime',
        description='Mount the current live revision for a hosted app.',
        input_schema='_name_schema',
        guidance={'next_step': 'Check the live route and run health checks.',
         'suggested_tools': ['app_run_healthcheck', 'app_get_status'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_stop',
        handler='_tool_app_stop',
        title='Stop an app runtime',
        description='Unmount the live route without deleting revisions.',
        input_schema='_name_schema',
        guidance={'next_step': 'Restart the app when you are ready to republish it.',
         'suggested_tools': ['app_start', 'app_get_status'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_restart',
        handler='_tool_app_restart',
        title='Restart an app runtime',
        description='Remount the current live revision for a hosted app.',
        input_schema='_name_schema',
        guidance={'next_step': 'Verify the live route is healthy after restart.',
         'suggested_tools': ['app_run_healthcheck', 'app_get_status', 'app_tail_logs'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_get_status',
        handler='_tool_app_get_status',
        title='Get app status',
        description='Return lifecycle state, revision pointers, and draft workspace state for a hosted app.',
        input_schema='_name_schema',
        guidance={'next_step': 'Use the status to decide whether to edit, deploy, or diagnose the app.',
         'suggested_tools': ['app_validate', 'app_run_healthcheck', 'app_collect_diagnostics'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_build',
        handler='_tool_app_build',
        title='Build a new immutable revision',
        description='Validate the draft workspace and create a new immutable revision with a stored source artifact. Use app_start_preview or app_promote_revision after this. force_clean only bypasses cached dependency-install state; it does not change source snapshotting.',
        input_schema='_app_build_schema',
        guidance={'next_step': 'Preview or promote the built revision.',
         'suggested_tools': ['app_start_preview', 'app_promote_revision', 'app_run_healthcheck'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_deploy_draft',
        handler='_tool_app_deploy_draft',
        title='Validate, build, and promote the draft',
        description='Run validate -> build -> deploy in one tool call. deployment_target=live promotes the revision to /apps/{name}; deployment_target=preview mounts it under /preview/{name}/{revision}. Optionally auto-rollback a live deployment if post-deploy health checks fail. force_clean only bypasses cached dependency-install state; it does not change source snapshotting.',
        input_schema='_app_deploy_draft_schema',
        guidance={'next_step': 'If the app is mounted, check the live app and health. If it is still '
                      'stopped, call app_start first.',
         'suggested_tools': ['app_get_status', 'app_start', 'app_run_healthcheck'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_start_preview',
        handler='_tool_app_start_preview',
        title='Start a preview revision',
        description='Mount a revision under /preview/{app}/{revision}.',
        input_schema='_revision_schema',
        guidance={'next_step': 'Probe the preview and then promote it if it looks correct.',
         'suggested_tools': ['app_run_healthcheck', 'app_promote_revision', 'app_tail_logs'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_promote_revision',
        handler='_tool_app_promote_revision',
        title='Promote a revision to live',
        description='Switch the live route to a built revision and retain the previous live revision for rollback. If the app runtime is currently stopped, call app_start afterwards to remount the live route. Pass require_healthy=true to refuse promoting a revision with a recorded build-preflight or data-layer failure (defaults to false: promotion is unconditional unless you opt in).',
        input_schema='_app_promote_revision_schema',
        guidance={'next_step': 'If the app runtime is running, run health checks on the live route. If the '
                      'app is stopped, call app_start to remount it first.',
         'suggested_tools': ['app_get_status', 'app_start', 'app_run_healthcheck'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_rollback',
        handler='_tool_app_rollback',
        title='Rollback the live revision',
        description='Revert the live route to the retained rollback target.',
        input_schema='_name_schema',
        guidance={'next_step': 'Confirm the rolled back live app is healthy.',
         'suggested_tools': ['app_run_healthcheck', 'app_get_status', 'app_collect_diagnostics'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_put_files',
        handler='_tool_app_put_files',
        title='Write draft files',
        description='Create or replace one or more files in the app draft workspace. Use this before app_validate.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'files': {'type': 'array',
                                  'description': 'Draft files to create or replace.',
                                  'items': {'type': 'object',
                                            'properties': {'path': {'type': 'string',
                                                                    'description': 'Workspace-relative '
                                                                                   'file path such '
                                                                                   'as app.py or '
                                                                                   'assets/theme.css.'},
                                                           'content': {'type': 'string',
                                                                       'description': 'Entire file '
                                                                                      'content to '
                                                                                      'write.'}},
                                            'required': ['path', 'content'],
                                            'additionalProperties': False}}},
         'required': ['name', 'files'],
         'additionalProperties': False},
        guidance={'next_step': 'Validate the updated draft workspace.',
         'suggested_tools': ['app_read_file', 'app_validate', 'app_patch_file'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_read_file',
        handler='_tool_app_read_file',
        title='Read a draft file',
        description='Return the current content of one draft workspace file. Use this to inspect app.py, requirements.txt, or other uploaded files before patching.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'path': {'type': 'string',
                                 'description': 'Workspace-relative file path such as app.py or '
                                                'dash-app.json.'}},
         'required': ['name', 'path'],
         'additionalProperties': False},
        guidance={'next_step': 'Patch the file or validate the draft after inspecting its contents.',
         'suggested_tools': ['app_patch_file', 'app_put_files', 'app_validate'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_diff_draft_vs_artifact',
        handler='_tool_app_diff_draft_vs_artifact',
        title='Compare draft against a built artifact',
        description='Show what differs between the current draft workspace and a built artifact. When revision_number is omitted, the tool compares against the latest built revision.',
        input_schema='_app_diff_draft_vs_artifact_schema',
        guidance={'next_step': 'This tool only reports changed/unchanged status and byte counts per file. '
                      'For the actual line-level diff content, read the dash://apps/{name}/diff/... '
                      'resource for the same comparison.',
         'suggested_tools': ['app_read_file', 'app_build', 'app_patch_file'],
         'related_resources': ['dash://apps/{app}/diff/latest-build...draft', 'dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_patch_file',
        handler='_tool_app_patch_file',
        title='Patch a draft file',
        description='Apply a search/replace patch to one file in the app draft workspace and return a compact line-context preview of the updated file.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'path': {'type': 'string',
                                 'description': 'Workspace-relative file path to patch.'},
                        'search': {'type': 'string', 'description': 'Exact text to search for.'},
                        'replace': {'type': 'string', 'description': 'Replacement text.'},
                        'replace_all': {'type': 'boolean',
                                        'description': 'Replace every match when true. Defaults to '
                                                       'false.'}},
         'required': ['name', 'path', 'search', 'replace'],
         'additionalProperties': False},
        guidance={'next_step': 'Review the patch preview, then validate the updated draft workspace.',
         'suggested_tools': ['app_validate', 'app_patch_file', 'app_put_files'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_delete_file',
        handler='_tool_app_delete_file',
        title='Delete a draft file',
        description='Delete a non-required file from the app draft workspace.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string'}, 'path': {'type': 'string'}},
         'required': ['name', 'path'],
         'additionalProperties': False},
        guidance={'next_step': 'Validate the updated draft workspace.',
         'suggested_tools': ['app_validate', 'app_put_files', 'app_deploy_draft'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_validate',
        handler='_tool_app_validate',
        title='Validate a draft workspace',
        description=(
            'Run manifest, dependency, lint, syntax, import, callback, and '
            'credential-safety validation on the current draft workspace. Use this '
            'before app_build or app_deploy_draft. This is a static/offline check: it '
            'does not verify that SQL in queries/*.sql references real schemas, '
            'tables, or columns against a live Exasol connection. A nonexistent-column '
            'typo passes app_validate as is_valid: true and is only caught later, by '
            'app_build\'s sql_smoke preflight - run app_build early if you want that '
            'check sooner.'
        ),
        input_schema='_name_schema',
        guidance={'next_step': 'Fix any reported issues in the draft, or build a revision when validation '
                      'passes.',
         'suggested_tools': ['app_patch_file', 'app_build', 'app_read_file'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_collect_diagnostics',
        handler='_tool_app_collect_diagnostics',
        title='Collect diagnostics',
        description='Return lifecycle, health, logs, latest errors, validation results, and recovery suggestions.',
        input_schema='_name_schema',
        guidance={'next_step': 'Use the latest error and validation report to decide the next patch.',
         'suggested_tools': ['app_patch_file', 'app_put_files', 'app_validate'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_inspect_traceback',
        handler='_tool_app_inspect_traceback',
        title='Inspect a traceback',
        description="Parse and classify a provided traceback, or inspect the app's latest captured traceback.",
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string'}, 'traceback_text': {'type': 'string'}},
         'required': ['name'],
         'additionalProperties': False},
        guidance={'next_step': 'Patch the failing code path and validate again.',
         'suggested_tools': ['app_patch_file', 'app_validate', 'app_collect_diagnostics'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_tail_logs',
        handler='_tool_app_tail_logs',
        title='Tail app logs',
        description='Return recent log entries from the latest, build, runtime, or health log channels.',
        input_schema='_app_tail_logs_schema',
        guidance={'next_step': 'Use the logs to decide whether to patch the app or inspect diagnostics.',
         'suggested_tools': ['app_collect_diagnostics', 'app_patch_file', 'app_validate'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_run_healthcheck',
        handler='_tool_app_run_healthcheck',
        title='Run app health checks',
        description=(
            'Probe the mounted live or preview route, layout endpoint, dependencies '
            'endpoint, static assets, and (for Exasol-backed apps) each queries/*.sql '
            'file via sql_smoke. A "healthy" result does not mean any @app.callback has '
            'actually executed: sql_smoke runs SQL files directly, independent of '
            'app.py, and the other probes are plain GETs. To force a real callback run, '
            'POST to {mount_path}/_dash-update-component - see '
            'dash://meta/app-authoring-guide triggering_callbacks_for_real for the '
            'exact request shape.'
        ),
        input_schema='_app_healthcheck_schema',
        guidance={'next_step': 'Inspect any failed probes before changing the live revision.',
         'suggested_tools': ['app_collect_diagnostics', 'app_tail_logs', 'app_get_status'],
         'related_resources': ['dash://meta/workflows', 'dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_session_eval_js',
        handler='_tool_app_session_eval_js',
        title='Run JavaScript in a live dashboard tab',
        description=(
            'Evaluate ephemeral JavaScript inside the browser session a user currently has '
            'open, and return a bounded result. This is the only way to read interaction '
            'state — current dropdown values, dcc.Store contents, client-side Plotly '
            'zoom/selection, what is actually visible — because Dash keeps that in the '
            'browser, not on the server. Use the injected `ctx` helpers rather than raw DOM '
            'work: ctx.props(ids), ctx.dom(ids), ctx.plots(), ctx.stores(), ctx.page(), '
            'ctx.setProps(id, props), ctx.waitForIdle(ms), ctx.summarize(value). A trailing '
            'expression is returned and `await` is allowed, so one call can set a filter, '
            'wait for the app to settle, and report the result. Read '
            'dash://meta/session-channel-guide for the full reference and recipes. Local '
            'mode only; a page-side exception comes back as ok=false with the failing line '
            'relative to the code you submitted.'
        ),
        input_schema='_app_session_eval_js_schema',
        guidance={'next_step': 'Read the returned value; if a component was missing, list sessions or widen the id list.',
         'suggested_tools': ['app_sessions_list', 'app_session_eval_js', 'app_tail_logs'],
         'related_resources': ['dash://meta/session-channel-guide', 'dash://apps/{app}/sessions']},
    ),
    ToolSpec(
        name='app_sessions_list',
        handler='_tool_app_sessions_list',
        title='List live dashboard browser sessions',
        description=(
            'List the browser tabs currently attached to hosted dashboards, newest poll '
            'first, with liveness and the prop-access tier each page reported. Use it to '
            'pick a session_id for app_session_eval_js, or to confirm the user actually has '
            'the dashboard open. Local mode only.'
        ),
        input_schema='_app_sessions_list_schema',
        guidance={'next_step': 'Pick a live session and evaluate JavaScript in it.',
         'suggested_tools': ['app_session_eval_js', 'app_get_status'],
         'related_resources': ['dash://meta/session-channel-guide']},
    ),
    ToolSpec(
        name='app_runtime_workers_list',
        handler='_tool_app_runtime_workers_list',
        title='List runtime workers and baselines',
        description='Return the in-process snapshot of out-of-process workers and forkserver baselines, including aggregate RSS and p50 cold-start time. Available in isolated runtime mode.',
        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
        guidance={'next_step': 'Restart an unhealthy worker or inspect its logs.',
         'suggested_tools': ['app_runtime_workers_restart', 'app_tail_logs', 'app_get_status'],
         'related_resources': ['dash://runtime/status']},
    ),
    ToolSpec(
        name='app_runtime_workers_restart',
        handler='_tool_app_runtime_workers_restart',
        title='Restart a runtime worker',
        description='Stop the worker at mount_path and re-spawn it from the persisted spec. Available in isolated runtime mode.',
        input_schema={'type': 'object',
         'properties': {'mount_path': {'type': 'string',
                                       'description': 'Absolute mount path (e.g. /apps/sales).'}},
         'required': ['mount_path'],
         'additionalProperties': False},
        guidance={'next_step': 'Confirm the restarted worker serves the app again.',
         'suggested_tools': ['app_run_healthcheck', 'app_runtime_workers_list'],
         'related_resources': ['dash://runtime/status']},
    ),
    ToolSpec(
        name='app_environment_invalidate',
        handler='_tool_app_environment_invalidate',
        title='Invalidate a per-app environment',
        description='Mark a per-app dependency environment for removal on the next GC pass. Available in per_app dependency-isolation mode.',
        input_schema={'type': 'object',
         'properties': {'environment_id': {'type': 'string',
                                           'description': 'Environment id (sha256:…) from '
                                                          'dash://runtime/environments.'}},
         'required': ['environment_id'],
         'additionalProperties': False},
        guidance={'next_step': 'Rebuild or restart the app so a fresh dependency environment is '
                      'provisioned.',
         'suggested_tools': ['app_build', 'app_restart', 'app_get_status'],
         'related_resources': ['dash://runtime/status']},
    ),
    ToolSpec(
        name='app_acknowledge_data_layer_errors',
        handler='_tool_app_acknowledge_data_layer_errors',
        title='Acknowledge data-layer errors',
        description='Reset the `data_layer` healthcheck probe by acknowledging all currently recorded Exasol query failures. Use after fixing SQL in-place without promoting a new revision; the underlying `dash://apps/{name}/errors` ledger is preserved, but the probe and `app_collect_diagnostics` both filter past the new watermark.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string',
                                 'description': 'The hosted app to acknowledge errors for.'}},
         'required': ['name'],
         'additionalProperties': False},
        guidance={'next_step': 'Re-run the health check to confirm the data-layer probe is clean.',
         'suggested_tools': ['app_run_healthcheck', 'app_collect_diagnostics'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_list_files',
        handler='_tool_app_list_files',
        app_capability=DASHBOARD_EDIT_DRAFT,
        enforce_in_handler=True,
        title='List draft files',
        description='List every editable file in the app draft workspace.',
        input_schema='_name_schema',
        guidance={'next_step': 'Read or patch one of the listed draft files.',
         'suggested_tools': ['app_read_file', 'app_patch_file', 'app_validate'],
         'related_resources': ['dash://meta/app-authoring-guide']},
    ),
    ToolSpec(
        name='app_delete',
        handler='_tool_app_delete',
        app_capability=DASHBOARD_DELETE,
        enforce_in_handler=True,
        title='Delete a hosted app',
        description='Permanently remove an app from the active runtime, catalog, draft workspace, local artifacts, sharing state, and current GitOps branch. Published source remains recoverable from Git history. confirmation must exactly equal name.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'confirmation': {'type': 'string',
                                         'description': 'Must exactly match name to confirm '
                                                        'destructive deletion.'}},
         'required': ['name', 'confirmation'],
         'additionalProperties': False},
        guidance={'next_step': 'Confirm the app no longer appears in the catalog or app inventory.',
         'suggested_tools': ['apps_list'],
         'related_resources': ['dash://apps', 'dash://repo/status']},
    ),
    ToolSpec(
        name='app_share_get',
        handler='_tool_app_share_get',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Get app sharing policy',
        description='[hosted-mode] Return the app share policy, active grants, revoked grants, and sharing warnings.',
        input_schema='_name_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': 'Grant, revoke, or explain access based on the current grants.',
         'suggested_tools': ['app_share_grant', 'app_share_revoke', 'app_share_explain_access'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_grant',
        handler='_tool_app_share_grant',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Grant app access',
        description='[hosted-mode] Grant viewer, preview_viewer, editor, or owner access to a user, group, domain, organization, or public principal.',
        input_schema='_app_share_grant_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': "Confirm the grant appears in the app's sharing state.",
         'suggested_tools': ['app_share_get', 'app_share_explain_access'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_revoke',
        handler='_tool_app_share_revoke',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Revoke app access',
        description='[hosted-mode] Revoke one sharing grant by grant_id, or revoke active grants matching a principal.',
        input_schema='_app_share_revoke_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': "Confirm the principal no longer appears in the app's sharing state.",
         'suggested_tools': ['app_share_get', 'app_share_explain_access'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_set_link_scope',
        handler='_tool_app_share_set_link_scope',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Set app link scope',
        description='[hosted-mode] Set the app-level sharing policy to restricted, organization, domain, anyone_with_link, or public. Public anonymous access also requires server tenant policy.',
        input_schema='_app_share_set_link_scope_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': "Verify the link scope change with the app's sharing state.",
         'suggested_tools': ['app_share_get', 'app_share_create_one_time_link'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_explain_access',
        handler='_tool_app_share_explain_access',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Explain app access',
        description='[hosted-mode] Explain whether a current or specified principal can access the live or preview dashboard and which grant or policy matched.',
        input_schema='_app_share_explain_access_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': 'Adjust grants if the explained decision does not match expectations.',
         'suggested_tools': ['app_share_grant', 'app_share_revoke', 'app_share_get'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_create_one_time_link',
        handler='_tool_app_share_create_one_time_link',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Create a one-time sharing link',
        description='[hosted-mode] Create a single-use, manually shared dashboard access link. The raw token is returned only in the tool response and only a hash is stored.',
        input_schema='_app_share_create_one_time_link_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': 'Deliver the display-once link; revoke it if it should no longer grant '
                      'access.',
         'suggested_tools': ['app_share_revoke_one_time_link', 'app_share_get'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_share_revoke_one_time_link',
        handler='_tool_app_share_revoke_one_time_link',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Revoke a one-time sharing link',
        description='[hosted-mode] Revoke a manually shared one-time link and any link-derived ACL grant created by redemption.',
        input_schema='_app_share_revoke_one_time_link_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': "Confirm the link no longer appears in the app's sharing state.",
         'suggested_tools': ['app_share_get'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_invite_external_user',
        handler='_tool_app_invite_external_user',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Invite an external user',
        description='[hosted-mode] Create a hashed-token email invitation for an external user. The raw accept token is returned only once; manual email delivery is used until a sender integration is configured.',
        input_schema='_app_invite_external_user_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': 'Track invitation delivery and resend or revoke as needed.',
         'suggested_tools': ['app_share_get', 'app_revoke_external_invitation'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_revoke_external_invitation',
        handler='_tool_app_revoke_external_invitation',
        app_capability=DASHBOARD_MANAGE_SHARING,
        enforce_in_handler=True,
        title='Revoke an external invitation',
        description='[hosted-mode] Revoke a pending or accepted external invitation and revoke the accepted grant when present.',
        input_schema='_app_revoke_external_invitation_schema',
        meta={'availability': 'hosted'},
        guidance={'next_step': "Confirm the invitation is revoked in the app's sharing state.",
         'suggested_tools': ['app_share_get'],
         'related_resources': ['dash://meta/workflows']},
    ),
    ToolSpec(
        name='app_outputs_list',
        handler='_tool_app_outputs_list',
        app_capability=DASHBOARD_EXPORT,
        title='List registered outputs',
        description='List governed dataset and view outputs declared by the current live revision, including parameter schemas, effective formats, limits, and policy decisions.',
        input_schema='_name_schema',
        guidance={'next_step': 'Inspect one registered output or queue an available CSV export.',
         'suggested_tools': ['app_output_get', 'app_export_create', 'app_exports_list'],
         'related_resources': ['dash://apps/{name}/outputs']},
    ),
    ToolSpec(
        name='app_output_get',
        handler='_tool_app_output_get',
        app_capability=DASHBOARD_EXPORT,
        title='Get a registered output',
        description='Inspect one governed output declared by the current live revision.',
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'output_id': {'type': 'string',
                                      'description': 'Stable output id from app_outputs_list.'}},
         'required': ['name', 'output_id'],
         'additionalProperties': False},
        guidance={'next_step': 'Queue a CSV export when its format availability is executable.',
         'suggested_tools': ['app_export_create', 'app_outputs_list'],
         'related_resources': ['dash://apps/{name}/outputs']},
    ),
    ToolSpec(
        name='app_export_create',
        handler='_tool_app_export_create',
        app_capability=DASHBOARD_EXPORT,
        title='Create dataset export',
        description=(
            'Queue a governed export from a registered output on the current live '
            'revision. Accepts any format the consumption contract supports '
            '(csv, xlsx for dataset outputs; pdf, png, pptx for view outputs), but '
            "only dataset formats are currently executable - a view output's formats "
            'are structurally valid here yet always fail with '
            'consumption_format_unavailable (reason renderer_not_available) until view '
            'rendering ships. Check app_outputs_list\'s per-format '
            'policy.format_availability before queuing to see which formats are '
            'actually executable right now.'
        ),
        input_schema={'type': 'object',
         'properties': {'name': {'type': 'string', 'description': 'Hosted app name.'},
                        'output_id': {'type': 'string',
                                      'description': 'Registered dataset output id.'},
                        'format': {'type': 'string', 'enum': ['csv', 'xlsx', 'pdf', 'png', 'pptx']},
                        'parameters': {'type': 'object',
                                       'description': 'Values allowed by the output parameter '
                                                      'schema.'},
                        'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
         'required': ['name', 'output_id', 'format', 'parameters'],
         'additionalProperties': False},
        guidance={'next_step': 'Poll the queued job until it succeeds, fails, or is cancelled.',
         'suggested_tools': ['export_get', 'export_cancel'],
         'related_resources': ['dash://exports/{job_id}']},
    ),
    ToolSpec(
        name='app_exports_list',
        handler='_tool_app_exports_list',
        app_capability=DASHBOARD_EXPORT,
        title='List personal exports',
        description="List the caller's recent export jobs for one app.",
        input_schema='_name_schema',
        guidance={'next_step': 'Inspect a job or create a download link for a completed export.',
         'suggested_tools': ['export_get', 'export_download_link_create'],
         'related_resources': ['dash://exports/{job_id}']},
    ),
    ToolSpec(
        name='app_exports_admin_list',
        handler='_tool_app_exports_admin_list',
        app_capability=DASHBOARD_MANAGE_CONSUMPTION,
        title='List app-wide exports (owner/admin)',
        description="List every principal's export jobs for one app with redacted parameter summaries. Requires the dashboard.manage_consumption capability.",
        input_schema='_name_schema',
        guidance={'next_step': 'Inspect or cancel a problem job surfaced by the app-wide view.',
         'suggested_tools': ['export_get', 'export_cancel'],
         'related_resources': ['dash://exports/{job_id}']},
    ),
    ToolSpec(
        name='export_get',
        handler='_tool_export_get',
        app_capability=DASHBOARD_EXPORT,
        job_scoped=True,
        title='Get export job',
        description='Read principal-bound export status and bounded artifact metadata.',
        input_schema='_job_id_schema',
        guidance={'next_step': 'Create a download link after success, or inspect the structured failure.',
         'suggested_tools': ['export_get', 'export_cancel', 'export_download_link_create'],
         'related_resources': ['dash://exports/{job_id}']},
    ),
    ToolSpec(
        name='export_cancel',
        handler='_tool_export_cancel',
        app_capability=DASHBOARD_EXPORT,
        job_scoped=True,
        title='Cancel export job',
        description="Request cancellation of the caller's queued or running export.",
        input_schema='_job_id_schema',
        guidance={'next_step': 'Poll the job until cancellation reaches a terminal state.',
         'suggested_tools': ['export_get'],
         'related_resources': ['dash://exports/{job_id}']},
    ),
    ToolSpec(
        name='export_download_link_create',
        handler='_tool_export_download_link_create',
        app_capability=DASHBOARD_EXPORT,
        job_scoped=True,
        title='Create export download link',
        description='Create a short-lived authenticated URL for a completed export artifact.',
        input_schema='_job_id_schema',
        guidance={'next_step': 'Open the authenticated, expiring URL to download the CSV artifact.',
         'suggested_tools': ['export_get'],
         'related_resources': ['dash://exports/{job_id}']},
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
