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

Queries with pyexasol placeholders (``{name!s}``, ``{name!d}``, etc.) require
per-query sample values in ``queries/sql_smoke.json``. Without sample values they
fail preflight with an actionable message; with values they are executed through
pyexasol's formatter so bad columns are caught before live promotion.

Queries against an Exasol Semantic Views published model need the model's SQL
preprocessor active in the session (normally via the profile's ``session_init_sql``).
With it, the wrapped query is checked exactly as at runtime (unknown fields, invalid
combinations). Without it, the ``WHERE 1=0`` wrapper can pass a query that fails for
the first viewer, so such files fail here with the fix instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
    # Names of the published semantic models the query reads.
    semantic_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticModelRef:
    """A published Exasol Semantic Views model, as the smoke session sees it."""

    model: str
    published_schema: str
    preprocessor: str
    # From SEMANTIC_AGENT.MODELS_FOR_AGENT: VALID, WARNING, INVALID, PRECONDITION,
    # NEEDS_VALIDATION, or NOT_PUBLISHED.
    readiness: str | None = None
    validation_status: str | None = None
    validation_warning_count: int | None = None
    validation_error_count: int | None = None
    latest_validation_run_id: int | None = None


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
    # The published semantic models the files read, with their readiness.
    semantic_models: list[SemanticModelRef] = field(default_factory=list)

    @property
    def first_failure(self) -> SqlSmokeFile | None:
        for entry in self.files:
            if entry.outcome == "failed":
                return entry
        return None


# pyexasol's substitution markers: `{name}` / `{name!s}` (string),
# `{name!d}` (decimal), `{name!f}` (float), `{name!q}` (quoted identifier),
# `{name!i}` (identifier), `{name!r}` (raw SQL fragment).
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:![sdfqir])?\}")


def run_sql_smoke(
    *,
    profile: ExasolProfile,
    sql_files: list[tuple[str, str]],
    connection_manager: ExasolConnectionManager,
    smoke_params: dict[str, dict[str, Any]] | None = None,
) -> SqlSmokeReport:
    """Smoke-test each SQL file against the bound profile. See module docstring.

    ``sql_files`` is a list of ``(relative_path, sql_text)`` pairs. Order is preserved
    in the returned report so the first failure is deterministic.
    """

    if not sql_files:
        return SqlSmokeReport(overall_status="skipped", files=[])

    # Open a dedicated, uncached connection for the whole batch; close in a
    # finally even on the connection-failure path. This must not use the runtime
    # callback cache, because smoke preflight deliberately closes its connection.
    try:
        connection = connection_manager.connect_uncached(
            profile,
            query_timeout_seconds=connection_manager.statement_timeout_seconds(profile),
        )
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
    semantic = _SemanticContext(connection)
    try:
        for relative_path, sql_text in sql_files:
            results.append(
                _smoke_one(
                    connection,
                    relative_path,
                    sql_text,
                    params=(smoke_params or {}).get(relative_path, {}),
                    semantic=semantic,
                    profile_name=profile.name,
                )
            )
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
    referenced = {name for entry in results for name in entry.semantic_models}
    return SqlSmokeReport(
        overall_status=overall,
        files=results,
        semantic_models=[ref for ref in semantic.known_models if ref.model in referenced],
    )


