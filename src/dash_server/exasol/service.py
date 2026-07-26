"""Orchestration for Exasol profiles and generated dashboard scaffolds."""

from __future__ import annotations

import re
from typing import Any

from dash_server.exceptions import DashServerError
from dash_server.gitops import GitRepoService

from .connection_manager import ExasolConnectionManager
from .models import ExasolProfile
from .profiles import ExasolProfileStore
from .scaffold import (
    EXASOL_DASHBOARD_PATTERNS,
    build_exasol_dashboard_bundle,
    build_schema_scaffold_bundle,
    exasol_agent_workflow_help,
    exasol_connection_modes_help,
    exasol_dashboard_patterns_help,
    exasol_sql_placeholders_help,
)
from .secrets import ExasolSecretStore

_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ExasolDashboardService:
    """Own Exasol profile metadata, secrets, validation, and scaffolds."""

    def __init__(
        self,
        git_repo_service: GitRepoService,
        secrets_root: str,
    ) -> None:
        self.profile_store = ExasolProfileStore(git_repo_service)
        self.secret_store = ExasolSecretStore(secrets_root)
        self.connection_manager = ExasolConnectionManager(self.secret_store)

    def list_profiles(self) -> dict[str, Any]:
        return {"profiles": [self._serialize_profile(profile) for profile in self.profile_store.list_profiles()]}

    def get_profile(self, name: str) -> dict[str, Any]:
        return {"profile": self._serialize_profile(self.profile_store.get_profile(name))}

    def connection_modes_help(self) -> dict[str, Any]:
        return exasol_connection_modes_help()

    def dashboard_patterns_help(self) -> dict[str, Any]:
        return exasol_dashboard_patterns_help()

    def agent_workflow_help(self) -> dict[str, Any]:
        return exasol_agent_workflow_help()

    def sql_placeholders_help(self) -> dict[str, Any]:
        return exasol_sql_placeholders_help()

    def create_local_profile(
        self,
        *,
        name: str,
        backend: str,
        credential_mode: str,
        dsn: str,
        user: str,
        description: str | None = None,
        tls_verify: bool = True,
        secret_value: str | None = None,
        secret_env_var: str | None = None,
        statement_timeout_seconds: int | None = None,
        row_limit: int | None = None,
    ) -> dict[str, Any]:
        self._validate_app_name(name, field_name="name")
        self._validate_backend_mode(backend, credential_mode)
        if not dsn:
            raise self._field_error("dsn", "must be a non-empty string.")
        if not user:
            raise self._field_error("user", "must be a non-empty string.")
        if bool(secret_value) == bool(secret_env_var):
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary="Provide exactly one of secret_value or secret_env_var.",
                details={"field": "secret_value"},
            )
        if secret_value:
            secret_ref = self.secret_store.store_local_secret(name, secret_value)
        else:
            assert secret_env_var is not None
            secret_ref = self.secret_store.env_secret_ref(secret_env_var)

        query_defaults = {
            "statement_timeout_seconds": statement_timeout_seconds or 30,
            "row_limit": row_limit or 50000,
        }
        profile = ExasolProfile(
            name=name,
            backend=backend,
            deployment_mode="local_direct",
            credential_mode=credential_mode,
            user=user,
            dsn=dsn,
            description=description or f"Local Exasol profile {name}.",
            tls_verify=tls_verify,
            secret_ref=secret_ref,
            query_defaults=query_defaults,
        )
        stored = self.profile_store.save_profile(profile)
        return {
            "profile": self._serialize_profile(stored),
            "secret_storage": {
                "provider": stored.secret_ref.provider,
                "key": stored.secret_ref.key,
            },
        }

    def validate_profile(self, name: str) -> dict[str, Any]:
        profile = self.profile_store.get_profile(name)
        validation = self.connection_manager.validate_profile(profile)
        validation["profile"] = self._serialize_profile(profile)
        return validation

    def build_dashboard_bundle(
        self,
        *,
        app_name: str,
        profile_name: str,
        title: str | None = None,
        route: str | None = None,
        description: str | None = None,
        pattern: str = "analytics-hub",
    ) -> dict[str, Any]:
        self._validate_app_name(app_name, field_name="name")
        profile = self.profile_store.get_profile(profile_name)
        if pattern not in EXASOL_DASHBOARD_PATTERNS:
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"Unsupported Exasol dashboard pattern {pattern}.",
                details={"field": "pattern", "supported_patterns": list(EXASOL_DASHBOARD_PATTERNS)},
            )
        return build_exasol_dashboard_bundle(
            app_name=app_name,
            title=title or self._humanize_name(app_name),
            route=route or f"/apps/{app_name}",
            description=description or f"Live Exasol dashboard backed by profile {profile.name}.",
            profile_name=profile.name,
            pattern=pattern,
        )

    def build_schema_scaffold_bundle(
        self,
        *,
        app_name: str,
        profile_name: str,
        schema_name: str | None = None,
        table_name: str | None = None,
        title: str | None = None,
        route: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        self._validate_app_name(app_name, field_name="name")
        profile = self.profile_store.get_profile(profile_name)
        blueprint = self._discover_schema_blueprint(
            profile.name, schema_name=schema_name, table_name=table_name
        )
        return build_schema_scaffold_bundle(
            app_name=app_name,
            title=title or f"{self._humanize_name(app_name)} Analytics",
            route=route or f"/apps/{app_name}",
            description=description
            or (
                f"Schema-tailored Exasol analytics app for {blueprint['schema_name']}.{blueprint['table_name']} "
                f"backed by profile {profile.name}."
            ),
            profile_name=profile.name,
            blueprint=blueprint,
        )

    def execute_profile_query(
        self,
        profile_name: str,
        sql_text: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = self.profile_store.get_profile(profile_name)
        query_defaults = profile.query_defaults or {}
        row_limit = int(query_defaults.get("row_limit", 50000))
        # Try once with a cached connection; if the statement fails in a way that
        # looks like a stale session, invalidate and retry once with a fresh one.
        # The retry path covers Exasol idle-timeouts and TCP resets without making
        # every healthy query pay a reconnect cost. (Per-callback queries against a
        # cached session run in ~30-50ms; cold-start costs ~400ms for the TLS
        # handshake + login.)
        for attempt in range(2):
            statement: Any = None
            try:
                # `connect()` itself can raise (SSL verify failure, refused TCP, bad DSN);
                # this must happen INSIDE the try so the same `{status: "error"}` shape
                # downstream code expects covers connection-level failures too. (BUG-006.)
                connection = self.connection_manager.connect(profile)
                statement = connection.execute(sql_text, params or {})
                visible_rows, truncated = self._fetch_bounded_rows(statement, row_limit)
                columns = self._extract_columns(statement)
                # Close only the statement; the connection stays cached. Closing
                # promptly here also matters more than it used to (PS26-BUG-004):
                # for a statement we only partially consumed via `_fetch_bounded_rows`'s
                # `fetchmany` above, this is the signal that tells Exasol it can
                # abandon any remaining unfetched rows for this cursor rather than
                # holding them ready.
                close_statement = getattr(statement, "close", None)
                if callable(close_statement):
                    try:
                        close_statement()
                    except Exception:
                        pass
                return {
                    "status": "ok",
                    "profile": profile.name,
                    "columns": columns,
                    "rows": visible_rows,
                    "row_count": len(visible_rows),
                    "truncated": truncated,
                    "summary": (
                        f"Loaded {len(visible_rows)} row(s) from Exasol profile {profile.name}."
                        + (" Results were truncated to the configured row limit." if truncated else "")
                    ),
                }
            except Exception as exc:
                if attempt == 0 and _looks_like_dead_session(exc):
                    # First attempt failed on what looks like a stale connection —
                    # drop it from the cache and retry once with a fresh handshake.
                    self.connection_manager.invalidate(profile.name)
                    continue
                # Genuine failure (bad SQL, bad credentials, server down). Make sure
                # the connection used for this attempt is gone so the next caller
                # doesn't inherit a half-broken session.
                self.connection_manager.invalidate(profile.name)
                return {
                    "status": "error",
                    "profile": profile.name,
                    "error": str(exc),
                    "summary": f"Exasol query failed for profile {profile.name}.",
                }
        # Unreachable — the loop returns on success or after the second attempt's
        # except branch — but keeps type checkers happy.
        return {
            "status": "error",
            "profile": profile.name,
            "error": "exceeded retry budget",
            "summary": f"Exasol query failed for profile {profile.name}.",
        }

    def _serialize_profile(self, profile: ExasolProfile) -> dict[str, Any]:
        payload = profile.to_dict()
        payload["secret_ref"] = {
            "provider": profile.secret_ref.provider,
            "key": profile.secret_ref.key,
            "exposed_value": False,
        }
        return payload

    def _validate_backend_mode(self, backend: str, credential_mode: str) -> None:
        allowed = {
            "onprem": {"password", "access_token", "refresh_token"},
            "saas": {"saas_pat"},
        }
        if backend not in allowed:
            raise self._field_error("backend", "must be onprem or saas.")
        if credential_mode not in allowed[backend]:
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"Credential mode {credential_mode} is not valid for backend {backend}.",
                details={"field": "credential_mode", "backend": backend},
            )

    def _field_error(self, field_name: str, detail: str) -> DashServerError:
        return DashServerError(
            category="exasol_profile_validation_error",
            summary=f"{field_name} {detail}",
            details={"field": field_name},
        )

    def _validate_app_name(self, name: str, *, field_name: str) -> None:
        if not _APP_NAME_RE.match(name):
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"{field_name} must use lowercase letters, numbers, and hyphens.",
                details={"field": field_name},
            )

    def _humanize_name(self, name: str) -> str:
        return name.replace("-", " ").title()

    def _discover_schema_blueprint(
        self,
        profile_name: str,
        *,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        params = {"schema_name": schema_name} if schema_name else {}
        sql_text = (
            "SELECT COLUMN_SCHEMA,\n"
            "       COLUMN_TABLE,\n"
            "       COLUMN_NAME,\n"
            "       COLUMN_TYPE,\n"
            "       COLUMN_ORDINAL_POSITION\n"
            "FROM EXA_ALL_COLUMNS\n"
        )
        if schema_name:
            sql_text += "WHERE COLUMN_SCHEMA = {schema_name!s}\n"
        else:
            sql_text += (
                "WHERE COLUMN_SCHEMA NOT LIKE 'EXA_%'\n"
                "  AND COLUMN_SCHEMA NOT IN ('SYS')\n"
            )
        sql_text += "ORDER BY COLUMN_SCHEMA, COLUMN_TABLE, COLUMN_ORDINAL_POSITION\n"

        result = self.execute_profile_query(profile_name, sql_text, params=params)
        if result.get("status") != "ok":
            raise DashServerError(
                category="exasol_schema_scaffold_error",
                summary="Schema introspection failed for the selected Exasol profile.",
                details={"profile": profile_name, "query_result": result},
            )
        rows = self._records_from_result(result)
        if not rows:
            raise DashServerError(
                category="exasol_schema_scaffold_error",
                summary="Schema introspection returned no visible business tables.",
                details={"profile": profile_name, "schema_name": schema_name},
            )

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            resolved_schema = str(row.get("COLUMN_SCHEMA") or "")
            resolved_table = str(row.get("COLUMN_TABLE") or "")
            if not resolved_schema or not resolved_table:
                continue
            grouped.setdefault((resolved_schema, resolved_table), []).append(row)
        if not grouped:
            raise DashServerError(
                category="exasol_schema_scaffold_error",
                summary="Schema introspection did not return usable column metadata.",
                details={"profile": profile_name, "schema_name": schema_name},
            )

        candidates = []
        for (resolved_schema, resolved_table), column_rows in grouped.items():
            classified = self._classify_table_columns(column_rows)
            score = (
                8 * int(bool(classified["measure_columns"]))
                + 5 * int(bool(classified["time_column"]))
                + 3 * int(bool(classified["dimension_column"]))
                + min(len(column_rows), 12)
            )
            candidates.append(
                {
                    "schema_name": resolved_schema,
                    "table_name": resolved_table,
                    "score": score,
                    **classified,
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["schema_name"], item["table_name"]))
        selected = candidates[0]
        if table_name:
            override = next(
                (cand for cand in candidates if cand["table_name"] == table_name),
                None,
            )
            if override is None:
                raise DashServerError(
                    category="exasol_schema_scaffold_error",
                    summary=(
                        f"Table {table_name} was not found in the introspected schema."
                    ),
                    details={
                        "profile": profile_name,
                        "schema_name": schema_name,
                        "table_name": table_name,
                        "available_tables": sorted({cand["table_name"] for cand in candidates}),
                    },
                )
            selected = override

        relationship_hints = self._relationship_hints(candidates, selected)
        business_caption = (
            f"Seeded from {selected['schema_name']}.{selected['table_name']}. "
            "Review the generated SQL against business semantics before promotion."
        )
        return {
            "schema_name": selected["schema_name"],
            "table_name": selected["table_name"],
            "time_column": selected["time_column"],
            "dimension_column": selected["dimension_column"],
            "primary_measure": selected["primary_measure"],
            "measure_columns": selected["measure_columns"],
            "business_caption": business_caption,
            "summary_heading": f"{selected['table_name']} KPI Snapshot",
            "chart_heading": (
                f"{selected['primary_measure']} over {selected['time_column']}"
                if selected["primary_measure"] and selected["time_column"]
                else f"{selected['table_name']} Trend"
            ),
            "table_heading": f"Recent {selected['table_name']} Rows",
            "relationship_hints": relationship_hints,
            "table_candidates": [
                {
                    "schema_name": candidate["schema_name"],
                    "table_name": candidate["table_name"],
                    "score": candidate["score"],
                }
                for candidate in candidates[:5]
            ],
        }

    def _classify_table_columns(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        time_columns: list[str] = []
        measure_columns: list[str] = []
        dimension_columns: list[str] = []
        key_columns: list[str] = []
        for row in rows:
            column_name = str(row.get("COLUMN_NAME") or "")
            column_type = str(row.get("COLUMN_TYPE") or "").upper()
            if not column_name:
                continue
            normalized_name = column_name.upper()
            if self._is_temporal_type(column_type):
                time_columns.append(column_name)
            elif self._is_numeric_type(column_type) and not normalized_name.endswith("_ID") and normalized_name != "ID":
                measure_columns.append(column_name)
            elif self._is_dimension_type(column_type):
                dimension_columns.append(column_name)
            if normalized_name.endswith("_ID") or normalized_name == "ID":
                key_columns.append(column_name)

        dimension_column = next(
            (
                column_name
                for column_name in dimension_columns
                if column_name.upper() not in {name.upper() for name in key_columns}
            ),
            dimension_columns[0] if dimension_columns else (key_columns[0] if key_columns else None),
        )
        return {
            "time_column": time_columns[0] if time_columns else None,
            "dimension_column": dimension_column,
            "primary_measure": measure_columns[0] if measure_columns else None,
            "measure_columns": measure_columns,
            "key_columns": key_columns,
        }

    def _relationship_hints(
        self,
        candidates: list[dict[str, Any]],
        selected: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Find foreign tables that share a key column with `selected`.

        Each hint carries the foreign table's own `time_column` / `dimension_column` so
        the SQL emitter can decide whether to join into a related table (e.g. to reach
        a `ORDER_DATE` column on `ORDERS` from a single-line `ORDER_LINES` row). The
        SCHEMA_NOTES.md generator and the trend-SQL emitter both consume these fields.
        """

        hints: list[dict[str, Any]] = []
        selected_keys = {column_name.upper(): column_name for column_name in selected.get("key_columns", [])}
        if not selected_keys:
            return hints
        for candidate in candidates[1:]:
            for key_name in candidate.get("key_columns", []):
                upper_name = key_name.upper()
                if upper_name in selected_keys:
                    hints.append(
                        {
                            "column_name": selected_keys[upper_name],
                            "other_schema": candidate["schema_name"],
                            "other_table": candidate["table_name"],
                            "other_time_column": candidate.get("time_column"),
                            "other_dimension_column": candidate.get("dimension_column"),
                            "other_key_column": key_name,
                        }
                    )
        return hints[:5]

    def _is_temporal_type(self, column_type: str) -> bool:
        return any(token in column_type for token in ("DATE", "TIME", "TIMESTAMP"))

    def _is_numeric_type(self, column_type: str) -> bool:
        return any(token in column_type for token in ("DECIMAL", "DOUBLE", "FLOAT", "NUMBER", "INT"))

    def _is_dimension_type(self, column_type: str) -> bool:
        return any(token in column_type for token in ("CHAR", "VARCHAR", "STRING", "CLOB", "TEXT"))

    def _records_from_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = result.get("rows")
        columns = result.get("columns")
        if not isinstance(rows, list) or not isinstance(columns, list):
            return []
        records: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                records.append(row)
            elif isinstance(row, list):
                records.append({str(column): value for column, value in zip(columns, row, strict=False)})
        return records

    def _fetch_bounded_rows(self, statement: Any, row_limit: int) -> tuple[list[list[Any]], bool]:
        """Fetch at most `row_limit + 1` rows and report whether more existed.

        PS26-BUG-004: the previous implementation always called `_fetch_rows`
        (effectively `statement.fetchall()`), pulling the *entire* result set into
        Python before slicing to `row_limit` — for a query with no SQL-level
        `LIMIT` (an accidental cross join, a bad join condition, an adversarial
        parameter), that meant computing and transmitting an unbounded number of
        rows just to throw most of them away. `fetchmany(row_limit + 1)` asks the
        driver for only as many rows as we can ever show plus one (to detect
        truncation), so an oversized result stops costing dash-server (and, once
        the statement is closed right after in the caller, ideally Exasol too)
        proportional to its real size rather than its full size.

        This does not replace `statement_timeout_seconds` (see
        `ExasolConnectionManager.statement_timeout_seconds`) as the primary
        defense against a runaway query — a result that's expensive to *compute*
        server-side (as opposed to expensive to transfer) can still run long
        before the first `fetchmany` call returns at all. The two guardrails are
        complementary: the timeout bounds execution time, this bounds how much of
        the result dash-server ever pulls into process memory.

        Falls back to the old fetchall()-then-slice behavior when `statement`
        doesn't implement `fetchmany` (some lightweight test doubles don't).
        """

        fetchmany = getattr(statement, "fetchmany", None)
        if callable(fetchmany):
            rows = self._normalize_rows(fetchmany(row_limit + 1))
        else:
            rows = self._fetch_rows(statement)
        return rows[:row_limit], len(rows) > row_limit

    def _fetch_rows(self, statement: Any) -> list[list[Any]]:
        # `statement` is `Any` rather than `ExaStatementLike` because tests and the
        # `_extract_columns` retries pass non-pyexasol objects too; the body probes
        # for fetchall/list shape rather than nominally type-checking.
        if hasattr(statement, "fetchall"):
            rows = statement.fetchall()
        elif isinstance(statement, list):
            rows = statement
        else:
            rows = list(statement)
        return self._normalize_rows(rows)

    def _normalize_rows(self, rows: Any) -> list[list[Any]]:
        normalized: list[list[Any]] = []
        for row in rows:
            if isinstance(row, tuple):
                normalized.append(list(row))
            elif isinstance(row, list):
                normalized.append(row)
            else:
                normalized.append([row])
        return normalized

    def _extract_columns(self, statement: Any) -> list[str]:
        column_names = getattr(statement, "column_names", None)
        if callable(column_names):
            try:
                value = column_names()
                if isinstance(value, list):
                    return [str(c) for c in value]
            except Exception:
                pass
        elif isinstance(column_names, list):
            return [str(c) for c in column_names]
        columns_attr = getattr(statement, "columns", None)
        if callable(columns_attr):
            try:
                value = columns_attr()
                if isinstance(value, dict):
                    return list(value.keys())
                if isinstance(value, list):
                    return [str(c) for c in value]
            except Exception:
                pass
        elif isinstance(columns_attr, list) and all(isinstance(item, str) for item in columns_attr):
            return list(columns_attr)
        description = getattr(statement, "description", None)
        if isinstance(description, (list, tuple)):
            columns: list[str] = []
            for item in description:
                if isinstance(item, (list, tuple)) and item:
                    columns.append(str(item[0]))
            if columns:
                return columns
        return []


_DEAD_SESSION_HINTS = (
    "connection reset",
    "broken pipe",
    "websocket connection is already closed",
    "websocket is closed",
    "connection is closed",
    "session not found",
    "session is not found",
    "no connection",
    "remote end closed connection",
)


def _looks_like_dead_session(exc: Exception) -> bool:
    """Return True when `exc` looks like a stale cached pyexasol session.

    Used by `execute_profile_query`'s retry loop to decide whether to invalidate
    the cached connection and try once more. We keep the heuristic deliberately
    narrow — only obviously connection-level errors flip the bit, so SQL syntax
    errors and missing-table errors fail fast without an extra reconnect.
    """

    text = str(exc).lower()
    return any(hint in text for hint in _DEAD_SESSION_HINTS)
