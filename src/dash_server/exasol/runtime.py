"""Runtime helpers for hosted Dash apps backed by Exasol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask

from dash_server.exceptions import DashServerError


def execute_profile_query(
    server: Flask,
    metadata: dict[str, Any],
    *,
    base_dir: str,
    sql_relative_path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one SQL file against the Exasol profile bound in app metadata."""

    service = server.extensions.get("exasol_dashboard_service")
    if service is None:
        raise DashServerError(
            category="exasol_runtime_error",
            summary="Exasol dashboard service is not registered on the Flask server.",
            details={},
            jsonrpc_code=-32012,
            http_status=500,
        )
    data_sources = metadata.get("data_sources")
    if not isinstance(data_sources, dict):
        raise DashServerError(
            category="exasol_runtime_error",
            summary="Hosted app metadata does not declare Exasol data_sources.",
            details={},
            jsonrpc_code=-32012,
            http_status=500,
        )
    primary = data_sources.get("primary")
    if not isinstance(primary, dict) or not isinstance(primary.get("profile"), str):
        raise DashServerError(
            category="exasol_runtime_error",
            summary="Hosted app metadata does not declare a primary Exasol profile.",
            details={},
            jsonrpc_code=-32012,
            http_status=500,
        )
    sql_path = Path(base_dir) / sql_relative_path
    if not sql_path.exists():
        raise DashServerError(
            category="exasol_runtime_error",
            summary=f"SQL file {sql_relative_path} was not found.",
            details={"path": str(sql_path)},
            jsonrpc_code=-32012,
            http_status=404,
        )
    sql_text = sql_path.read_text()
    return service.execute_profile_query(
        primary["profile"],
        sql_text,
        params=params or {},
    )


def query_rows(
    server: Flask,
    metadata: dict[str, Any],
    *,
    base_dir: str,
    sql_relative_path: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute one SQL file and normalize the result to row dictionaries."""

    result = execute_profile_query(
        server,
        metadata,
        base_dir=base_dir,
        sql_relative_path=sql_relative_path,
        params=params,
    )
    if result.get("status") != "ok":
        error_text = result.get("error", "Unknown Exasol error")
        _record_data_layer_error(server, metadata, sql_relative_path, error_text)
        return [{"_error": error_text}]
    return _records_from_result(result)


def _record_data_layer_error(
    server: Flask,
    metadata: dict[str, Any],
    sql_relative_path: str,
    error_text: str,
) -> None:
    """Best-effort propagation of a runtime SQL error into the diagnostics surface."""

    try:
        diagnostics = server.extensions.get("diagnostics_service") if server is not None else None
        if diagnostics is None:
            return
        app_name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(app_name, str) or not app_name:
            return
        data_sources = metadata.get("data_sources") if isinstance(metadata, dict) else None
        primary = (
            data_sources.get("primary")
            if isinstance(data_sources, dict) and isinstance(data_sources.get("primary"), dict)
            else {}
        )
        profile_name = primary.get("profile") if isinstance(primary, dict) else None
        # BUG-005 fix: stamp errors with the active revision number so probes and the
        # `dash://apps/{name}/errors` resource can filter old-revision noise out after
        # promote/rollback. The mount-time metadata carries `revision_number` (see
        # `AppRuntimeService._mount_revision_inprocess`); legacy mounts without it
        # fall through as `None` and behave the way they did pre-fix.
        revision_number = metadata.get("revision_number") if isinstance(metadata, dict) else None
        diagnostics.record_data_layer_error(
            app_name,
            sql_file=sql_relative_path,
            profile_name=profile_name if isinstance(profile_name, str) else "",
            error_text=str(error_text),
            revision_number=revision_number if isinstance(revision_number, int) else None,
        )
    except Exception:
        # Diagnostics is best-effort; never let it kill a Dash callback.
        return


def query_one(
    server: Flask,
    metadata: dict[str, Any],
    *,
    base_dir: str,
    sql_relative_path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute one SQL file and return the first row or an error record."""

    rows = query_rows(
        server,
        metadata,
        base_dir=base_dir,
        sql_relative_path=sql_relative_path,
        params=params,
    )
    if not rows:
        return None
    return rows[0]


def has_error(rows: list[dict[str, Any]] | dict[str, Any] | None) -> bool:
    """Return True when ``rows`` is the single-row error envelope `query_rows` returns on failure.

    Use this before iterating rows:

        rows = query_rows(server, metadata, sql_relative_path="queries/x.sql")
        if has_error(rows):
            return render_error_panel(rows[0]["_error"])
        ...

    `query_rows` and friends never raise on data-layer failure — they return
    ``[{"_error": "<message>"}]`` so the Dash callback path stays stable. This helper
    centralizes the "did the query fail?" check so callers don't reach for KeyError
    on `row["AGENT_ID"]` to discover failure (which was Persona 2's BUG-018 cause).
    """

    if isinstance(rows, list):
        return len(rows) == 1 and isinstance(rows[0], dict) and "_error" in rows[0]
    if isinstance(rows, dict):
        return "_error" in rows
    return False


def query_scalar(
    server: Flask,
    metadata: dict[str, Any],
    *,
    base_dir: str,
    sql_relative_path: str,
    params: dict[str, Any] | None = None,
    column: str | None = None,
) -> Any:
    """Execute one SQL file and return a scalar value from the first row."""

    row = query_one(
        server,
        metadata,
        base_dir=base_dir,
        sql_relative_path=sql_relative_path,
        params=params,
    )
    if not row:
        return None
    if "_error" in row:
        return row
    if column is not None:
        return row.get(column)
    if not row:
        return None
    return next(iter(row.values()))


def _records_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("rows")
    columns = result.get("columns")
    if not isinstance(rows, list):
        return []
    records: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            records.append(row)
            continue
        if isinstance(row, list) and isinstance(columns, list):
            record = {str(column): value for column, value in zip(columns, row, strict=False)}
            records.append(record)
    return records
