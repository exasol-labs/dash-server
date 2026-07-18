"""Normalization and validation for registered consumption outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, NoReturn

from dash_server.exceptions import DashServerError


_OUTPUT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
_DATASET_FORMATS = {"csv", "xlsx"}
_VIEW_FORMATS = {"pdf", "png", "pptx"}
_SCALAR_SCHEMA_TYPES = {"string", "integer", "number", "boolean"}
_SCALAR_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "default",
    "enum",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
}
_PYEXASOL_PLACEHOLDER_PATTERN = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]*)(?:![sdfqir])?\}"
)


def normalize_consumption_contract(
    raw_consumption: Any,
    *,
    data_sources: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and normalize the optional manifest consumption contract."""

    if raw_consumption is None:
        return None
    if not isinstance(raw_consumption, dict):
        _error("manifest.consumption", "Manifest consumption must be an object.")
    _reject_unknown(raw_consumption, {"outputs"}, "manifest.consumption")
    raw_outputs = raw_consumption.get("outputs", [])
    if not isinstance(raw_outputs, list):
        _error("manifest.consumption.outputs", "Consumption outputs must be an array.")

    outputs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_output in enumerate(raw_outputs):
        field = f"manifest.consumption.outputs[{index}]"
        output = _normalize_output(raw_output, field=field, data_sources=data_sources or {})
        output_id = output["id"]
        if output_id in seen_ids:
            _error(f"{field}.id", f"Consumption output id {output_id!r} is duplicated.")
        seen_ids.add(output_id)
        outputs.append(output)
    return {"outputs": outputs}


