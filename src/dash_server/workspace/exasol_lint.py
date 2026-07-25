"""Exasol-specific static linting for draft workspaces.

Builds the ``exasol`` report used by ``WorkspaceService.validate_workspace``
for Exasol-backed apps: flags direct pyexasol usage, import-time query
execution, reserved-word SQL aliases, unbounded queries, and dead SQL files.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from dash_server.registry.models import AppManifest

_EXASOL_QUERY_CALLS = {
    "execute_profile_query",
    "query_rows",
    "query_one",
    "query_scalar",
    "load_rows",
    "load_row",
    "load_scalar",
}

# Common Exasol reserved words that bite scaffold authors when used as bare AS
# aliases. Exasol's full reserved list is much larger; these are the ones we've
# actually seen trip up persona-1 / persona-2 / persona-3 SQL.
_RESERVED_ALIAS_WORDS = {
    "DAY", "MONTH", "YEAR", "HOUR", "MINUTE", "SECOND",
    "ORDER", "GROUP", "LEVEL", "USER", "TIMESTAMP", "DATE",
    "TYPE", "VALUE", "ROW", "RANK", "COUNT",
}
_RESERVED_ALIAS_RE = re.compile(
    r"\bAS\s+(" + "|".join(sorted(_RESERVED_ALIAS_WORDS)) + r")\b",
    re.IGNORECASE,
)


def is_exasol_workspace(manifest: AppManifest) -> bool:
    data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else {}
    primary = data_sources.get("primary")
    return bool(
        manifest.template == "exasol-analytics"
        or (isinstance(primary, dict) and primary.get("kind") == "exasol")
    )


def exasol_validation_report(
    files: dict[str, str],
    manifest: AppManifest,
    *,
    python_files: dict[str, str],
) -> dict[str, Any]:
    if not is_exasol_workspace(manifest):
        return {"status": "not_applicable", "issues": []}

    issues: list[dict[str, Any]] = []

    for relative_path, content in python_files.items():
        if "import pyexasol" in content or "from pyexasol" in content:
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "message": "Exasol-backed hosted apps should rely on the server helper path instead of importing pyexasol directly.",
                }
            )
        try:
            tree = ast.parse(content, filename=relative_path)
        except SyntaxError:
            continue
        for node in tree.body:
            call = _top_level_call(node)
            if call is None:
                continue
            if _call_matches_names(call.func, _EXASOL_QUERY_CALLS):
                issues.append(
                    {
                        "level": "error",
                        "path": relative_path,
                        "line": getattr(call, "lineno", None),
                        "message": "Do not execute Exasol queries at import time. Run them inside callbacks or explicit request handlers.",
                    }
                )

    for relative_path, content in files.items():
        if not relative_path.startswith("queries/") or not relative_path.endswith(".sql"):
            continue
        normalized = content.upper()
        for match in _RESERVED_ALIAS_RE.finditer(content):
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "line": content[: match.start()].count("\n") + 1,
                    "message": (
                        f"AS {match.group(1).upper()} uses an Exasol reserved word as a bare alias. "
                        f'Quote it: AS "{match.group(1).upper()}".'
                    ),
                }
            )
        if "SELECT *" in normalized:
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "message": "Avoid SELECT * in Exasol query files. Select only the columns the dashboard needs.",
                }
            )
        stripped = content.strip()
        if ";" in stripped[:-1]:
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "message": "Prefer one statement per Exasol SQL file.",
                }
            )
        from_clause = " " + re.sub(r"\s+", " ", normalized).strip() + " "
        # Skip the "no LIMIT or aggregation" warning for single-row scalar queries
        # that come only from DUAL (e.g. the scaffold's placeholder
        # `SELECT 1240 AS ACTIVE_CUSTOMERS FROM DUAL`).
        only_from_dual = (
            " FROM DUAL " in from_clause
            and " JOIN " not in from_clause
            and " UNION " not in from_clause
        )
        if (
            " FROM " in from_clause
            and " LIMIT " not in from_clause
            and " GROUP BY " not in from_clause
            and not any(token in normalized for token in ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX("))
            and not only_from_dual
        ):
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "message": "This query does not declare LIMIT or obvious aggregation. Ensure Exasol is doing bounded or aggregated work before returning rows.",
                }
            )

    # Dead SQL detection: queries/*.sql files that no .py file references.
    sql_files = {p for p in files if p.startswith("queries/") and p.endswith(".sql")}
    if sql_files:
        referenced: set[str] = set()
        for relative_path, content in files.items():
            if relative_path.endswith(".py"):
                for sql_path in sql_files:
                    if sql_path in content:
                        referenced.add(sql_path)
        for sql_path in sorted(sql_files - referenced):
            issues.append(
                {
                    "level": "info",
                    "path": sql_path,
                    "message": (
                        f"{sql_path} is not referenced by any .py file in the workspace. "
                        "Delete unused SQL files with app_delete_file to keep the workspace tidy."
                    ),
                }
            )

    if any(issue["level"] == "error" for issue in issues):
        status = "failed"
    elif any(issue["level"] == "warning" for issue in issues):
        status = "passed_with_warnings"
    else:
        status = "passed"
    return {"status": status, "issues": issues}


def _top_level_call(node: ast.stmt) -> ast.Call | None:
    value = None
    if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
        value = node.value
    if isinstance(value, ast.Call):
        return value
    return None


def _call_matches_names(func: ast.AST, allowed_names: set[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in allowed_names
    if isinstance(func, ast.Attribute):
        return func.attr in allowed_names
    return False
