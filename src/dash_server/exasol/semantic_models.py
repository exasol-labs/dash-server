"""Published Exasol Semantic Views models, as an Exasol session sees them.

Shared by SQL smoke (which semantic models a query file reads, and whether the session
has their preprocessor active) and profile validation (which models an agent should
build on). Every lookup is best effort: a database without Semantic Views, or a user who
can't read its agent views, simply has no published models.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class SemanticModelRef:
    """A published Exasol Semantic Views model, as a session sees it."""

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
    description: str | None = None


MODELS_COLUMNS = [
    "MODEL_NAME",
    "PUBLISHED_SCHEMA",
    "PREPROCESSOR_QUALIFIED_NAME",
    "AGENT_READINESS",
    "VALIDATION_STATUS",
    "VALIDATION_WARNING_COUNT",
    "VALIDATION_ERROR_COUNT",
    "LATEST_VALIDATION_RUN_ID",
    "DESCRIPTION",
]
MODELS_QUERY = (
    f"SELECT DISTINCT {', '.join(MODELS_COLUMNS)} "
    "FROM SEMANTIC_AGENT.MODELS_FOR_AGENT "
    "WHERE MODEL_STATUS = 'PUBLISHED' AND PUBLISHED_SCHEMA IS NOT NULL "
    "ORDER BY MODEL_NAME"
)


def published_semantic_models(connection: object) -> list[SemanticModelRef]:
    """Published Semantic Views models visible to the session; empty when none or not installed.

    The result is trusted only when the statement reports exactly the queried columns.
    """

    try:
        statement = connection.execute(MODELS_QUERY)  # type: ignore[attr-defined]
        if list(statement.column_names()) != MODELS_COLUMNS:
            return []
        rows = statement.fetchall()
    except Exception:
        return []
    return models_from_rows(rows)


GOVERNED_SOURCES_COLUMNS = ["SOURCE_OBJECT", "MODEL_NAME", "PUBLISHED_SCHEMA"]
GOVERNED_SOURCES_QUERY = (
    "SELECT DISTINCT f.SOURCE_OBJECT, m.MODEL_NAME, m.PUBLISHED_SCHEMA "
    "FROM SEMANTIC_AGENT.FUSION_FOR_AGENT f "
    "JOIN SEMANTIC_AGENT.MODELS_FOR_AGENT m ON m.MODEL_ID = f.MODEL_ID "
    "WHERE m.MODEL_STATUS = 'PUBLISHED' AND f.SOURCE_OBJECT IS NOT NULL "
    "ORDER BY 1, 2"
)


def governed_sources(connection: object) -> dict[str, list[tuple[str, str]]]:
    """Physical relations behind published models: ``"SCHEMA.TABLE"`` -> [(model, published schema)].

    Covers every representation, partition and identity mapping a model reads. Best effort,
    like ``published_semantic_models``.
    """

    try:
        statement = connection.execute(GOVERNED_SOURCES_QUERY)  # type: ignore[attr-defined]
        if list(statement.column_names()) != GOVERNED_SOURCES_COLUMNS:
            return {}
        rows = statement.fetchall()
    except Exception:
        return {}
    sources: dict[str, list[tuple[str, str]]] = {}
    for source, model, schema in rows:
        if source and model and schema:
            sources.setdefault(normalize_identifier(str(source)), []).append((str(model), str(schema)))
    return sources


def referenced_governed_sources(sql_text: str, sources: dict[str, list[tuple[str, str]]]) -> list[str]:
    """The governed relations (``"SCHEMA.TABLE"``) that ``sql_text`` reads, in the order given."""

    found = []
    for source in sources:
        schema, _, table = source.partition(".")
        pattern = (
            r'(?<![A-Za-z0-9_$"])"?' + re.escape(schema) + r'"?\s*\.\s*"?' + re.escape(table) + r'"?(?![A-Za-z0-9_$])'
        )
        if table and re.search(pattern, sql_text, flags=re.IGNORECASE):
            found.append(source)
    return found


def models_from_rows(rows: Any) -> list[SemanticModelRef]:
    """Build model refs from rows of ``MODELS_QUERY``, skipping incomplete ones."""

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
            description=description,
        )
        for model, schema, preprocessor, readiness, validation_status, warnings, errors, run_id, description in rows
        if model and schema and preprocessor
    ]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def session_preprocessor(connection: object) -> str | None:
    """The session's active SQL preprocessor (normalized), or None."""

    try:
        statement = connection.execute(  # type: ignore[attr-defined]
            "SELECT SESSION_VALUE FROM EXA_PARAMETERS WHERE PARAMETER_NAME = 'SQL_PREPROCESSOR_SCRIPT'"
        )
        value = statement.fetchval()
    except Exception:
        return None
    return normalize_identifier(value) if value else None


def normalize_identifier(name: str) -> str:
    return name.replace('"', "").upper()


def referenced_models(sql_text: str, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
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


__all__ = [
    "GOVERNED_SOURCES_COLUMNS",
    "GOVERNED_SOURCES_QUERY",
    "MODELS_COLUMNS",
    "MODELS_QUERY",
    "SemanticModelRef",
    "governed_sources",
    "models_from_rows",
    "normalize_identifier",
    "published_semantic_models",
    "referenced_governed_sources",
    "referenced_models",
    "session_preprocessor",
]
