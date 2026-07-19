"""Streaming, spreadsheet-safe CSV formatter."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any

from dash_server.exceptions import DashServerError


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def write_csv(
    path: Path,
    *,
    columns: list[str],
    batches: Iterable[list[list[Any]]],
    max_rows: int,
    max_bytes: int,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    row_count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for batch in batches:
            if cancelled():
                raise DashServerError(
                    category="consumption_job_cancelled",
                    summary="Export cancellation was requested.",
                    details={},
                    jsonrpc_code=-32032,
                    http_status=409,
                )
            for row in batch:
                if row_count >= max_rows:
                    raise _limit_error("rows", max_rows)
                writer.writerow([_safe_cell(value) for value in row])
                row_count += 1
            handle.flush()
            if path.stat().st_size > max_bytes:
                raise _limit_error("bytes", max_bytes)
    byte_size = path.stat().st_size
    if byte_size > max_bytes:
        raise _limit_error("bytes", max_bytes)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"row_count": row_count, "byte_size": byte_size, "sha256": digest.hexdigest()}


def _safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _limit_error(kind: str, limit: int) -> DashServerError:
    return DashServerError(
        category="consumption_export_limit_exceeded",
        summary=f"Export exceeded the configured {kind} limit.",
        details={"limit_kind": kind, "limit": limit},
        jsonrpc_code=-32033,
        http_status=413,
    )


__all__ = ["write_csv"]
