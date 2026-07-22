from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

from dash_server.db import ensure_column, open_connection


def test_open_connection_commits_closes_and_rolls_back(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    with open_connection(db) as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO t (value) VALUES ('kept')")

    # Committed without an explicit commit() and connection was closed.
    with open_connection(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert [row["value"] for row in connection.execute("SELECT value FROM t")] == ["kept"]

    try:
        with open_connection(db) as connection:
            connection.execute("INSERT INTO t (value) VALUES ('discarded')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with open_connection(db) as connection:
        assert [row["value"] for row in connection.execute("SELECT value FROM t")] == ["kept"]

    ensure_column(db, "t", "extra", "TEXT")
    ensure_column(db, "t", "extra", "TEXT")  # idempotent
    with open_connection(db) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(t)")}
    assert "extra" in columns


def test_concurrent_writers_and_readers_share_one_file_without_lock_errors(tmp_path: Path):
    """Wave 1 gate: consumption-worker-style writers must not starve registry-style readers."""
    db = tmp_path / "shared.sqlite3"
    with open_connection(db) as connection:
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, progress TEXT)")
        connection.executemany(
            "INSERT INTO jobs (progress) VALUES (?)", [("queued",)] * 8
        )

    def writer(job_id: int) -> None:
        for step in range(40):
            with open_connection(db, foreign_keys=True) as connection:
                connection.execute(
                    "UPDATE jobs SET progress = ? WHERE id = ?", (f"step-{step}", job_id)
                )

    def reader() -> int:
        seen = 0
        for _ in range(80):
            with open_connection(db) as connection:
                rows = connection.execute("SELECT progress FROM jobs").fetchall()
            seen += len(rows)
        return seen

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(writer, job_id) for job_id in range(1, 9)]
        futures += [pool.submit(reader) for _ in range(4)]
        for future in futures:
            future.result()  # sqlite3.OperationalError("database is locked") would surface here

    with open_connection(db) as connection:
        final = {row["progress"] for row in connection.execute("SELECT progress FROM jobs")}
    assert final == {"step-39"}


def test_open_connection_surfaces_real_errors(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    try:
        with open_connection(db) as connection:
            connection.execute("SELECT * FROM missing_table")
        raise AssertionError("expected OperationalError")
    except sqlite3.OperationalError:
        pass