def _smoke_one(
    connection: object,
    relative_path: str,
    sql_text: str,
    *,
    params: dict[str, Any],
    semantic: _SemanticContext | None = None,
    profile_name: str = "",
) -> SqlSmokeFile:
    stripped = sql_text.strip()
    if not stripped:
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="skipped",
            skip_reason="empty file",
        )

    placeholder_names = sorted({match.group(1) for match in _PLACEHOLDER_RE.finditer(stripped)})
    if placeholder_names:
        missing = [name for name in placeholder_names if name not in params]
        if missing:
            return SqlSmokeFile(
                relative_path=relative_path,
                outcome="failed",
                error_text=(
                    "Parameterized SQL requires smoke-test params before deployment. "
                    f"Missing values for: {', '.join(missing)}. "
                    "Add queries/sql_smoke.json with sample values for this SQL file, "
                    "or run an explicit callback smoke test before live promotion."
                ),
            )

    semantic_models = _referenced_models(stripped, semantic.models) if semantic is not None else []
    model_names = tuple(ref.model for ref in semantic_models)
    inactive = [
        ref
        for ref in semantic_models
        if semantic is not None and _normalize_identifier(ref.preprocessor) != semantic.session_preprocessor
    ]
    if inactive:
        ref = inactive[0]
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="failed",
            semantic_models=model_names,
            error_text=(
                f"This query reads the published semantic model {ref.model!r} ({ref.published_schema}), "
                f"which needs the {ref.preprocessor} SQL preprocessor active in the session, but "
                f"sessions opened by Exasol profile {profile_name!r} don't activate it. Without it the "
                "dashboard fails at runtime (SEMANTIC_SURFACE_001 or 'not a valid GROUP BY expression'). "
                "Add this statement to the profile's session_init_sql (exasol_profile_create_local with "
                f"overwrite: true): ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {ref.preprocessor}"
            ),
        )

    # Wrap so the server parses and binds names without fetching rows. Strip a
    # trailing semicolon so the wrapped query parses cleanly.
    wrapped = f"SELECT * FROM (\n{stripped.rstrip(';')}\n) WHERE 1=0"
    try:
        statement = connection.execute(wrapped, params)  # type: ignore[attr-defined]
    except Exception as exc:
        return SqlSmokeFile(
            relative_path=relative_path,
            outcome="failed",
            error_text=str(exc),
            semantic_models=model_names,
        )

    close = getattr(statement, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    return SqlSmokeFile(relative_path=relative_path, outcome="passed", semantic_models=model_names)


_SEMANTIC_MODELS_COLUMNS = [
    "MODEL_NAME",
    "PUBLISHED_SCHEMA",
    "PREPROCESSOR_QUALIFIED_NAME",
    "AGENT_READINESS",
    "VALIDATION_STATUS",
    "VALIDATION_WARNING_COUNT",
    "VALIDATION_ERROR_COUNT",
    "LATEST_VALIDATION_RUN_ID",
]
_SEMANTIC_MODELS_QUERY = (
    f"SELECT DISTINCT {', '.join(_SEMANTIC_MODELS_COLUMNS)} "
    "FROM SEMANTIC_AGENT.MODELS_FOR_AGENT "
    "WHERE MODEL_STATUS = 'PUBLISHED' AND PUBLISHED_SCHEMA IS NOT NULL"
)


class _SemanticContext:
    """The smoke session's view of Semantic Views, looked up on first use.

    Lazy so a batch whose files all fail before execution (missing placeholder values)
    runs no statements at all.
    """

    def __init__(self, connection: object) -> None:
        self._connection = connection
        self._models: list[SemanticModelRef] | None = None
        self._session_preprocessor: str | None = None

    @property
    def models(self) -> list[SemanticModelRef]:
        if self._models is None:
            self._models = _published_semantic_models(self._connection)
            if self._models:
                self._session_preprocessor = _session_preprocessor(self._connection)
        return self._models

    @property
    def known_models(self) -> list[SemanticModelRef]:
        """The models looked up so far, without triggering a lookup."""

        return self._models or []

    @property
    def session_preprocessor(self) -> str | None:
        self.models  # noqa: B018 - looks up the session state on first use
        return self._session_preprocessor


def _published_semantic_models(connection: object) -> list[SemanticModelRef]:
    """Published Semantic Views models visible to the session; empty when none or not installed.

    Best effort: a database without Semantic Views (or a user who can't read its agent
    views) simply has no semantic files. The result is trusted only when the statement
    reports exactly the queried columns.
    """

    try:
        statement = connection.execute(_SEMANTIC_MODELS_QUERY)  # type: ignore[attr-defined]
        if list(statement.column_names()) != _SEMANTIC_MODELS_COLUMNS:
            return []
        rows = statement.fetchall()
    except Exception:
        return []
    return [
        SemanticModelRef(
            model=str(model),
            published_schema=str(schema),
            preprocessor=str(preprocessor),
            readiness=readiness,
            validation_status=validation_status,
            validation_warning_count=_optional_int(warnings),
            validation_error_count=_optional_int(errors),
            latest_validation_run_id=_optional_int(run_id),
        )
        for model, schema, preprocessor, readiness, validation_status, warnings, errors, run_id in rows
        if model and schema and preprocessor
    ]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _session_preprocessor(connection: object) -> str | None:
    """The session's active SQL preprocessor (normalized), or None."""

    try:
        statement = connection.execute(  # type: ignore[attr-defined]
            "SELECT SESSION_VALUE FROM EXA_PARAMETERS WHERE PARAMETER_NAME = 'SQL_PREPROCESSOR_SCRIPT'"
        )
        value = statement.fetchval()
    except Exception:
        return None
    return _normalize_identifier(value) if value else None


def _normalize_identifier(name: str) -> str:
    return name.replace('"', "").upper()


def _referenced_models(sql_text: str, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
    """The published models whose schema ``sql_text`` qualifies an object with."""

    return [
        ref
        for ref in models
        if re.search(
            r'(?<![A-Za-z0-9_$])"?' + re.escape(ref.published_schema) + r'"?\s*\.',
            sql_text,
            flags=re.IGNORECASE,
        )
    ]


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


def collect_sql_smoke_params(artifact_root: Path) -> dict[str, dict[str, Any]]:
    """Read optional per-query smoke params from ``queries/sql_smoke.json``.

    The file maps SQL relative paths to pyexasol parameter dictionaries:

    ``{"queries/agent_latency.sql": {"agent_id": "agent-001"}}``
    """

    config_path = artifact_root / "queries" / "sql_smoke.json"
    if not config_path.is_file():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    params_by_file: dict[str, dict[str, Any]] = {}
    for raw_path, raw_params in payload.items():
        if not isinstance(raw_path, str) or not isinstance(raw_params, dict):
            continue
        relative_path = raw_path if raw_path.startswith("queries/") else f"queries/{raw_path}"
        params_by_file[relative_path] = dict(raw_params)
    return params_by_file


__all__ = [
    "SemanticModelRef",
    "SqlSmokeFile",
    "SqlSmokeReport",
    "collect_sql_files",
    "collect_sql_smoke_params",
    "run_sql_smoke",
]
