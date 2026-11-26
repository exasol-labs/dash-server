"""Published Exasol Semantic Views models, as an Exasol session sees them.

Shared by SQL smoke (which semantic models a query file reads, and whether the session
can run Semantic SQL against them) and profile validation (which models an agent should
build on). Every lookup is best effort: a database without Semantic Views, or a user who
can't read its agent views, simply has no published models.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    # Set when the model's newest validation run never finished (it says RUNNING): the
    # fields above then describe the newest finished run instead.
    unfinished_validation_run_id: int | None = None
    # Issues the validation run behind ``readiness`` recorded; filled in by
    # ``with_validation_issues`` for models that aren't VALID.
    issues: tuple[ValidationIssue, ...] = ()
    # For a fused model (partitions, alternate sources, reconciled attributes): how it is
    # fused, and how its reconciled attributes are answered today. Filled in by ``with_fusion``.
    fusion_layout: tuple[FusionPart, ...] = ()
    fusion_health: tuple[FusionHealth, ...] = ()
    # Other names the model's fields answer to, as {"kind", "synonym", "field"}: a renamed
    # field keeps its old name as a synonym. Filled in by ``with_synonyms``.
    synonyms: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class FusionPart:
    """One row of a model's fusion layout (SEMANTIC_AGENT.FUSION_FOR_AGENT)."""

    entity: str
    aspect: str
    representation: str | None
    attribute: str | None
    strategy: str | None
    coverage: str | None


@dataclass(frozen=True)
class FusionHealth:
    """How one reconciled attribute is answered (SEMANTIC_ADMIN.CHECK_FUSION_HEALTH)."""

    entity: str
    attribute: str
    strategy: str | None
    first_source: str | None
    total_keys: int | None
    from_first_source: int | None
    filled: int | None
    unresolved: int | None
    probe_error: str | None


@dataclass(frozen=True)
class ValidationIssue:
    """One finding of a Semantic Views validation run."""

    severity: str
    rule_code: str
    object_name: str | None
    message: str


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
    return with_finished_validation(connection, models_from_rows(rows))


# A validation run that never finished reads as VALID in MODELS_FOR_AGENT (it has no
# errors or warnings yet), and Semantic Views' read-only CHECK_* scripts can leave such
# runs behind. Readiness is therefore taken from the newest *finished* run, using the
# same rules as MODELS_FOR_AGENT.AGENT_READINESS.
_UNFINISHED_VALIDATION_STATUS = "RUNNING"
FINISHED_RUNS_COLUMNS = [
    "MODEL_NAME",
    "VALIDATION_RUN_ID",
    "STATUS",
    "ERROR_COUNT",
    "WARNING_COUNT",
    "PRECONDITION_COUNT",
]
FINISHED_RUNS_QUERY = (
    "SELECT m.MODEL_NAME, vr.VALIDATION_RUN_ID, vr.STATUS, vr.ERROR_COUNT, vr.WARNING_COUNT, "
    "COALESCE(pc.PRECONDITION_COUNT, 0) AS PRECONDITION_COUNT "
    "FROM SEMANTIC_AGENT.MODELS_FOR_AGENT m "
    "JOIN (SELECT MODEL_ID, VERSION_ID, MAX(VALIDATION_RUN_ID) AS VALIDATION_RUN_ID "
    "FROM SEMANTIC_SOURCE.VALIDATION_RUNS WHERE STATUS <> 'RUNNING' GROUP BY MODEL_ID, VERSION_ID) latest "
    "ON latest.MODEL_ID = m.MODEL_ID AND latest.VERSION_ID = m.VERSION_ID "
    "JOIN SEMANTIC_SOURCE.VALIDATION_RUNS vr ON vr.VALIDATION_RUN_ID = latest.VALIDATION_RUN_ID "
    "LEFT JOIN (SELECT VALIDATION_RUN_ID, COUNT(*) AS PRECONDITION_COUNT FROM SEMANTIC_SOURCE.VALIDATION_RESULTS "
    "WHERE SEVERITY = 'PRECONDITION' GROUP BY VALIDATION_RUN_ID) pc ON pc.VALIDATION_RUN_ID = vr.VALIDATION_RUN_ID "
    "WHERE m.MODEL_STATUS = 'PUBLISHED'"
)


