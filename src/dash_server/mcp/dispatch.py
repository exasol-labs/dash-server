"""MCP JSON-RPC dispatch core: routing, tool-call, envelopes, and enforcement."""

from __future__ import annotations

import json
import logging
from typing import Any

import jsonschema
from flask import current_app, has_request_context, request

from dash_server.auth import current_auth_context
from dash_server.dash_apps.factory import (
    app_create_from_files_schema_help,
    app_create_schema_help,
)
from dash_server.errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
)
from dash_server.exceptions import DashServerError
from dash_server.mcp.tool_specs import TOOL_SPECS_BY_NAME

LOGGER = logging.getLogger(__name__)


class DispatchMixin:
    """JSON-RPC method routing, tool invocation, and response envelopes."""

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
                request_id, {"code": JSONRPC_INVALID_REQUEST, "message": "Only JSON-RPC 2.0 is supported."}, 200
            )
        if not isinstance(method, str):
            return self._error_response(
                request_id, {"code": JSONRPC_INVALID_REQUEST, "message": "A string method is required."}, 200
            )
        if not isinstance(params, dict):
            return self._error_response(
                request_id, {"code": JSONRPC_INVALID_PARAMS, "message": "Params must be an object."}, 200
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
        except Exception as exc:
            # PS26-BUG-003 backstop: this method's own dispatch (resources/list,
            # resources/read, tools/list, ...) has the same shape of risk as
            # `_call_tool` below - anything raised here that isn't a `DashServerError`
            # used to propagate straight past this handler with no catch anywhere
            # above it, so Flask's default error handling turned it into a raw HTML
            # 500 instead of a JSON-RPC response. `_call_tool` has its own matching
            # backstop for `tools/call` specifically (see below) so its errors come
            # back tool-shaped; this one covers everything else that reaches
            # `handle_jsonrpc`, with a plain JSON-RPC error object.
            LOGGER.exception("Unhandled exception dispatching MCP method=%s", method)
            return self._error_response(
                request_id,
                {
                    "code": JSONRPC_INTERNAL_ERROR,
                    "message": f"Unexpected server error handling {method}: {type(exc).__name__}: {exc}",
                },
                200,
            )

        return self._error_response(
            request_id, {"code": JSONRPC_METHOD_NOT_FOUND, "message": f"Method not found: {method}"}, 200
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
                )
            if not isinstance(arguments, dict):
                raise DashServerError(
                    category="tool_validation_error",
                    summary="Tool arguments must be an object.",
                    details={"tool": name},
                )
            handler = self._tool_handlers.get(name)
            if handler is None:
                raise DashServerError(
                    category="tool_not_found",
                    summary="Unknown tool.",
                    details={"tool": name},
                )
            self._validate_tool_arguments(str(name), arguments)
            self._enforce_tool_capability(str(name), arguments)
            return handler(arguments)
        except DashServerError as exc:
            self._log_mcp_error("tools/call", params, exc)
            return self._tool_error_result(str(name), exc)
        except Exception as exc:
            # PS26-BUG-003: two concurrent `app_build` calls for the same app used to
            # let one of them raise a raw, non-`DashServerError` exception (a git
            # `CalledProcessError`, a `sqlite3.IntegrityError`, ...) straight out of
            # `handler(arguments)` above. Nothing between here and Flask's own
            # unhandled-exception page caught anything but `DashServerError`, so the
            # caller got an HTML 500 instead of a JSON-RPC response, and - because
            # `_log_mcp_error` is only ever called from a `DashServerError` branch -
            # no diagnostics record was left anywhere to explain it afterward.
            # `AppRuntimeService._locked_app_operation` now serializes the specific
            # racy calls (build/put_files/patch_file/delete_file/start_preview/
            # promote_revision/rollback) so this shouldn't fire for that class of bug
            # anymore, but it stays as the backstop for every other tool too: any
            # unexpected exception, from anywhere, always comes back as a clean
            # tool-shaped JSON-RPC error rather than leaking transport-level.
            LOGGER.exception("Unhandled exception in tool call name=%s", name)
            synthetic = DashServerError(
                category="unexpected_runtime_error",
                summary=f"{name} failed with an unexpected error: {type(exc).__name__}: {exc}",
                details={"tool": str(name), "exception_type": type(exc).__name__},
            )
            self._log_mcp_error("tools/call", params, synthetic)
            return self._tool_error_result(str(name), synthetic)


    def _enforce_tool_capability(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Handler-path capability gate for app-scoped tools without a service check.

        Driven by ``ToolSpec.enforce_in_handler`` so the transport capability map
        is defense in depth rather than the sole gate — closing the gap where a
        control-plane role that lacks the specific capability (e.g. an editor
        without ``dashboard.manage_sharing``) could reach a sharing tool.
        ``_require_app_capability`` is itself a no-op in local mode.
        """

        spec = TOOL_SPECS_BY_NAME.get(tool_name)
        if spec is None or not spec.enforce_in_handler or spec.app_capability is None:
            return
        self._require_app_capability(
            self._require_name(arguments),
            spec.app_capability,
            tool_name=tool_name,
        )


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
                jsonrpc_code=JSONRPC_INVALID_PARAMS,
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


    def _require_app_capability(
        self,
        name: str,
        capability: str,
        *,
        tool_name: str,
    ) -> None:
        if not has_request_context():
            return
        auth_context = current_auth_context()
        if auth_context.mode == "local":
            return
        app = self._require_existing_app(name, tool_name=tool_name)
        decision = current_app.extensions["authorization_service"].authorize_app(
            auth_context,
            app,
            capability,
        )
        if decision.allowed:
            return
        raise DashServerError(
            category="app_authorization_denied",
            summary=f"Principal cannot perform {capability} on app {name}.",
            details=decision.to_dict(),
            http_status=decision.status_code,
        )

