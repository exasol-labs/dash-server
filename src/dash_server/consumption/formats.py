"""Registry of executable export formats shared by the single job pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .csv_format import write_csv
from .xlsx_format import write_xlsx


class ExportWriter(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        columns: list[str],
        batches: Iterable[list[list[Any]]],
        max_rows: int,
        max_bytes: int,
        cancelled: Callable[[], bool],
        provenance: dict[str, Any],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExportFormat:
    """One executable dataset format: writer plus its delivery metadata."""

    name: str
    content_type: str
    extension: str
    writer: ExportWriter


_DATASET_FORMATS: dict[str, ExportFormat] = {
    "csv": ExportFormat(
        name="csv",
        content_type="text/csv; charset=utf-8",
        extension="csv",
        writer=write_csv,
    ),
    "xlsx": ExportFormat(
        name="xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extension="xlsx",
        writer=write_xlsx,
    ),
}


def get_dataset_format(name: str) -> ExportFormat | None:
    return _DATASET_FORMATS.get(name)


def executable_dataset_formats() -> frozenset[str]:
    return frozenset(_DATASET_FORMATS)


__all__ = ["ExportFormat", "ExportWriter", "executable_dataset_formats", "get_dataset_format"]