def with_finished_validation(connection: object, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
    """``models`` with readiness from each model's newest finished validation run.

    Only models whose newest run is unfinished change. When the finished runs can't be
    read (no access to ``SEMANTIC_SOURCE``), their readiness becomes ``UNKNOWN`` rather
    than the unfinished run's misleading ``VALID``.
    """

    if not has_unfinished_validation(models):
        return models
    try:
        statement = connection.execute(FINISHED_RUNS_QUERY)  # type: ignore[attr-defined]
        if list(statement.column_names()) != FINISHED_RUNS_COLUMNS:
            raise ValueError("unexpected columns")
        rows = statement.fetchall()
    except Exception:
        rows = None
    return apply_finished_runs(models, rows)


def has_unfinished_validation(models: list[SemanticModelRef]) -> bool:
    return any(ref.validation_status == _UNFINISHED_VALIDATION_STATUS for ref in models)


def apply_finished_runs(models: list[SemanticModelRef], rows: Any) -> list[SemanticModelRef]:
    """``models`` with readiness from ``FINISHED_RUNS_QUERY`` rows (None: the runs couldn't be read)."""

    finished = {str(row[0]): tuple(row[1:]) for row in rows} if rows is not None else None
    resolved = []
    for ref in models:
        if ref.validation_status != _UNFINISHED_VALIDATION_STATUS:
            resolved.append(ref)
            continue
        run = finished.get(ref.model) if finished is not None else None
        if run is None:
            resolved.append(
                replace(
                    ref,
                    readiness="UNKNOWN" if finished is None else "NEEDS_VALIDATION",
                    validation_status=None,
                    validation_warning_count=None,
                    validation_error_count=None,
                    latest_validation_run_id=None,
                    unfinished_validation_run_id=ref.latest_validation_run_id,
                )
            )
            continue
        run_id, status, errors, warnings, preconditions = run
        resolved.append(
            replace(
                ref,
                readiness=_readiness(_optional_int(errors), _optional_int(preconditions), _optional_int(warnings)),
                validation_status=status,
                validation_warning_count=_optional_int(warnings),
                validation_error_count=_optional_int(errors),
                latest_validation_run_id=_optional_int(run_id),
                unfinished_validation_run_id=ref.latest_validation_run_id,
            )
        )
    return resolved


def _readiness(errors: int | None, preconditions: int | None, warnings: int | None) -> str:
    if errors:
        return "INVALID"
    if preconditions:
        return "PRECONDITION"
    if warnings:
        return "WARNING"
    return "VALID"


_FUSION_LAYOUT_COLUMNS = [
    "MODEL_NAME",
    "ENTITY_NAME",
    "FUSION_ASPECT",
    "REPRESENTATION_NAME",
    "ATTRIBUTE_NAME",
    "STRATEGY",
    "COVERAGE_PREDICATE",
]
_FUSION_HEALTH_COLUMNS = [
    "ENTITY_NAME",
    "ATTRIBUTE_TYPE",
    "ATTRIBUTE_NAME",
    "FUSION_STRATEGY",
    "FIRST_SOURCE",
    "TOTAL_KEYS",
    "FROM_FIRST_SOURCE",
    "FILLED",
    "UNRESOLVED",
    "PROBE_ERROR",
]


def with_fusion(connection: object, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
    """``models`` with the fusion layout and attribute health of each fused one.

    Every model has a single PRIMARY representation per entity; anything else (partitions,
    alternates, authority, reconcile policies, identity maps) makes it fused. Health comes
    from ``CHECK_FUSION_HEALTH``, which reads only and records nothing. Best effort.
    """

    if not models:
        return models
    names = {f"m{index}": ref.model for index, ref in enumerate(models)}
    try:
        statement = connection.execute(  # type: ignore[attr-defined]
            f"SELECT {', '.join(_FUSION_LAYOUT_COLUMNS)} FROM SEMANTIC_AGENT.FUSION_FOR_AGENT "
            f"WHERE MODEL_NAME IN ({', '.join(f'{{{key}!s}}' for key in names)})",
            names,
        )
        if list(statement.column_names()) != _FUSION_LAYOUT_COLUMNS:
            return models
        rows = statement.fetchall()
    except Exception:
        return models
    layouts: dict[str, set[FusionPart]] = {}
    for model, entity, aspect, representation, attribute, strategy, coverage in rows:
        layouts.setdefault(str(model), set()).add(
            FusionPart(
                entity=str(entity),
                aspect=str(aspect),
                representation=representation,
                attribute=attribute,
                strategy=strategy,
                coverage=coverage,
            )
        )
    resolved = []
    for ref in models:
        layout = layouts.get(ref.model, set())
        if all(part.aspect == "REPRESENTATION" and part.strategy == "PRIMARY" for part in layout):
            resolved.append(ref)
            continue
        resolved.append(
            replace(
                ref,
                fusion_layout=tuple(
                    sorted(layout, key=lambda part: tuple(str(value) for value in vars(part).values()))
                ),
                fusion_health=_fusion_health(connection, ref.model),
            )
        )
    return resolved


def _fusion_health(connection: object, model: str) -> tuple[FusionHealth, ...]:
    try:
        statement = connection.execute(  # type: ignore[attr-defined]
            "EXECUTE SCRIPT SEMANTIC_ADMIN.CHECK_FUSION_HEALTH({model!s})", {"model": model}
        )
        if list(statement.column_names()) != _FUSION_HEALTH_COLUMNS:
            return ()
        rows = statement.fetchall()
    except Exception:
        return ()
    return tuple(
        FusionHealth(
            entity=str(entity),
            attribute=str(attribute),
            strategy=strategy,
            first_source=first_source,
            total_keys=_optional_int(total),
            from_first_source=_optional_int(first),
            filled=_optional_int(filled),
            unresolved=_optional_int(unresolved),
            probe_error=probe_error,
        )
        for entity, _kind, attribute, strategy, first_source, total, first, filled, unresolved, probe_error in rows
    )


def with_synonyms(connection: object, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
    """``models`` with their fields' synonyms (from SEMANTIC_SOURCE.SYNONYMS). Best effort."""

    if not models:
        return models
    names = {f"m{index}": ref.model for index, ref in enumerate(models)}
    try:
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT DISTINCT f.MODEL_NAME, f.FIELD_KIND, s.SYNONYM, f.FIELD_NAME "
            "FROM SEMANTIC_SOURCE.SYNONYMS s JOIN SEMANTIC_AGENT.FIELDS_FOR_AGENT f "
            "ON f.MODEL_ID = s.MODEL_ID AND f.VERSION_ID = s.VERSION_ID AND f.FIELD_KIND = s.OBJECT_TYPE "
            "AND f.FIELD_ID = s.OBJECT_ID "
            f"WHERE f.MODEL_NAME IN ({', '.join(f'{{{key}!s}}' for key in names)}) ORDER BY 1, 2, 3",
            names,
        ).fetchall()
    except Exception:
        return models
    synonyms: dict[str, list[dict[str, str]]] = {}
    for model, kind, synonym, field in rows:
        synonyms.setdefault(str(model), []).append(
            {"kind": str(kind).lower(), "synonym": str(synonym), "field": str(field)}
        )
    return [replace(ref, synonyms=tuple(synonyms.get(ref.model, ()))) for ref in models]


_ISSUES_PER_MODEL = 20
_ISSUE_MESSAGE_CHARS = 300


def with_validation_issues(connection: object, models: list[SemanticModelRef]) -> list[SemanticModelRef]:
    """``models`` with the issues behind each non-VALID readiness (rule code, object, message).

    Read from ``SEMANTIC_SOURCE.VALIDATION_RESULTS`` by run id, so the issues match the
    run the readiness came from. Best effort: models keep no issues if they can't be read.
    """

    run_ids = {
        ref.latest_validation_run_id
        for ref in models
        if ref.readiness not in (None, "VALID") and ref.latest_validation_run_id is not None
    }
    if not run_ids:
        return models
    try:
        statement = connection.execute(  # type: ignore[attr-defined]
            "SELECT VALIDATION_RUN_ID, SEVERITY, RULE_CODE, OBJECT_NAME, MESSAGE "
            "FROM SEMANTIC_SOURCE.VALIDATION_RESULTS "
            f"WHERE VALIDATION_RUN_ID IN ({', '.join(str(int(run_id)) for run_id in sorted(run_ids))}) "
            "ORDER BY VALIDATION_RUN_ID, CASE SEVERITY WHEN 'ERROR' THEN 0 WHEN 'PRECONDITION' THEN 1 ELSE 2 END, "
            "VALIDATION_RESULT_ID"
        )
        rows = statement.fetchall()
    except Exception:
        return models
    issues: dict[int, list[ValidationIssue]] = {}
    for run_id, severity, rule_code, object_name, message in rows:
        bucket = issues.setdefault(int(run_id), [])
        if len(bucket) < _ISSUES_PER_MODEL:
            text = str(message or "")
            bucket.append(
                ValidationIssue(
                    severity=str(severity),
                    rule_code=str(rule_code),
                    object_name=str(object_name) if object_name else None,
                    message=text if len(text) <= _ISSUE_MESSAGE_CHARS else text[: _ISSUE_MESSAGE_CHARS - 1] + "…",
                )
            )
    return [
        replace(ref, issues=tuple(issues.get(ref.latest_validation_run_id, ())))
        if ref.latest_validation_run_id is not None
        else ref
        for ref in models
    ]


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


@dataclass(frozen=True)
class PreprocessorSettings:
    """The ``SQL_PREPROCESSOR_SCRIPT`` parameter of a session, normalized; None when unset."""

    system: str | None
    session: str | None


PREPROCESSOR_SETTINGS_QUERY = (
    "SELECT SYSTEM_VALUE, SESSION_VALUE FROM EXA_PARAMETERS WHERE PARAMETER_NAME = 'SQL_PREPROCESSOR_SCRIPT'"
)


def preprocessor_settings(connection: object) -> PreprocessorSettings:
    """The database-wide and session SQL preprocessor; both None when they can't be read."""

    try:
        row = connection.execute(PREPROCESSOR_SETTINGS_QUERY).fetchone()  # type: ignore[attr-defined]
    except Exception:
        return PreprocessorSettings(system=None, session=None)
    if not row:
        return PreprocessorSettings(system=None, session=None)
    system, session = (normalize_identifier(str(value)) if value else None for value in row)
    return PreprocessorSettings(system=system, session=session)


@dataclass(frozen=True)
class SemanticSqlProbe:
    """Whether a session can run Semantic SQL against one published model.

    ``probed`` is False when the model's fields can't be read; ``active`` then falls back to
    comparing the session's preprocessor with the model's.
    """

    active: bool
    probed: bool
    statement: str | None = None
    error: str | None = None


# The model's own fields, metrics first: a metric alone is the query a model refuses least.
# Not the published schema's catalog, which also holds unguarded views (SEMANTIC_DISCOVERY)
# that answer without any rewrite.
_PROBE_FIELDS_QUERY = (
    "SELECT PUBLISHED_OBJECT_NAME, SQL_COLUMN_NAME FROM SEMANTIC_AGENT.FIELDS_FOR_AGENT "
    "WHERE MODEL_NAME = {model!s} AND PUBLISHED_OBJECT_NAME IS NOT NULL AND SQL_COLUMN_NAME IS NOT NULL "
    "ORDER BY CASE WHEN FIELD_KIND = 'METRIC' THEN 0 ELSE 1 END, PUBLISHED_OBJECT_NAME, ORDINAL_POSITION"
)
# How many published views the probe tries before concluding Semantic SQL is off.
_PROBE_ATTEMPTS = 3


def probe_semantic_sql(connection: object, ref: SemanticModelRef, settings: PreprocessorSettings) -> SemanticSqlProbe:
    """Run Semantic SQL against ``ref`` in the session and report whether it is rewritten.

    Comparing the session's preprocessor with the model's misses every other route to Semantic
    SQL, such as a preprocessor framework that dispatches to it as one of its modules, and
    whether such a route applies can depend on the user and the client. A test query answers
    that for this session. A published view returns rows only when Semantic SQL rewrites the
    query; otherwise its guard raises SEMANTIC_SURFACE_001. ``LIMIT 0`` keeps the probe free
    and still reaches the guard, unlike a ``WHERE 1=0`` wrapper, which the optimizer answers
    without evaluating it.

    A field can be one the compiler refuses on its own (SEMANTIC_QUERY_*, a partitioned joined
    dimension, say). That refusal still proves the rewrite ran. A preprocessor framework may
    swallow such a refusal and let the statement reach the guard instead, so a field of up to
    three published objects is tried before the session counts as not rewriting.
    """

    candidates = _probe_candidates(connection, ref.model)
    if not candidates:
        return SemanticSqlProbe(active=normalize_identifier(ref.preprocessor) == settings.session, probed=False)
    failure: SemanticSqlProbe | None = None
    for table, column in candidates:
        statement = f"SELECT {_quote(column)} FROM {_quote(ref.published_schema)}.{_quote(table)} LIMIT 0"
        try:
            result = connection.execute(statement)  # type: ignore[attr-defined]
            close = getattr(result, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            error = str(exc)
            if _SEMANTIC_REFUSAL_RE.search(error):
                return SemanticSqlProbe(active=True, probed=True, statement=statement)
            # Keep the most telling failure: any error other than the guard's names a real cause.
            if failure is None or "SEMANTIC_SURFACE_001" in (failure.error or ""):
                failure = SemanticSqlProbe(active=False, probed=True, statement=statement, error=error)
            continue
        return SemanticSqlProbe(active=True, probed=True, statement=statement)
    assert failure is not None
    return failure


# A compiler refusal (SEMANTIC_QUERY_074: ...) comes from the rewrite itself, so it shows Semantic SQL ran.
_SEMANTIC_REFUSAL_RE = re.compile(r"\bSEMANTIC_QUERY_\d+")


def _probe_candidates(connection: object, model: str) -> list[tuple[str, str]]:
    """One field of each of up to ``_PROBE_ATTEMPTS`` published objects of ``model``, metrics first."""

    try:
        rows = connection.execute(_PROBE_FIELDS_QUERY, {"model": model}).fetchall()  # type: ignore[attr-defined]
    except Exception:
        return []
    candidates: dict[str, str] = {}
    for table, column in rows or []:
        candidates.setdefault(str(table), str(column))
        if len(candidates) >= _PROBE_ATTEMPTS:
            break
    return list(candidates.items())


def probe_failure_note(probe: SemanticSqlProbe) -> str:
    """The probe's error, unless it is the guard's own message, which says to switch preprocessors.

    Semantic Views' guard (SEMANTIC_SURFACE_001) tells the caller to run ENABLE_SEMANTIC_SQL(),
    which is wrong wherever a database-wide preprocessor is set; ``semantic_sql_setup_advice``
    says what to do instead. Any other error (no access to the view, say) is the real cause.
    """

    if probe.error is None or "SEMANTIC_SURFACE_001" in probe.error:
        return ""
    error = " ".join(probe.error.split())
    if len(error) > 400:
        error = error[:400] + "..."
    return f"The test query {probe.statement} failed: {error}"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def semantic_sql_setup_advice(ref: SemanticModelRef, settings: PreprocessorSettings) -> str:
    """What to do when a profile's sessions can't run Semantic SQL against ``ref``.

    Setting the session's preprocessor is the fix only when no database-wide preprocessor is
    set. Exasol has one preprocessor slot: with a database-wide preprocessor in it (a
    preprocessor framework, say), ``ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT`` replaces it for
    the session and silently turns off everything else it does, or fails where the database
    forbids overriding it. That is an administrator's decision, never a profile setting.
    """

    if settings.system is None:
        return (
            "No SQL preprocessor is set database-wide. Add this statement to the profile's session_init_sql "
            "(exasol_profile_create_local with name, overwrite: true and session_init_sql; other settings are "
            f"kept): ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = {ref.preprocessor}"
        )
    if settings.system == normalize_identifier(ref.preprocessor):
        return (
            f"{ref.preprocessor} is the database-wide SQL preprocessor, so the profile's user most likely "
            "lacks the privileges to run it: ask a database administrator to grant it SEMANTIC_USER and the "
            "model's role. Don't add session_init_sql for this."
        )
    return (
        f"The database-wide SQL preprocessor is {settings.system}, and it doesn't run Semantic SQL for this "
        "profile's user or client. Don't set SQL_PREPROCESSOR_SCRIPT in the profile's session_init_sql, and "
        f"don't run ENABLE_SEMANTIC_SQL(): either replaces {settings.system} in every session of the profile "
        "and turns off everything else it does. Ask a database administrator to have it route Semantic SQL "
        "for this user (for a preprocessor framework: a Semantic Views module rule that covers the user and "
        "client), or to grant the user SEMANTIC_USER and the model's role."
    )


# Marks of SQL compiled by Exasol Semantic Views (COMPILE_SQL / COMPILE_REQUEST_JSON). Fused
# models emit "__esv_*" CTEs and f4_/f5_ representation aliases; every compile re-aliases its
# canonical lower-case field names to upper case in the outer projection.
_COMPILED_SEMANTIC_SQL_RE = re.compile(r'"__esv_|"F5_SEMANTIC_KEY"|\bf[45]_(?:rep|map|src)_\d+\b')
_COMPILED_PROJECTION_RE = re.compile(r'"([a-z][a-z0-9_]*)"\s+AS\s+"([A-Z][A-Z0-9_]*)"')


def looks_like_compiled_semantic_sql(sql: str) -> bool:
    """True for SQL saved from the semantic layer's compiler rather than written against a model.

    Such SQL is a frozen copy of the definitions at compile time: it keeps running, with
    the old numbers, after the model's owner changes a metric.
    """

    if _COMPILED_SEMANTIC_SQL_RE.search(sql):
        return True
    return any(alias == name.upper() for name, alias in _COMPILED_PROJECTION_RE.findall(sql))


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
    "FINISHED_RUNS_COLUMNS",
    "FINISHED_RUNS_QUERY",
    "GOVERNED_SOURCES_COLUMNS",
    "GOVERNED_SOURCES_QUERY",
    "MODELS_COLUMNS",
    "MODELS_QUERY",
    "PREPROCESSOR_SETTINGS_QUERY",
    "FusionHealth",
    "FusionPart",
    "PreprocessorSettings",
    "SemanticModelRef",
    "SemanticSqlProbe",
    "ValidationIssue",
    "apply_finished_runs",
    "governed_sources",
    "has_unfinished_validation",
    "looks_like_compiled_semantic_sql",
    "models_from_rows",
    "normalize_identifier",
    "preprocessor_settings",
    "probe_failure_note",
    "probe_semantic_sql",
    "published_semantic_models",
    "referenced_governed_sources",
    "referenced_models",
    "semantic_sql_setup_advice",
    "with_finished_validation",
    "with_fusion",
    "with_synonyms",
    "with_validation_issues",
]
