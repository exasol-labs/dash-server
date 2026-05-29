# MCP Reference

This is the practical reference for the current `dash-server` MCP surface.

> The tool and resource lists below are **auto-generated** from the live MCP
> registry by `scripts/generate_mcp_reference.py`. CI fails on drift (see
> `tests/test_mcp_reference_doc.py`); edit the generator, not the lists, then
> run `python scripts/generate_mcp_reference.py` to refresh.

## Main Tools

<!-- BEGIN: auto-tools -->
_41 tools registered. Each `tools/call` request must pass the tool name and the arguments defined by its `inputSchema`._

- **`app_acknowledge_data_layer_errors`** — Reset the `data_layer` healthcheck probe by acknowledging all currently recorded Exasol query failures. Use after fixing SQL in-place without promoting a new revision; the underlying `dash://apps/{name}/errors` ledger is preserved, but the probe and `app_collect_diagnostics` both filter past the new watermark.
- **`app_build`** — Validate the draft workspace and create a new immutable revision with a stored source artifact. Use app_start_preview or app_promote_revision after this. force_clean only bypasses cached dependency-install state; it does not change source snapshotting.
- **`app_collect_diagnostics`** — Return lifecycle, health, logs, latest errors, validation results, and recovery suggestions.
- **`app_create`** — Create a starter hosted app from metadata only. Use template=metric-cards for a generic static starter, or template=exasol-analytics only when you are intentionally creating a profile-bound Exasol scaffold. If you have source files, use app_create_from_files.
- **`app_create_exasol_dashboard`** — Generate an Exasol-backed exasol-analytics scaffold from a validated profile and create it as a hosted app. This is the preferred Exasol path because the hosted app only stores a profile reference and the server supplies credentials. The default analytics-hub pattern creates a multi-tab app with system health, query history, and a business analytics placeholder.
- **`app_create_from_files`** — Create a hosted app and seed its draft workspace from explicit files. Use this when you already have app.py, requirements.txt, or assets. template=metric-cards means a generic starter manifest; template=exasol-analytics means the files should follow the Exasol SQL-helper scaffold shape. Do not embed Exasol credentials or direct pyexasol.connect(...) code in uploaded files; use server-side Exasol profiles instead.
- **`app_delete_file`** — Delete a non-required file from the app draft workspace.
- **`app_deploy_draft`** — Run validate -> build -> deploy in one tool call. deployment_target=live promotes the revision to /apps/{name}; deployment_target=preview mounts it under /preview/{name}/{revision}. Optionally auto-rollback a live deployment if post-deploy health checks fail. force_clean only bypasses cached dependency-install state; it does not change source snapshotting.
- **`app_diff_draft_vs_artifact`** — Show what differs between the current draft workspace and a built artifact. When revision_number is omitted, the tool compares against the latest built revision.
- **`app_environment_invalidate`** — Mark a per-app dependency environment for removal on the next GC pass. Available in per_app dependency-isolation mode.
- **`app_get_status`** — Return lifecycle state, revision pointers, and draft workspace state for a hosted app.
- **`app_inspect_traceback`** — Parse and classify a provided traceback, or inspect the app's latest captured traceback.
- **`app_invite_external_user`** — [hosted-mode] Create a hashed-token email invitation for an external user. The raw accept token is returned only once; manual email delivery is used until a sender integration is configured.
- **`app_patch_file`** — Apply a search/replace patch to one file in the app draft workspace and return a compact line-context preview of the updated file.
- **`app_promote_revision`** — Switch the live route to a built revision and retain the previous live revision for rollback. If the app runtime is currently stopped, call app_start afterwards to remount the live route.
- **`app_put_files`** — Create or replace one or more files in the app draft workspace. Use this before app_validate.
- **`app_read_file`** — Return the current content of one draft workspace file. Use this to inspect app.py, requirements.txt, or other uploaded files before patching.
- **`app_restart`** — Remount the current live revision for a hosted app.
- **`app_revoke_external_invitation`** — [hosted-mode] Revoke a pending or accepted external invitation and revoke the accepted grant when present.
- **`app_rollback`** — Revert the live route to the retained rollback target.
- **`app_run_healthcheck`** — Probe the mounted live or preview route, layout endpoint, dependencies endpoint, and static assets.
- **`app_runtime_workers_list`** — Return the in-process snapshot of out-of-process workers and forkserver baselines, including aggregate RSS and p50 cold-start time. Available in isolated runtime mode.
- **`app_runtime_workers_restart`** — Stop the worker at mount_path and re-spawn it from the persisted spec. Available in isolated runtime mode.
- **`app_scaffold_from_schema`** — Introspect Exasol catalog metadata for a profile, choose analytically useful columns and relationship hints, and generate a tailored exasol-analytics scaffold with business SQL wired to the selected schema and table.
- **`app_share_create_one_time_link`** — [hosted-mode] Create a single-use, manually shared dashboard access link. The raw token is returned only in the tool response and only a hash is stored.
- **`app_share_explain_access`** — [hosted-mode] Explain whether a current or specified principal can access the live or preview dashboard and which grant or policy matched.
- **`app_share_get`** — [hosted-mode] Return the app share policy, active grants, revoked grants, and sharing warnings.
- **`app_share_grant`** — [hosted-mode] Grant viewer, preview_viewer, editor, or owner access to a user, group, domain, organization, or public principal.
- **`app_share_revoke`** — [hosted-mode] Revoke one sharing grant by grant_id, or revoke active grants matching a principal.
- **`app_share_revoke_one_time_link`** — [hosted-mode] Revoke a manually shared one-time link and any link-derived ACL grant created by redemption.
- **`app_share_set_link_scope`** — [hosted-mode] Set the app-level sharing policy to restricted, organization, domain, anyone_with_link, or public. Public anonymous access also requires server tenant policy.
- **`app_start`** — Mount the current live revision for a hosted app.
- **`app_start_preview`** — Mount a revision under /preview/{app}/{revision}.
- **`app_stop`** — Unmount the live route without deleting revisions.
- **`app_tail_logs`** — Return recent log entries from the latest, build, runtime, or health log channels.
- **`app_validate`** — Run manifest, dependency, lint, syntax, import, callback, and credential-safety validation on the current draft workspace. Use this before app_build or app_deploy_draft.
- **`apps_list`** — Return the current hosted app inventory from the SQLite registry.
- **`exasol_profile_create_local`** — Create one local Exasol profile for a single-user workflow. Provide either secret_value or secret_env_var so secrets stay outside Git.
- **`exasol_profile_validate`** — Resolve the configured secret, load pyexasol, and run a connection test.
- **`exasol_profiles_list`** — Return Git-tracked Exasol profile metadata without secret values.
- **`repo_reconcile`** — Read desired-state manifests from the GitOps repository and apply them to the observed runtime and cache state.
<!-- END: auto-tools -->

