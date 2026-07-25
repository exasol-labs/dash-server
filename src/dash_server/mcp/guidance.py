"""Agent-guidance attachment for MCP tool results.

Static per-tool guidance is folded onto each ``ToolSpec`` (see ``mcp/tool_specs.py``);
this module owns the dynamic (payload/error-dependent) cases and the fallback.
"""

from __future__ import annotations

import copy
from typing import Any

from dash_server.exceptions import DashServerError
from dash_server.mcp.tool_specs import TOOL_SPECS_BY_NAME


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



class GuidanceMixin:
    """Builds the ``guidance`` block attached to every tool result."""

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

        # Static guidance is folded onto each ToolSpec (P2.2); fall back to it here.
        spec = TOOL_SPECS_BY_NAME.get(tool_name)
        if spec is not None and spec.guidance is not None:
            return copy.deepcopy(spec.guidance)
        return {
            "next_step": "Inspect the returned payload and continue with the next workflow step.",
            "suggested_tools": ["app_get_status", "app_collect_diagnostics"],
            "related_resources": ["dash://meta/workflows"],
        }

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
        if category == "session_channel_session_gone":
            return {
                "next_step": (
                    "Check which tabs are live and ask the user to open the dashboard if none "
                    "are. Do not report a stale session's last known state as current."
                ),
                "suggested_tools": ["app_sessions_list", "app_get_status"],
                "related_resources": ["dash://meta/session-channel-guide"],
            }
        if category == "session_channel_unavailable":
            return {
                "next_step": (
                    "The browser session channel is a local-mode feature and is off here. "
                    "Fall back to server-side signals: diagnostics, logs, and health probes."
                ),
                "suggested_tools": ["app_collect_diagnostics", "app_tail_logs", "app_run_healthcheck"],
                "related_resources": ["dash://meta/session-channel-guide", "dash://runtime/status"],
            }
        if category in {"session_channel_timeout", "session_channel_busy"}:
            return {
                "next_step": (
                    "Confirm the tab is still polling, then retry with a smaller command or a "
                    "longer timeout_seconds."
                ),
                "suggested_tools": ["app_sessions_list", "app_session_eval_js"],
                "related_resources": ["dash://meta/session-channel-guide"],
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

