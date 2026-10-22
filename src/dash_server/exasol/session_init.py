"""Per-profile session init statements, run on every new Exasol connection.

Some Exasol features are configured per session rather than per query. The main case is
Exasol Semantic Views, whose Semantic SQL only works after the session activates its
preprocessor (`ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = ...` or
`EXECUTE SCRIPT SEMANTIC_ADMIN.ENABLE_SEMANTIC_SQL()`). dash-server opens and replaces
sessions itself (thread cache, idle eviction, reconnect after errors, uncached smoke and
export connections), so hosted apps can't do this reliably. A profile declares the
statements instead (`query_defaults.session_init_sql`), and every connection the
connection manager opens runs them first: the equivalent of a BI tool's "initial SQL".

Only session-scoped statements are accepted, since they run on every connection.
"""

from __future__ import annotations

import re
from typing import Any

from dash_server.exasol.models import ExasolProfile

MAX_SESSION_INIT_STATEMENTS = 10

_ALLOWED_STATEMENT = re.compile(r"(ALTER\s+SESSION|EXECUTE\s+SCRIPT|OPEN\s+SCHEMA)\b", re.IGNORECASE)


def split_sql_statements(text: str) -> list[str]:
    """Split ``text`` on ``;`` outside quoted literals and identifiers; drop empty parts."""

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == ";":
            statements.append("".join(current))
            current = []
            continue
        current.append(char)
    statements.append("".join(current))
    return [statement.strip() for statement in statements if statement.strip()]


def normalize_session_init_sql(value: Any) -> list[str]:
    """Validate a ``session_init_sql`` value and return its statements, stripped.

    Raises ``ValueError`` with a message suitable for a field error.
    """

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("must be a list of SQL statement strings.")
    if len(value) > MAX_SESSION_INIT_STATEMENTS:
        raise ValueError(f"accepts at most {MAX_SESSION_INIT_STATEMENTS} statements.")
    statements: list[str] = []
    for index, item in enumerate(value, start=1):
        parts = split_sql_statements(item)
        if not parts:
            raise ValueError(f"statement {index} is empty.")
        if len(parts) > 1:
            raise ValueError(f"statement {index} contains more than one statement; use one list entry each.")
        statement = parts[0]
        if not _ALLOWED_STATEMENT.match(statement):
            raise ValueError(
                f"statement {index} must be a session statement (ALTER SESSION, EXECUTE SCRIPT, "
                "or OPEN SCHEMA), because it runs on every new connection."
            )
        statements.append(statement)
    return statements


def session_init_statements(profile: ExasolProfile) -> list[str]:
    """The profile's session init statements; empty when none (or malformed) are stored."""

    value = (profile.query_defaults or {}).get("session_init_sql")
    if not isinstance(value, list):
        return []
    return [statement for statement in value if isinstance(statement, str) and statement.strip()]


__all__ = [
    "MAX_SESSION_INIT_STATEMENTS",
    "normalize_session_init_sql",
    "session_init_statements",
    "split_sql_statements",
]