## Main Resources

<!-- BEGIN: auto-resources -->
_17 server-wide resources plus the per-app pattern below ({{app}} matches any registered app name)._

### Server-wide

- **`dash://apps`** — Inventory of the currently registered Dash apps.
- **`dash://exasol/help/agent-workflow`** — Recommended separation of responsibilities between dash-server and an external Exasol MCP server.
- **`dash://exasol/help/connection-modes`** — Phase 0 local Exasol connection modes, required fields, and the recommended dashboard workflow.
- **`dash://exasol/help/dashboard-patterns`** — Built-in Exasol dashboard scaffold patterns and when to use them.
- **`dash://exasol/help/sql-placeholders`** — pyexasol placeholder grammar ({name!s}, {name!d}, etc.) for parameterized dashboard SQL. Replaces SQL-driver :name syntax which Exasol rejects.
- **`dash://exasol/profiles`** — Git-tracked Exasol profile metadata without secrets.
- **`dash://meta/app-authoring-guide`** — Recommended create_dash_app factory structure, prefix rules, and common mistakes.
- **`dash://meta/app-create-from-files-schema`** — Required fields, common mistakes, and a working example for app_create_from_files.
- **`dash://meta/app-create-schema`** — Required bundle shape, common mistakes, and a working example for app_create.
- **`dash://meta/workflows`** — Canonical tool sequences for creating, editing, validating, and deploying hosted Dash apps.
- **`dash://repo/desired-state`** — Authoritative live and preview deployment intent parsed from the GitOps repository.
- **`dash://repo/drift`** — Comparison between Git desired state and the observed runtime and cache state.
- **`dash://repo/status`** — Read-only status for the local GitOps repository, including draft worktrees and current runtime-isolation settings.
- **`dash://runtime/environments`** — Inventory of materialized per-app envs, disk usage, and wheel-cache size (per_app dependency mode).
- **`dash://runtime/logs/runtime.events`** — Server-wide audit log of operational decisions: env_evicted, wheel_cache_pruned, wheel_cache_gc_skipped, unsafe_override_warning.
- **`dash://runtime/status`** — Current APP_DEPENDENCY_ISOLATION and APP_RUNTIME_MODE settings plus the cache and worker config knobs.
- **`dash://runtime/workers`** — Snapshot of out-of-process workers, baselines, RSS totals, and p50 cold-start time (isolated runtime mode).

