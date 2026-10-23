"""Data-layer errors shown to dashboard viewers leave out connection details."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask

from dash_server.diagnostics.service import DiagnosticsService
from dash_server.exasol.runtime import error_reference, query_rows, viewer_error_message

PYEXASOL_ERROR = (
    "\n(\n"
    "    message     =>  SEMANTIC_QUERY_029: Field units_sold was dropped from model sales. "
    "Ask the model's owner what replaces it. (Session: 1879837036350930944)\n"
    "    dsn         =>  127.0.0.1:8563\n"
    "    user        =>  sys\n"
    "    schema      =>  \n"
    "    session_id  =>  1879837036350930944\n"
    "    code        =>  42000\n"
    "    query       =>  SELECT units_sold FROM SEMANTIC_SALES.SALES\n"
    ")\n"
)


def test_viewer_message_keeps_only_the_database_message() -> None:
    message = viewer_error_message(PYEXASOL_ERROR)

    assert message.startswith("SEMANTIC_QUERY_029: Field units_sold was dropped from model sales.")
    assert message.endswith(f"(error ref {error_reference(PYEXASOL_ERROR)})")
    for leaked in ("127.0.0.1", "sys", "1879837036350930944", "SELECT units_sold"):
        assert leaked not in message


def test_viewer_message_keeps_a_multiline_database_message() -> None:
    error = "(\n    message     =>  first line\nsecond line\n    dsn         =>  host:8563\n)\n"

    assert viewer_error_message(error).startswith("first line\nsecond line (error ref ")


def test_viewer_message_hides_unstructured_errors() -> None:
    message = viewer_error_message("Could not connect to wss://10.0.0.5:8563: Connection refused")

    assert message.startswith("The data source could not answer this query.")
    assert "10.0.0.5" not in message


class _FailingService:
    def execute_profile_query(self, profile_name: str, sql_text: str, *, params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "error", "profile": profile_name, "error": PYEXASOL_ERROR}


def test_query_rows_records_the_full_error_under_the_viewer_reference(tmp_path: Path) -> None:
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "kpi.sql").write_text("SELECT units_sold FROM SEMANTIC_SALES.SALES")
    diagnostics = DiagnosticsService(str(tmp_path / "diagnostics"))
    server = Flask(__name__)
    server.extensions["exasol_dashboard_service"] = _FailingService()
    server.extensions["diagnostics_service"] = diagnostics
    metadata = {"name": "kpis", "revision_number": 1, "data_sources": {"primary": {"profile": "warehouse"}}}

    rows = query_rows(server, metadata, base_dir=str(tmp_path), sql_relative_path="queries/kpi.sql")

    assert rows == [{"_error": viewer_error_message(PYEXASOL_ERROR)}]
    [record] = diagnostics.list_errors("kpis")["errors"]
    assert record["details"]["error"] == PYEXASOL_ERROR
    assert record["details"]["error_ref"] == error_reference(PYEXASOL_ERROR)