def consumption_contract_hash(consumption: dict[str, Any] | None) -> str:
    """Return a stable hash for one normalized output contract."""

    canonical = json.dumps(consumption or {"outputs": []}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_consumption_sources(
    consumption: dict[str, Any] | None,
    *,
    files: dict[str, str],
) -> dict[str, Any]:
    """Validate that output source files exist in the current workspace snapshot."""

    issues: list[dict[str, Any]] = []
    for output in (consumption or {}).get("outputs", []):
        source = output.get("source", {})
        if source.get("type") != "exasol_sql":
            continue
        path = source.get("path")
        if not isinstance(path, str) or path not in files:
            issues.append(
                {
                    "level": "error",
                    "output_id": output.get("id"),
                    "path": path,
                    "message": f"Declared consumption SQL source {path!r} was not found.",
                }
            )
            continue
        sql_text = files[path]
        placeholders = {
            match.group(1) for match in _PYEXASOL_PLACEHOLDER_PATTERN.finditer(sql_text)
        }
        declared_parameters = set(output.get("parameters", {}).get("properties", {}))
        undeclared = sorted(placeholders - declared_parameters)
        unused = sorted(declared_parameters - placeholders)
        if undeclared:
            issues.append(
                {
                    "level": "error",
                    "output_id": output.get("id"),
                    "path": path,
                    "message": (
                        "SQL uses undeclared consumption parameter(s): "
                        + ", ".join(undeclared)
                        + "."
                    ),
                }
            )
        if unused:
            issues.append(
                {
                    "level": "error",
                    "output_id": output.get("id"),
                    "path": path,
                    "message": (
                        "Consumption parameter(s) are not used by the SQL source: "
                        + ", ".join(unused)
                        + "."
                    ),
                }
            )
    return {
        "status": "failed" if issues else "passed",
        "contract_hash": consumption_contract_hash(consumption),
        "output_count": len((consumption or {}).get("outputs", [])),
        "issues": issues,
    }


def _normalize_output(
    raw_output: Any,
    *,
    field: str,
    data_sources: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_output, dict):
        _error(field, "Each consumption output must be an object.")
    allowed = {
        "id",
        "title",
        "description",
        "kind",
        "source",
        "parameters",
        "formats",
        "classification",
        "limits",
        "allow_subscriptions",
        "allow_alerts",
        "render",
    }
    _reject_unknown(raw_output, allowed, field)
    output_id = _required_string(raw_output, "id", field)
    if not _OUTPUT_ID_PATTERN.fullmatch(output_id):
        _error(
            f"{field}.id",
            "Output id must start with a lowercase letter and contain only lowercase letters, numbers, or hyphens.",
        )
    title = _required_string(raw_output, "title", field)
    kind = _required_string(raw_output, "kind", field)
    if kind not in {"dataset", "view"}:
        _error(f"{field}.kind", "Output kind must be dataset or view.")
    classification = _required_string(raw_output, "classification", field)
    if classification not in _CLASSIFICATIONS:
        _error(
            f"{field}.classification",
            f"Classification must be one of {', '.join(sorted(_CLASSIFICATIONS))}.",
        )
    source = _normalize_source(
        raw_output.get("source"),
        field=f"{field}.source",
        kind=kind,
        data_sources=data_sources,
    )
    formats = _normalize_formats(raw_output.get("formats"), field=f"{field}.formats", kind=kind)
    parameters = _normalize_parameters(raw_output.get("parameters"), field=f"{field}.parameters")
    limits = _normalize_limits(raw_output.get("limits"), field=f"{field}.limits")
    render = _normalize_render(raw_output.get("render"), field=f"{field}.render", kind=kind)
    description = raw_output.get("description", "")
    if not isinstance(description, str):
        _error(f"{field}.description", "Output description must be a string.")

    normalized: dict[str, Any] = {
        "id": output_id,
        "title": title,
        "description": description,
        "kind": kind,
        "source": source,
        "parameters": parameters,
        "formats": formats,
        "classification": classification,
        "limits": limits,
        "allow_subscriptions": _optional_bool(raw_output, "allow_subscriptions", field),
        "allow_alerts": _optional_bool(raw_output, "allow_alerts", field),
    }
    if render is not None:
        normalized["render"] = render
    return normalized


def _normalize_source(
    raw_source: Any,
    *,
    field: str,
    kind: str,
    data_sources: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_source, dict):
        _error(field, "Output source must be an object.")
    source_type = _required_string(raw_source, "type", field)
    if kind == "dataset":
        _reject_unknown(raw_source, {"type", "data_source", "path"}, field)
        if source_type != "exasol_sql":
            _error(f"{field}.type", "Dataset outputs currently support only exasol_sql.")
        alias = _required_string(raw_source, "data_source", field)
        if alias not in data_sources:
            _error(f"{field}.data_source", f"Datasource alias {alias!r} is not declared.")
        source_config = data_sources.get(alias)
        if not isinstance(source_config, dict) or source_config.get("kind") != "exasol":
            _error(f"{field}.data_source", "exasol_sql requires an Exasol datasource alias.")
        path = _safe_relative_path(_required_string(raw_source, "path", field), f"{field}.path")
        if not path.startswith("queries/") or not path.endswith(".sql"):
            _error(f"{field}.path", "Exasol output sources must be queries/*.sql files.")
        return {"type": source_type, "data_source": alias, "path": path}

    _reject_unknown(raw_source, {"type", "path"}, field)
    if source_type != "dash_route":
        _error(f"{field}.type", "View outputs currently support only dash_route.")
    path = _required_string(raw_source, "path", field)
    if not path.startswith("/") or "://" in path or ".." in PurePosixPath(path).parts:
        _error(f"{field}.path", "View output path must be an app-relative route beginning with /.")
    return {"type": source_type, "path": path}


def _normalize_formats(raw_formats: Any, *, field: str, kind: str) -> list[str]:
    if not isinstance(raw_formats, list) or not raw_formats:
        _error(field, "Output formats must be a non-empty array.")
    if any(not isinstance(item, str) for item in raw_formats):
        _error(field, "Every output format must be a string.")
    formats = list(dict.fromkeys(item.lower() for item in raw_formats))
    allowed = _DATASET_FORMATS if kind == "dataset" else _VIEW_FORMATS
    unsupported = sorted(set(formats) - allowed)
    if unsupported:
        _error(field, f"Unsupported {kind} format(s): {', '.join(unsupported)}.")
    return formats


def _normalize_parameters(raw_parameters: Any, *, field: str) -> dict[str, Any]:
    if raw_parameters is None:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    if not isinstance(raw_parameters, dict):
        _error(field, "Output parameters must be a JSON Schema object.")
    _reject_unknown(raw_parameters, {"type", "properties", "required", "additionalProperties"}, field)
    if raw_parameters.get("type") != "object":
        _error(f"{field}.type", "Output parameter schema type must be object.")
    properties = raw_parameters.get("properties", {})
    if not isinstance(properties, dict):
        _error(f"{field}.properties", "Parameter properties must be an object.")
    normalized_properties: dict[str, Any] = {}
    for name, schema in properties.items():
        if not isinstance(name, str) or not re.fullmatch(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", name):
            _error(f"{field}.properties", f"Invalid parameter name {name!r}.")
        if not isinstance(schema, dict):
            _error(f"{field}.properties.{name}", "Parameter schema must be an object.")
        _reject_unknown(schema, _SCALAR_SCHEMA_KEYS, f"{field}.properties.{name}")
        if schema.get("type") not in _SCALAR_SCHEMA_TYPES:
            _error(
                f"{field}.properties.{name}.type",
                "Parameters support string, integer, number, or boolean types.",
            )
        normalized_properties[name] = dict(schema)
    required = raw_parameters.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        _error(f"{field}.required", "Required parameters must be an array of names.")
    unknown_required = sorted(set(required) - set(properties))
    if unknown_required:
        _error(f"{field}.required", f"Unknown required parameter(s): {', '.join(unknown_required)}.")
    if raw_parameters.get("additionalProperties", False) is not False:
        _error(f"{field}.additionalProperties", "additionalProperties must be false.")
    return {
        "type": "object",
        "properties": normalized_properties,
        "required": list(dict.fromkeys(required)),
        "additionalProperties": False,
    }


def _normalize_limits(raw_limits: Any, *, field: str) -> dict[str, int]:
    if raw_limits is None:
        return {}
    if not isinstance(raw_limits, dict):
        _error(field, "Output limits must be an object.")
    _reject_unknown(raw_limits, {"max_rows", "max_bytes"}, field)
    normalized: dict[str, int] = {}
    for key, value in raw_limits.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            _error(f"{field}.{key}", f"{key} must be a positive integer.")
        normalized[key] = value
    return normalized


def _normalize_render(raw_render: Any, *, field: str, kind: str) -> dict[str, Any] | None:
    if raw_render is None:
        return None
    if kind != "view" or not isinstance(raw_render, dict):
        _error(field, "Render settings are allowed only for view outputs and must be an object.")
    _reject_unknown(raw_render, {"viewport", "ready_timeout_seconds"}, field)
    normalized: dict[str, Any] = {}
    viewport = raw_render.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, dict):
            _error(f"{field}.viewport", "Viewport must be an object.")
        _reject_unknown(viewport, {"width", "height"}, f"{field}.viewport")
        width = viewport.get("width")
        height = viewport.get("height")
        if not isinstance(width, int) or not 320 <= width <= 7680:
            _error(f"{field}.viewport.width", "Viewport width must be between 320 and 7680.")
        if not isinstance(height, int) or not 240 <= height <= 4320:
            _error(f"{field}.viewport.height", "Viewport height must be between 240 and 4320.")
        normalized["viewport"] = {"width": width, "height": height}
    timeout = raw_render.get("ready_timeout_seconds")
    if timeout is not None:
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            _error(f"{field}.ready_timeout_seconds", "Render timeout must be between 1 and 300 seconds.")
        normalized["ready_timeout_seconds"] = timeout
    return normalized


def _safe_relative_path(value: str, field: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        _error(field, "Output source path must be a normalized workspace-relative path.")
    return str(path)


def _required_string(payload: dict[str, Any], key: str, field: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        _error(f"{field}.{key}", f"{key} must be a non-empty string.")
    return value.strip()


def _optional_bool(payload: dict[str, Any], key: str, field: str) -> bool:
    value = payload.get(key, False)
    if not isinstance(value, bool):
        _error(f"{field}.{key}", f"{key} must be a boolean.")
    return value


def _reject_unknown(payload: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _error(field, f"Unknown field(s): {', '.join(unknown)}.")


def _error(field: str, summary: str) -> NoReturn:
    raise DashServerError(
        category="consumption_contract_validation_error",
        summary=summary,
        details={"field": field},
        jsonrpc_code=-32602,
        http_status=400,
    )
