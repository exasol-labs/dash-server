"""Exasol-specific static linting for draft workspaces.

Builds the ``exasol`` report used by ``WorkspaceService.validate_workspace``
for Exasol-backed apps: flags direct pyexasol usage, import-time query
execution, reserved-word SQL aliases, unbounded queries, and dead SQL files.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from dash_server.exasol.semantic_models import looks_like_compiled_semantic_sql
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
    "DAY",
    "MONTH",
    "YEAR",
    "HOUR",
    "MINUTE",
    "SECOND",
    "ORDER",
    "GROUP",
    "LEVEL",
    "USER",
    "TIMESTAMP",
    "DATE",
    "TYPE",
    "VALUE",
    "ROW",
    "RANK",
    "COUNT",
}
_RESERVED_ALIAS_RE = re.compile(
    r"\bAS\s+(" + "|".join(sorted(_RESERVED_ALIAS_WORDS)) + r")\b",
    re.IGNORECASE,
)


# A query against an Exasol Semantic Views published schema (SEMANTIC_<MODEL>, the default
# naming): metrics aggregate implicitly, so the LIMIT/aggregation heuristic doesn't apply.
_SEMANTIC_SCHEMA_REF_RE = re.compile(r'\bFROM\s+"?SEMANTIC_[A-Z0-9_]+"?\s*\.', re.IGNORECASE)
_SQL_COMMENT_OR_LITERAL_RE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL)


def _without_comments_and_literals(sql: str) -> str:
    """``sql`` with comments and quoted strings/identifiers blanked, for structural checks."""

    return _SQL_COMMENT_OR_LITERAL_RE.sub(" ", sql)


def is_exasol_workspace(manifest: AppManifest) -> bool:
    data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else {}
    primary = data_sources.get("primary")
    return bool(
        manifest.template == "exasol-analytics" or (isinstance(primary, dict) and primary.get("kind") == "exasol")
    )


_EXASOL_HELPER_MODULES = {"dash_server_exasol", "dash_server.exasol.runtime", "dash_server.exasol.scaffold_helper"}

# Keys agents plausibly write for the binding that the manifest schema does not recognise
# (they are silently ignored). Named in the hint when the binding is missing.
_UNRECOGNISED_BINDING_KEYS = ("exasol", "profile", "profile_name", "data_source", "datasource", "datasources")

_BINDING_EXAMPLE = '"data_sources": {"primary": {"kind": "exasol", "profile": "<profile name>"}}'


def has_exasol_binding(manifest: AppManifest) -> bool:
    """Whether the manifest binds a primary Exasol profile the runtime helper can resolve."""

    data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else {}
    primary = data_sources.get("primary")
    return (
        isinstance(primary, dict)
        and primary.get("kind") == "exasol"
        and isinstance(primary.get("profile"), str)
        and bool(primary["profile"].strip())
    )


def _files_using_exasol_helper(python_files: dict[str, str]) -> list[str]:
    """App files that import the Exasol runtime helper (the helper module itself excluded)."""

    using: list[str] = []
    for relative_path, content in python_files.items():
        if relative_path == "dash_server_exasol.py":
            continue
        try:
            tree = ast.parse(content, filename=relative_path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(module in _EXASOL_HELPER_MODULES for module in modules):
                using.append(relative_path)
                break
    return using


def _binding_issue(
    manifest: AppManifest,
    files: dict[str, str],
    helper_users: list[str],
) -> dict[str, Any] | None:
    """BUG-30: an app that queries Exasol through the helper but binds no profile passes
    every probe (``sql_smoke`` has nothing to test without a profile) and then 500s on its
    first callback. Fail validation instead, so it is never built or published."""

    if has_exasol_binding(manifest):
        return None
    hint = f"Add {_BINDING_EXAMPLE} to dash-app.json (or pass data_sources to the tool)."
    try:
        raw_manifest = json.loads(files.get("dash-app.json") or "{}")
    except json.JSONDecodeError:
        raw_manifest = {}
    stray = [key for key in _UNRECOGNISED_BINDING_KEYS if isinstance(raw_manifest, dict) and key in raw_manifest]
    if stray:
        hint = (
            f"dash-app.json sets {', '.join(repr(key) for key in stray)}, which is not a recognised "
            f"manifest key and is ignored. Use {_BINDING_EXAMPLE} instead."
        )
    if helper_users:
        return {
            "level": "error",
            "path": "dash-app.json",
            "message": (
                f"{', '.join(helper_users)} {'queries' if len(helper_users) == 1 else 'query'} Exasol "
                "through the runtime helper, but the manifest "
                "declares no Exasol data source, so the first callback would fail with HTTP 500. " + hint
            ),
        }
    return {
        "level": "warning",
        "path": "dash-app.json",
        "message": (
            "template is exasol-analytics but the manifest declares no Exasol data source; any "
            "query through the runtime helper will fail. " + hint
        ),
    }


def exasol_validation_report(
    files: dict[str, str],
    manifest: AppManifest,
    *,
    python_files: dict[str, str],
) -> dict[str, Any]:
    helper_users = _files_using_exasol_helper(python_files)
    if not is_exasol_workspace(manifest) and not helper_users:
        return {"status": "not_applicable", "issues": []}

    issues: list[dict[str, Any]] = []
    binding_issue = _binding_issue(manifest, files, helper_users)
    if binding_issue is not None:
        issues.append(binding_issue)

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
            if _is_cast_target(content, match.start()):
                # PS26-BUG-017: `CAST(x AS DATE)` is cast syntax, not a column alias -
                # quoting the type (the warning's own suggested fix) would change what
                # the cast does. Only flag `AS <word>` when it isn't the type half of a
                # CAST(...).
                continue
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
        stripped = _without_comments_and_literals(content).strip()
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
        only_from_dual = " FROM DUAL " in from_clause and " JOIN " not in from_clause and " UNION " not in from_clause
        if looks_like_compiled_semantic_sql(content):
            issues.append(
                {
                    "level": "warning",
                    "path": relative_path,
                    "message": (
                        "This looks like SQL compiled by Exasol Semantic Views (COMPILE_SQL or "
                        "COMPILE_REQUEST_JSON output). Saved compiled SQL freezes today's metric definitions "
                        "(and, for a fused model, its coverage and authority): later fixes never reach this "
                        "dashboard, and nothing detects the drift. Query the published semantic view live "
                        "instead; see dash://exasol/help/semantic-layer."
                    ),
                }
            )
        if (
            " FROM " in from_clause
            and not _SEMANTIC_SCHEMA_REF_RE.search(content)
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


def _is_cast_target(content: str, match_start: int) -> bool:
    """True if the `AS <word>` at `match_start` is the type half of `CAST(expr AS type)`.

    No SQL parser here, just enough paren-tracking to answer "is this AS inside a
    CAST(...) whose opener is the word CAST": walk backward from the match counting
    parenthesis depth to find the nearest *unclosed* `(` enclosing it, then check
    whether the token immediately before that `(` is the whole word `CAST`.
    """

    depth = 0
    i = match_start - 1
    while i >= 0:
        char = content[i]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                before = content[:i].rstrip()
                if before.upper().endswith("CAST"):
                    boundary_index = len(before) - len("CAST") - 1
                    if boundary_index < 0 or not (before[boundary_index].isalnum() or before[boundary_index] == "_"):
                        return True
                return False
            depth -= 1
        i -= 1
    return False


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
