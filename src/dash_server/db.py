"""Shared SQLite connection discipline.

The registry and the consumption store write to the same database file from
multiple threads (request handlers plus the consumption worker pool). Every
connection to that file must therefore agree on journal mode and lock
patience, and must actually be closed — ``sqlite3``'s own connection context
manager commits or rolls back but never closes.

`open_connection` is the one way to touch the file: WAL so readers proceed
while a writer writes, a generous busy timeout instead of instant
``database is locked`` failures, commit on success, rollback on exception,
close always.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def open_connection(
    db_path: str | Path,
    *,
    foreign_keys: bool = False,
    timeout: float = 30.0,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path, timeout=timeout)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_column(
    db_path: str | Path,
    table_name: str,
    column_name: str,
    column_type: str,
    *,
    foreign_keys: bool = False,
) -> None:
    """Idempotent guarded ALTER shared by the registry and consumption stores."""
    with open_connection(db_path, foreign_keys=foreign_keys) as connection:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if column_name not in {row["name"] for row in rows}:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


__all__ = ["ensure_column", "open_connection"]
