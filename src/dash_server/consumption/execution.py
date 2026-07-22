"""Revision-pinned, bounded Exasol dataset execution."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from dash_server.exceptions import DashServerError
from dash_server.paths import safe_join
from dash_server.exasol.service import ExasolDashboardService
from dash_server.exasol.sql_smoke import collect_sql_smoke_params, run_sql_smoke
from dash_server.registry.models import AppRevision


@dataclass
class DatasetStream:
    columns: list[str]
    batches: Iterator[list[list[Any]]]


class ExasolDatasetExecutor:
    """Open a dedicated export connection and fetch rows in bounded batches."""

    def __init__(self, service: ExasolDashboardService, *, batch_size: int, max_runtime_seconds: int) -> None:
        self.service = service
        self.batch_size = batch_size
        self.max_runtime_seconds = max_runtime_seconds

    def preflight(self, revision: AppRevision, output: dict[str, Any]) -> None:
        profile, sql_path, sql_text = self._resolve(revision, output)
        smoke_params = collect_sql_smoke_params(Path(revision.artifact_path))
        report = run_sql_smoke(
            profile=profile,
            sql_files=[(sql_path, sql_text)],
            connection_manager=self.service.connection_manager,
            smoke_params=smoke_params,
        )
        if report.overall_status != "passed":
            first = report.first_failure
            reason = "connection_failed" if report.connection_error else "sql_smoke_failed"
            if first is not None and first.error_text and "Missing values for:" in first.error_text:
                reason = "smoke_parameters_missing"
            raise DashServerError(
                category="consumption_output_preflight_failed",
                summary="The registered output did not pass executable Exasol preflight.",
                details={
                    "app": revision.app_name,
                    "revision_number": revision.revision_number,
                    "output_id": output.get("id"),
                    "reason": reason,
                },
            )

    def stream(
        self,
        revision: AppRevision,
        output: dict[str, Any],
        parameters: dict[str, Any],
        *,
        cancelled: Callable[[], bool],
    ) -> DatasetStream:
        profile, _sql_path, sql_text = self._resolve(revision, output)
        try:
            connection = self.service.connection_manager.connect_uncached(
                profile,
                query_timeout_seconds=self.max_runtime_seconds,
            )
            statement = connection.execute(sql_text, parameters)
        except Exception as exc:
            raise DashServerError(
                category="consumption_query_failed",
                summary="The Exasol export query could not be started.",
                details={"profile": profile.name, "reason": type(exc).__name__},
            ) from exc
        columns = _extract_columns(statement)
        started = time.monotonic()

        def batches() -> Iterator[list[list[Any]]]:
            try:
                while True:
                    if cancelled():
                        raise _cancelled_error()
                    if time.monotonic() - started > self.max_runtime_seconds:
                        raise DashServerError(
                            category="consumption_query_timeout",
                            summary="The export exceeded its configured runtime limit.",
                            details={"max_runtime_seconds": self.max_runtime_seconds},
                        )
                    rows = _fetch_batch(statement, self.batch_size)
                    if not rows:
                        break
                    yield [_normalize_row(row) for row in rows]
            except DashServerError:
                raise
            except Exception as exc:
                raise DashServerError(
                    category="consumption_query_failed",
                    summary="The Exasol export query failed while fetching results.",
                    details={"profile": profile.name, "reason": type(exc).__name__},
                ) from exc
            finally:
                with suppress(Exception):
                    statement.close()
                with suppress(Exception):
                    connection.close()

        return DatasetStream(columns=columns, batches=batches())

    def _resolve(self, revision: AppRevision, output: dict[str, Any]):
        source = output.get("source", {})
        alias = source.get("data_source")
        data_sources = revision.manifest.get("data_sources")
        source_config = data_sources.get(alias) if isinstance(data_sources, dict) else None
        profile_name = source_config.get("profile") if isinstance(source_config, dict) else None
        if not isinstance(profile_name, str) or not profile_name:
            raise DashServerError(
                category="consumption_profile_not_found",
                summary="The registered output has no usable Exasol profile binding.",
                details={"app": revision.app_name, "output_id": output.get("id")},
            )
        try:
            profile = self.service.profile_store.get_profile(profile_name)
        except Exception as exc:
            raise DashServerError(
                category="consumption_profile_not_found",
                summary="The registered output's Exasol profile was not found.",
                details={"profile": profile_name, "output_id": output.get("id")},
            ) from exc
        relative_path = source.get("path")
        try:
            sql_path = safe_join(Path(revision.artifact_path), str(relative_path))
        except ValueError:
            sql_path = None
        if sql_path is None or not sql_path.is_file():
            raise DashServerError(
                category="consumption_source_not_found",
                summary="The pinned export SQL source is unavailable.",
                details={"path": relative_path, "revision_number": revision.revision_number},
            )
        return profile, str(relative_path), sql_path.read_text(encoding="utf-8")


def _fetch_batch(statement: Any, size: int) -> Sequence[Sequence[Any]]:
    fetchmany = getattr(statement, "fetchmany", None)
    if callable(fetchmany):
        return fetchmany(size)
    fetchone = getattr(statement, "fetchone", None)
    if callable(fetchone):
        rows: list[Sequence[Any]] = []
        for _ in range(size):
            row = fetchone()
            if row is None:
                break
            rows.append(row)
        return rows
    raise DashServerError(
        category="consumption_streaming_unsupported",
        summary="The configured Exasol driver does not provide bounded result fetching.",
        details={},
    )


def _extract_columns(statement: Any) -> list[str]:
    column_names = getattr(statement, "column_names", None)
    if callable(column_names):
        names = column_names()
        if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
            return [str(name) for name in names]
    columns = getattr(statement, "columns", None)
    if callable(columns):
        value = columns()
        if isinstance(value, dict):
            return [str(name) for name in value]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [str(name) for name in value]
    return []


def _normalize_row(row: Any) -> list[Any]:
    if isinstance(row, (tuple, list)):
        return list(row)
    return [row]


def _cancelled_error() -> DashServerError:
    return DashServerError(
        category="consumption_job_cancelled",
        summary="Export cancellation was requested.",
        details={},
    )


__all__ = ["DatasetStream", "ExasolDatasetExecutor"]