### Per-app (`dash://apps/{app}/…`)

- **`dash://apps/{app}`** — Current app overview including exposure, runtime, and revision pointers.
- **`dash://apps/{app}/artifacts/latest/files`** — List of source files present in the latest built artifact revision.
- **`dash://apps/{app}/callback-failures`** — Structured callback error records captured for the app.
- **`dash://apps/{app}/dependency-report`** — Declared requirements, invalid requirement entries, and install-plan notes for the draft workspace.
- **`dash://apps/{app}/diff/current...draft`** — Unified diff between the current live revision artifact and the draft workspace.
- **`dash://apps/{app}/diff/latest-build...draft`** — Unified diff and per-file comparison between the latest built artifact and the draft workspace.
- **`dash://apps/{app}/errors`** — Structured build and runtime errors captured for the app.
- **`dash://apps/{app}/events`** — Event log for revision build, preview, promote, rollback, and workspace edits.
- **`dash://apps/{app}/files`** — List of editable draft files in the app workspace.
- **`dash://apps/{app}/health`** — Structured health probe results for the live app route.
- **`dash://apps/{app}/logs/build`** — Recent build, validation, and workspace-edit log entries.
- **`dash://apps/{app}/logs/latest`** — Recent log entries aggregated across runtime, build, and health channels.
- **`dash://apps/{app}/logs/runtime`** — Recent runtime mount and lifecycle log entries.
- **`dash://apps/{app}/manifest`** — Current manifest for the app's live revision.
- **`dash://apps/{app}/permissions`** — Declared filesystem, network, and env permissions for the app.
- **`dash://apps/{app}/revisions`** — Immutable revisions for the app.
- **`dash://apps/{app}/routes`** — Live and preview route bindings for the app.
- **`dash://apps/{app}/sharing`** — Share policy, active ACL grants, revoked ACL grants, and warnings.
- **`dash://apps/{app}/status`** — Lifecycle state, runtime mount state, revision pointers, and draft workspace state.
<!-- END: auto-resources -->

## Example: Create an App

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": "app_create",
      "arguments": {
        "bundle": {
          "manifest": {
            "name": "support",
            "title": "Support Dashboard v1",
            "route": "/apps/support",
            "description": "Support dashboard created through dash-server.",
            "template": "metric-cards"
          },
          "dashboard": {
            "headline": "Support Dashboard v1",
            "summary": "Initial live revision.",
            "metrics": [
              {"label": "Revenue", "value": "$640K"},
              {"label": "Conversion", "value": "4.8%"}
            ]
          }
        }
      }
    }
  }'
```

## Example: Diagnose a Broken Draft

Break the draft:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 5,
    "method": "tools/call",
    "params": {
      "name": "app_patch_file",
      "arguments": {
        "name": "support",
        "path": "app.py",
        "search": "from dash import Dash, Input, Output, dcc, html",
        "replace": "from totally_missing_package import Dash, Input, Output, dcc, html"
      }
    }
  }'
```

Collect diagnostics:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 6,
    "method": "tools/call",
    "params": {
      "name": "app_collect_diagnostics",
      "arguments": {
        "name": "support"
      }
    }
  }'
```

Inspect recent errors:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 7,
    "method": "resources/read",
    "params": {
      "uri": "dash://apps/support/errors"
    }
  }'
```

## Example: Force a Fresh Dependency Install During Build

Use `force_clean` only when you need to bypass cached dependency-install state. It does not force a new source snapshot or reset local module import isolation.

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 8,
    "method": "tools/call",
    "params": {
      "name": "app_build",
      "arguments": {
        "name": "support",
        "force_clean": true
      }
    }
  }'
```

## Notes

- The authoritative source of deployment intent is Git.
- SQLite is the rebuildable local projection used for fast reads and runtime-friendly lookup.
- `app_build` accepts `force_clean` to bypass cached dependency-install state before validation/build. It does not change source snapshotting.
- `app_deploy_draft` accepts `deployment_target: "live"` (default) or `deployment_target: "preview"`, can auto-rollback a live deploy when post-deploy health checks fail, and also accepts the same narrow `force_clean` dependency-cache bypass. The argument name is `deployment_target`, not `target` — and as of the 0.6 release, unknown arguments are rejected rather than silently ignored.
- `app_run_healthcheck` can probe either the live route or the current preview route.
- `metric-cards` is the generic starter template; `exasol-analytics` is the profile-bound Exasol scaffold with SQL helper files.
- For code-level architecture, use [architecture.md](architecture.md).
