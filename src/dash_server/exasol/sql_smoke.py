"""SQL smoke-test capability: run each query with ``WHERE 1=0`` to validate it parses.

Used by two consumers:

- ``app_run_healthcheck`` (the ``sql_smoke`` probe) — answers "is the live dashboard
  actually able to talk to its data source?" Before this, the data-layer probe only
  inspected already-recorded errors, so a freshly-broken-but-never-clicked dashboard
  reported ``all probes passed``.
- ``_preflight_revision`` — answers "before we deploy this revision to live, do its
  queries even parse?" Before this, preflight only exercised the Dash layout, so an
  agent could ship a broken `queries/*.sql` to live and only notice when an
  end-user clicked the chart.

The smoke test wraps each query with ``SELECT * FROM (...) WHERE 1=0`` so the
server parses and binds names without fetching rows. Connection-level failures
collapse to a single ``connection_failed`` outcome — there's no point reporting
per-file when the profile itself can't open a session.

Queries with pyexasol placeholders (``{name!s}``, ``{name!d}``, etc.) are skipped
rather than executed, because we don't have meaningful test values. The persona-3
report flagged this as a friction point but the alternative (substitute defaults)
is too easy to get wrong; we report `skipped: parameterized` and an operator who
cares can add per-query smoke inputs in a follow-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .connection_manager import ExasolConnectionManager
    from .models import ExasolProfile


SmokeOutcome = Literal["passed", "failed", "skipped"]


@dataclass(frozen=True)
class SqlSmokeFile:
    """Per-query smoke result."""

    relative_path: str
    outcome: SmokeOutcome
    error_text: str | None = None
    skip_reason: str | None = None


@dataclass(frozen=True)
class SqlSmokeReport:
    """Aggregate smoke result over a set of SQL files for one profile.

    ``overall_status``:
        - ``"passed"`` — every non-skipped file parsed cleanly.
        - ``"failed"`` — at least one file failed to parse/bind, or the connection
          itself was unreachable.
        - ``"skipped"`` — no files were exercised (empty file list or every file
          skipped). Healthcheck callers report this as ``not_applicable``.
    """

    overall_status: SmokeOutcome
    files: list[SqlSmokeFile] = field(default_factory=list)
    connection_error: str | None = None

    @property
    def first_failure(self) -> SqlSmokeFile | None:
        for entry in self.files:
            if entry.outcome == "failed":
                return entry
        return None


# pyexasol's substitution markers: `{name!s}` (string), `{name!d}` (decimal),
# `{name!f}` (float), `{name!q}` (quoted identifier), `{name!i}` (identifier),
# `{name!r}` (raw SQL fragment). A literal `{` that's part of a JSON or a string
# literal in SQL wouldn't have the `!letter` shape, so the false-positive surface
# for this check is small.
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*![sdfqir]\}")


def run_sql_smoke(
    *,
    profile: ExasolProfile,
    sql_files: list[tuple[str, str]],
    connection_manager: ExasolConnectionManager,
) -> SqlSmokeReport:
    """Smoke-test each SQL file against the bound profile. See module docstring.

    ``sql_files`` is a list of ``(relative_path, sql_text)`` pairs. Order is preserved
    in the returned report so the first failure is deterministic.
    """

    if not sql_files:
        return SqlSmokeReport(overall_status="skipped", files=[])

    # Open one connection for the whole batch; close in a finally even on the
    # connection-failure path.
    try:
        connection = connection_manager.connect(profile)
    except Exception as exc:
        # Profile unreachable. Treat every file as `failed` with the connection
        # error attached so consumers can show a clear single message.
        error_text = str(exc)
        return SqlSmokeReport(
            overall_status="failed",
            files=[
                SqlSmokeFile(relative_path=rel_path, outcome="failed", error_text=error_text)
                for rel_path, _ in sql_files
            ],
            connection_error=error_text,
        )

    results: list[SqlSmokeFile] = []
    try:
        for relative_path, sql_text in sql_files:
            results.append(_smoke_one(connection, relative_path, sql_text))
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    # Aggregate. Files-list status semantics:
    # - any failed → overall failed
    # - all skipped → overall skipped (caller treats as not_applicable)
    # - otherwise passed
    if any(entry.outcome == "failed" for entry in results):
        overall: SmokeOutcome = "failed"
    elif all(entry.outcome == "skipped" for entry in results):
        overall = "skipped"
    else:
        overall = "passed"
    return SqlSmokeReport(overall_status=overall, files=results)


def _smoke_one(connection: object, relative_path: str, sql_text: str) -> SqlSmokeFile:
    stripped = sql_text.strip()
    if not stripped:
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="skipped",
            skip_reason="empty file",
        )
    if _PLACEHOLDER_RE.search(stripped):
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="skipped",
            skip_reason="parameterized (placeholders unfilled)",
        )

    # Wrap so the server parses and binds names without fetching rows. Strip a
    # trailing semicolon so the wrapped query parses cleanly.
    wrapped = f"SELECT * FROM (\n{stripped.rstrip(';')}\n) WHERE 1=0"
    try:
        statement = connection.execute(wrapped)  # type: ignore[attr-defined]
    except Exception as exc:
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="failed",
            error_text=str(exc),
        )

    close = getattr(statement, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    return SqlSmokeFile(relative_path=relative_path, outcome="passed")


def collect_sql_files(artifact_root: Path) -> list[tuple[str, str]]:
    """Walk an app's artifact directory and return every ``queries/**/*.sql`` file.

    Returns ``(relative_path, content)`` pairs in deterministic order so smoke
    reports are stable across runs.
    """

    queries_dir = artifact_root / "queries"
    if not queries_dir.is_dir():
        return []
    files: list[tuple[str, str]] = []
    for sql_path in sorted(queries_dir.rglob("*.sql")):
        if not sql_path.is_file():
            continue
        try:
            content = sql_path.read_text(encoding="utf-8")
        except OSError:
            continue
        files.append((str(sql_path.relative_to(artifact_root)), content))
    return files


__all__ = [
    "SqlSmokeFile",
    "SqlSmokeReport",
    "collect_sql_files",
    "run_sql_smoke",
]
