"""MCP tool handler methods grouped by domain, plus their shared helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import secrets
from typing import Any

from flask import current_app, has_request_context

from dash_server.auth import AuthContext, Principal, current_auth_context
from dash_server.consumption import ConsumptionService
from dash_server.constants import GRANT_SCOPES, PRINCIPAL_TYPES
from dash_server.exasol import ExasolDashboardService
from dash_server.exceptions import DashServerError
from dash_server.mailer import InvitationEmailSender
from dash_server.registry.models import Invitation, ShareLink


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
        + _len(report.get("consumption"), "issues")
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



class HandlersMixin:
    """Tool handlers plus the argument/service helpers they share."""

    def _tool_input_schema(self, tool_name: str) -> dict[str, Any] | None:
        if not hasattr(self, "_tool_schemas_cache"):
            self._tool_schemas_cache = {
                tool["name"]: tool.get("inputSchema")
                for tool in self._tool_definitions()
                if isinstance(tool.get("inputSchema"), dict)
            }
        return self._tool_schemas_cache.get(tool_name)


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
            "consumption",
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


    def _tool_app_list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # Capability enforced centrally via ToolSpec(enforce_in_handler=True).
        name = self._require_name(arguments)
        listed = self.runtime_service.list_workspace_files(name)
        return self._tool_result(
            "app_list_files",
            text=f"Listed {len(listed['draft']['files'])} draft file(s) for app {listed['app']['name']}.",
            structured_content=listed,
        )


    def _tool_app_outputs_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        payload = self._consumption_service().list_outputs(
            name,
            self._consumption_auth_context(),
        )
        return self._tool_result(
            "app_outputs_list",
            text=f"Listed {payload['output_count']} registered output(s) for app {name}.",
            structured_content=payload,
        )


    def _tool_app_output_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        output_id = self._require_string(arguments.get("output_id"), "output_id")
        payload = self._consumption_service().get_output(
            name,
            output_id,
            self._consumption_auth_context(),
        )
        return self._tool_result(
            "app_output_get",
            text=f"Read registered output {output_id} for app {name}.",
            structured_content=payload,
        )


    def _tool_app_export_create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        output_id = self._require_string(arguments.get("output_id"), "output_id")
        requested_format = self._require_string(arguments.get("format"), "format")
        parameters = arguments.get("parameters", {})
        if not isinstance(parameters, dict):
            raise DashServerError(
                category="consumption_parameter_validation_error",
                summary="Export parameters must be an object.",
                details={"field": "parameters"},
            )
        idempotency_key = arguments.get("idempotency_key")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise DashServerError(
                category="consumption_idempotency_key_invalid",
                summary="idempotency_key must be a string.",
                details={"field": "idempotency_key"},
            )
        payload = self._consumption_service().create_export(
            name,
            output_id,
            requested_format,
            parameters,
            self._consumption_auth_context(),
            idempotency_key=idempotency_key,
        )
        return self._tool_result(
            "app_export_create",
            text=f"Queued export {payload['job']['id']} for app {name}.",
            structured_content=payload,
        )


    def _tool_app_exports_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        payload = self._consumption_service().list_exports(
            self._consumption_auth_context(), app_name=name
        )
        return self._tool_result(
            "app_exports_list",
            text=f"Listed {payload['job_count']} export job(s) for app {name}.",
            structured_content=payload,
        )


    def _tool_app_exports_admin_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        payload = self._consumption_service().list_app_jobs(
            name, self._consumption_auth_context()
        )
        return self._tool_result(
            "app_exports_admin_list",
            text=f"Listed {payload['job_count']} app-wide export job(s) for app {name}.",
            structured_content=payload,
        )


    def _tool_export_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_string(arguments.get("job_id"), "job_id")
        payload = self._consumption_service().get_export(
            job_id, self._consumption_auth_context()
        )
        return self._tool_result(
            "export_get",
            text=f"Export {job_id} is {payload['job']['status']}.",
            structured_content=payload,
        )


    def _tool_export_cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_string(arguments.get("job_id"), "job_id")
        payload = self._consumption_service().cancel_export(
            job_id, self._consumption_auth_context()
        )
        return self._tool_result(
            "export_cancel",
            text=f"Export {job_id} cancellation state is {payload['job']['status']}.",
            structured_content=payload,
        )


    def _tool_export_download_link_create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = self._require_string(arguments.get("job_id"), "job_id")
        payload = self._consumption_service().create_download_link(
            job_id, self._consumption_auth_context()
        )
        return self._tool_result(
            "export_download_link_create",
            text=f"Created a principal-bound download link for export {job_id}.",
            structured_content=payload,
        )


    def _tool_app_delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = self._require_name(arguments)
        confirmation = self._require_string(arguments.get("confirmation"), "confirmation")
        if confirmation != name:
            raise DashServerError(
                category="app_delete_confirmation_error",
                summary="App deletion confirmation must exactly match the app name.",
                details={"app": name, "confirmation": confirmation},
            )
        # Capability enforced centrally via ToolSpec(enforce_in_handler=True).
        deleted = self.runtime_service.delete_app(name)
        return self._tool_result(
            "app_delete",
            text=(
                f"Deleted app {name}. Published source remains recoverable from Git history at "
                f"commit {deleted['recovery']['deletion_commit']}."
            ),
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
            # "link" principals come from share links, not direct grants.
            allowed=PRINCIPAL_TYPES - {"link"},
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
            allowed=set(GRANT_SCOPES),
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
                "grant_id": grant.id,
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": role,
                "scope": scope,
            },
        )
        return self._tool_result(
            "app_share_grant",
            text=f"Granted {role} {scope} access on app {name} to {principal_type}:{principal_id}.",
            structured_content={"app": app.to_dict(), "grant": grant.to_dict()},
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
                    "grant_id": grant.id,
                    "principal_type": grant.principal_type,
                    "principal_id": grant.principal_id,
                },
            )
        return self._tool_result(
            "app_share_revoke",
            text=f"Revoked {len(revoked)} sharing grant(s) for app {name}.",
            structured_content={
                "app": app.to_dict(),
                "revoked_grants": [grant.to_dict() for grant in revoked],
            },
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
        allowed_domain = None
        if link_scope == "domain":
            allowed_domain = self._normalize_allowed_domain(
                arguments.get("allowed_domain"),
                tool_name="app_share_set_link_scope",
            )
        elif arguments.get("allowed_domain") is not None:
            raise self._field_error(
                "app_share_set_link_scope",
                "allowed_domain",
                "is only valid when link_scope is domain.",
            )
        app = self._require_existing_app(name, tool_name="app_share_set_link_scope")
        policy = self.runtime_service.registry.upsert_share_policy(
            name,
            link_scope=link_scope,
            allowed_domain=allowed_domain,
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
                "allowed_domain": allowed_domain,
                "default_link_role": default_link_role,
                "public_catalog_visible": public_catalog_visible,
                "external_sharing_enabled": external_sharing_enabled,
                "allow_preview_link": allow_preview_link,
            },
        )
        return self._tool_result(
            "app_share_set_link_scope",
            text=f"Updated sharing link scope for app {name} to {link_scope}.",
            structured_content={"app": app.to_dict(), "share_policy": policy.to_dict()},
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
                "link_id": link.id,
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
        if link is None or link.app_name != name:
            raise DashServerError(
                category="share_link_not_found",
                summary=f"One-time sharing link {link_id} does not exist for app {name}.",
                details={"tool": "app_share_revoke_one_time_link", "app": name, "link_id": link_id},
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
            link_scope=existing_policy.link_scope,
            default_link_role=existing_policy.default_link_role,
            allow_preview_link=bool(existing_policy.allow_preview_link or scope == "preview"),
            public_catalog_visible=bool(existing_policy.public_catalog_visible),
            external_sharing_enabled=True,
            updated_by_principal_id=self._current_principal_id(),
        )
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_created",
            data={
                "invitation_id": invitation.id,
                "recipient_email": recipient_email,
                "scope": scope,
                "role": role,
                "expires_at": expires_at,
                "delivery_status": invitation.delivery_status,
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
            int(invitation.id),
            delivery_status=delivery_result.status,
            delivery_provider=delivery_result.provider,
            delivery_message_id=delivery_result.message_id,
            delivery_error=delivery_result.error,
        ) or invitation
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_delivery_updated",
            data={
                "invitation_id": invitation.id,
                "delivery_status": invitation.delivery_status,
                "delivery_provider": invitation.delivery_provider,
                "delivery_error": invitation.delivery_error,
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
                "status": invitation.delivery_status,
                "provider": invitation.delivery_provider,
                "message_id": invitation.delivery_message_id,
                "error": invitation.delivery_error,
                "mode": "manual" if invitation.delivery_status == "pending_manual_delivery" else "email",
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
        if invitation is None or invitation.app_name != name:
            raise DashServerError(
                category="invitation_not_found",
                summary=f"External invitation {invitation_id} does not exist for app {name}.",
                details={"tool": "app_revoke_external_invitation", "app": name, "invitation_id": invitation_id},
            )
        revoked = self.runtime_service.registry.revoke_invitation(invitation_id)
        self.runtime_service.registry.append_event(
            name,
            "external_invitation_revoked",
            data={
                "invitation_id": invitation_id,
                "grant_id": invitation.grant_id,
                "recipient_email": invitation.recipient_email,
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
            )
        # The Phase 3.5b re-spawn path: stop preserves the spec, ensure_running re-spawns it.
        manager.stop(mount_path, idle=True)
        record = manager.ensure_running(mount_path)
        if record is None:
            raise DashServerError(
                category="runtime_mount_error",
                summary=f"Failed to restart worker for {mount_path} — no persisted spec on disk.",
                details={"mount_path": mount_path},
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
                "name": "app_list_files",
                "title": "List draft files",
                "description": "List every editable file in the app draft workspace.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_outputs_list",
                "title": "List registered outputs",
                "description": (
                    "List governed dataset and view outputs declared by the current live revision, "
                    "including parameter schemas, effective formats, limits, and policy decisions."
                ),
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_output_get",
                "title": "Get a registered output",
                "description": "Inspect one governed output declared by the current live revision.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Hosted app name."},
                        "output_id": {
                            "type": "string",
                            "description": "Stable output id from app_outputs_list.",
                        },
                    },
                    "required": ["name", "output_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_export_create",
                "title": "Create dataset export",
                "description": "Queue a governed CSV or XLSX export from a registered output on the current live revision.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Hosted app name."},
                        "output_id": {"type": "string", "description": "Registered dataset output id."},
                        "format": {"type": "string", "enum": ["csv", "xlsx"]},
                        "parameters": {"type": "object", "description": "Values allowed by the output parameter schema."},
                        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "required": ["name", "output_id", "format", "parameters"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "app_exports_list",
                "title": "List personal exports",
                "description": "List the caller's recent export jobs for one app.",
                "inputSchema": self._name_schema(),
            },
            {
                "name": "app_exports_admin_list",
                "title": "List app-wide exports (owner/admin)",
                "description": (
                    "List every principal's export jobs for one app with redacted parameter "
                    "summaries. Requires the dashboard.manage_consumption capability."
                ),
                "inputSchema": self._name_schema(),
            },
            {
                "name": "export_get",
                "title": "Get export job",
                "description": "Read principal-bound export status and bounded artifact metadata.",
                "inputSchema": self._job_id_schema(),
            },
            {
                "name": "export_cancel",
                "title": "Cancel export job",
                "description": "Request cancellation of the caller's queued or running export.",
                "inputSchema": self._job_id_schema(),
            },
            {
                "name": "export_download_link_create",
                "title": "Create export download link",
                "description": "Create a short-lived authenticated URL for a completed export artifact.",
                "inputSchema": self._job_id_schema(),
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
                "name": "app_delete",
                "title": "Delete a hosted app",
                "description": (
                    "Permanently remove an app from the active runtime, catalog, draft workspace, "
                    "local artifacts, sharing state, and current GitOps branch. Published source "
                    "remains recoverable from Git history. confirmation must exactly equal name."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Hosted app name."},
                        "confirmation": {
                            "type": "string",
                            "description": "Must exactly match name to confirm destructive deletion.",
                        },
                    },
                    "required": ["name", "confirmation"],
                    "additionalProperties": False,
                },
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


    def _worker_manager_or_error(self) -> Any:
        if not has_request_context():
            raise DashServerError(
                category="runtime_state_error",
                summary="Runtime worker tools require a request context.",
                details={},
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
            )
        return manager


    def _dep_env_service_or_error(self) -> Any:
        if not has_request_context():
            raise DashServerError(
                category="runtime_state_error",
                summary="Environment tools require a request context.",
                details={},
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
            )
        return service


    def _exasol_service(self) -> ExasolDashboardService:
        if self.exasol_dashboard_service is None:
            raise DashServerError(
                category="exasol_not_configured",
                summary="Exasol dashboard features are not configured on this server.",
                details={},
            )
        return self.exasol_dashboard_service


    def _consumption_service(self) -> ConsumptionService:
        if self.consumption_service is None:
            raise DashServerError(
                category="consumption_not_configured",
                summary="Consumption output discovery is not configured on this server.",
                details={},
            )
        return self.consumption_service


    @staticmethod
    def _consumption_auth_context() -> AuthContext:
        if has_request_context():
            return current_auth_context()
        return AuthContext.for_mode("local", auth_enabled=False)


    def _require_name(self, arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        raise DashServerError(
            category="tool_validation_error",
            summary="Tool argument name must be a non-empty string.",
            details={"field": "name"},
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
            "share_policy": self.runtime_service.registry.get_share_policy(name).to_dict(),
            "grants": [grant.to_dict() for grant in self.runtime_service.registry.list_acl_entries(name)],
            "revoked_grants": [
                grant.to_dict() for grant in all_grants if grant.revoked_at is not None
            ],
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


    def _sanitize_share_link(self, link: ShareLink | None) -> dict[str, Any] | None:
        if link is None:
            return None
        return {
            key: value
            for key, value in link.to_dict().items()
            if key != "token_hash"
        }


    def _sanitize_invitation(self, invitation: Invitation | None) -> dict[str, Any] | None:
        if invitation is None:
            return None
        return {
            key: value
            for key, value in invitation.to_dict().items()
            if key != "token_hash"
        }


    def _sharing_warnings(self, name: str) -> list[dict[str, str]]:
        app = self.runtime_service.registry.get_app(name)
        policy = self.runtime_service.registry.get_share_policy(name)
        warnings = []
        if app is not None and app.visibility == "public" and policy.link_scope != "public":
            warnings.append(
                {
                    "code": "public_visibility_without_public_policy",
                    "message": "The app visibility is public, but the share policy is not public.",
                }
            )
        if app is not None and app.auth_policy == "required" and policy.link_scope == "public":
            warnings.append(
                {
                    "code": "public_policy_requires_auth",
                    "message": "The share policy is public, but auth_policy=required still blocks anonymous access.",
                }
            )
        if policy.link_scope == "domain" and not policy.allowed_domain:
            warnings.append(
                {
                    "code": "domain_policy_missing_allowed_domain",
                    "message": "The share policy is domain-scoped, but no allowed_domain is configured.",
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
                    app_name=link.app_name,
                    role=link.role,
                    scope=link.scope,
                    email=link.recipient_email,
                )
        user = self.runtime_service.registry.get_user_by_principal_id(principal_id)
        groups = arguments.get("groups", [])
        if groups is None:
            groups = []
        if not isinstance(groups, list) or any(not isinstance(item, str) for item in groups):
            raise self._field_error("app_share_explain_access", "groups", "must be an array of strings.")
        if user is not None:
            return Principal.authenticated_user(
                issuer=user.issuer,
                subject=user.subject,
                email=user.email,
                display_name=user.display_name,
                groups=tuple(groups),
                roles=(),
                email_verified=bool(user.email_verified),
                tenant_id=user.tenant_id,
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


    def _normalize_allowed_domain(self, value: Any, *, tool_name: str) -> str:
        if not isinstance(value, str):
            raise self._field_error(tool_name, "allowed_domain", "is required when link_scope is domain.")
        domain = value.strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        if (
            not domain
            or "@" in domain
            or "/" in domain
            or any(character.isspace() for character in domain)
        ):
            raise self._field_error(tool_name, "allowed_domain", "must be a DNS domain such as example.com.")
        return domain


    def _email_sender(self) -> InvitationEmailSender:
        if self.email_sender is not None:
            return self.email_sender
        if has_request_context():
            return current_app.extensions["email_sender"]
        return InvitationEmailSender({"DASH_SERVER_EMAIL_PROVIDER": "manual"})


    def _invitation_delivery_note(self, invitation: Invitation) -> str:
        status = invitation.delivery_status
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


    def _bundle_from_top_level_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        bundle: dict[str, Any] = {}
        for field_name in (
            "name",
            "title",
            "route",
            "description",
            "template",
            "data_sources",
            "consumption",
            "headline",
            "summary",
            "metrics",
        ):
            if field_name in arguments:
                bundle[field_name] = arguments[field_name]
        return bundle

