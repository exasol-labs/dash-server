"""Wave 2 item 4: registry/consumption schema ownership and migration ledgers.

Covers the plan's acceptance gate — "a fresh database is fully constructed by
each store's own initializer in any order; both ledgers refuse newer-than-code
schemas" — plus the write-hygiene invariant that ``next_revision_number`` is
assigned atomically inside the inserting transaction.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dash_server.consumption import store as consumption_store
from dash_server.consumption.store import ConsumptionStore
from dash_server.db import open_connection
from dash_server.registry import sqlite_registry
from dash_server.registry.models import AppManifest
from dash_server.registry.sqlite_registry import SQLiteAppRegistry

_REGISTRY_TABLES = {
    "apps",
    "app_revisions",
    "app_events",
    "users",
    "groups",
    "group_memberships",
    "app_acl_entries",
    "app_share_policies",
    "access_audit_events",
    "share_links",
    "app_invitations",
    "registry_schema_migrations",
}

_CONSUMPTION_TABLES = {
    "consumption_jobs",
    "consumption_artifacts",
    "consumption_subscriptions",
    "consumption_alerts",
    "consumption_delivery_attempts",
    "consumption_audit_events",
    "consumption_coordinator",
    "consumption_schema_migrations",
}


def _tables(db_path: Path) -> set[str]:
    with open_connection(db_path) as connection:
        return {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _max_version(db_path: Path, table: str) -> int | None:
    with open_connection(db_path) as connection:
        row = connection.execute(f"SELECT MAX(version) AS version FROM {table}").fetchone()
    return row["version"] if row else None


def _manifest(name: str = "app1") -> AppManifest:
    return AppManifest(
        name=name,
        title=name.title(),
        route=f"/apps/{name}",
        description="",
        template="metric-cards",
    )


def _bundle() -> dict:
    return {"source_files": []}


def test_fresh_db_registry_then_store(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    SQLiteAppRegistry(str(db)).initialize()
    ConsumptionStore(db).initialize()

    tables = _tables(db)
    assert tables >= _REGISTRY_TABLES
    assert tables >= _CONSUMPTION_TABLES
    assert _max_version(db, "registry_schema_migrations") == sqlite_registry._SCHEMA_VERSION
    assert _max_version(db, "consumption_schema_migrations") == consumption_store._SCHEMA_VERSION


def test_fresh_db_store_then_registry(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    ConsumptionStore(db).initialize()
    SQLiteAppRegistry(str(db)).initialize()

    tables = _tables(db)
    assert tables >= _REGISTRY_TABLES
    assert tables >= _CONSUMPTION_TABLES
    assert _max_version(db, "registry_schema_migrations") == sqlite_registry._SCHEMA_VERSION
    assert _max_version(db, "consumption_schema_migrations") == consumption_store._SCHEMA_VERSION


def test_store_only_db_has_no_registry_tables(tmp_path: Path):
    """The consumption store owns its tables without help from the registry."""
    db = tmp_path / "db.sqlite3"
    ConsumptionStore(db).initialize()

    tables = _tables(db)
    assert tables >= _CONSUMPTION_TABLES
    assert "consumption_jobs" in tables
    # The store must not depend on the registry having created anything.
    assert "apps" not in tables


def test_registry_only_db_has_no_consumption_tables(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    SQLiteAppRegistry(str(db)).initialize()

    tables = _tables(db)
    assert tables >= _REGISTRY_TABLES
    assert not (tables & (_CONSUMPTION_TABLES - {"consumption_schema_migrations"}))


def test_delete_app_tolerates_absent_consumption_tables(tmp_path: Path):
    """delete_app must not blow up when only the registry has initialized."""
    db = tmp_path / "db.sqlite3"
    registry = SQLiteAppRegistry(str(db))
    registry.initialize()
    registry.create_app(
        _manifest("solo"),
        _bundle(),
        status="running",
        artifact_path="/tmp/solo",
        source_hash="h",
        dependency_lock_hash="l",
    )
    assert registry.delete_app("solo") is True
    assert registry.get_app("solo") is None


def test_registry_ledger_refuses_newer_schema(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    SQLiteAppRegistry(str(db)).initialize()
    with open_connection(db) as connection:
        connection.execute(
            "INSERT INTO registry_schema_migrations (version, applied_at) VALUES (?, ?)",
            (sqlite_registry._SCHEMA_VERSION + 1, "2026-01-01T00:00:00Z"),
        )
    with pytest.raises(RuntimeError, match="newer"):
        SQLiteAppRegistry(str(db)).initialize()


def test_consumption_ledger_refuses_newer_schema(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    ConsumptionStore(db).initialize()
    with open_connection(db) as connection:
        connection.execute(
            "INSERT INTO consumption_schema_migrations (version, applied_at) VALUES (?, ?)",
            (consumption_store._SCHEMA_VERSION + 1, "2026-01-01T00:00:00Z"),
        )
    with pytest.raises(RuntimeError, match="newer"):
        ConsumptionStore(db).initialize()


def test_registry_ledger_idempotent_on_reinitialize(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    SQLiteAppRegistry(str(db)).initialize()
    SQLiteAppRegistry(str(db)).initialize()
    SQLiteAppRegistry(str(db)).initialize()

    with open_connection(db) as connection:
        rows = connection.execute("SELECT version FROM registry_schema_migrations ORDER BY version").fetchall()
    assert [row["version"] for row in rows] == [sqlite_registry._SCHEMA_VERSION]


def test_next_revision_number_monotonic_serial(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    registry = SQLiteAppRegistry(str(db))
    registry.initialize()
    _, first = registry.create_app(
        _manifest("mono"),
        _bundle(),
        status="running",
        artifact_path="/tmp/mono",
        source_hash="h",
        dependency_lock_hash="l",
    )
    assert first.revision_number == 1

    numbers = [first.revision_number]
    for _ in range(5):
        revision = registry.create_revision(
            "mono",
            _manifest("mono"),
            _bundle(),
            artifact_path="/tmp/mono",
            source_hash="h",
            dependency_lock_hash="l",
        )
        numbers.append(revision.revision_number)
    assert numbers == [1, 2, 3, 4, 5, 6]


def test_next_revision_number_atomic_under_concurrency(tmp_path: Path):
    """Concurrent create_revision calls must each get a unique, gapless number."""
    db = tmp_path / "db.sqlite3"
    registry = SQLiteAppRegistry(str(db))
    registry.initialize()
    registry.create_app(
        _manifest("race"),
        _bundle(),
        status="running",
        artifact_path="/tmp/race",
        source_hash="h",
        dependency_lock_hash="l",
    )

    def make_revision(_: int) -> int:
        revision = registry.create_revision(
            "race",
            _manifest("race"),
            _bundle(),
            artifact_path="/tmp/race",
            source_hash="h",
            dependency_lock_hash="l",
        )
        return revision.revision_number

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(make_revision, range(8)))

    # First revision from create_app is 1; the eight concurrent inserts must be
    # exactly 2..9 with no duplicates and no gaps despite racing.
    assert sorted(numbers) == list(range(2, 10))
    assert len(set(numbers)) == 8
