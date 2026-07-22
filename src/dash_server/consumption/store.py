"""SQLite persistence for consumption jobs, artifacts, and audit events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from dash_server.db import ensure_column, open_connection
from dash_server.timestamps import now_iso, parse_iso8601

from .models import ConsumptionArtifact, ConsumptionJob




_ACTIVE_STATUSES = ("queued", "running", "cancel_requested")
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "expired")

# Numbered consumption schema ledger. Each entry is applied at most once and
# recorded in consumption_schema_migrations; the individual steps stay
# idempotent (guarded ALTER/CREATE IF NOT EXISTS) so a partially applied
# migration can be re-run safely after a crash.
_SCHEMA_VERSION = 2


class ConsumptionStore:
    """Small transactional store over the registry database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        applied = self._applied_schema_version()
        if applied > _SCHEMA_VERSION:
            raise RuntimeError(
                "The consumption database schema "
                f"(version {applied}) is newer than this server supports "
                f"(version {_SCHEMA_VERSION}). Refusing to run against a downgraded install."
            )
        if applied < 1:
            self._apply_phase1_schema()
            self._record_schema_version(1)
        if applied < 2:
            self._apply_phase2_schema()
            self._record_schema_version(2)

    def _applied_schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) AS version FROM consumption_schema_migrations").fetchone()
        return int(row["version"]) if row and row["version"] is not None else 0

    def _record_schema_version(self, version: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO consumption_schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, now_iso()),
            )
            connection.commit()

    def _apply_phase1_schema(self) -> None:
        with self._connect() as connection:
            # Base consumption tables. These were historically created by the
            # registry; the consumption store now owns them end to end so a
            # fresh database is fully constructed by whichever initializer runs.
            # Phase 1 ALTERs below then add the store's own columns.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_jobs (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    output_id TEXT NOT NULL,
                    job_type TEXT NOT NULL DEFAULT 'export',
                    requested_by_principal_id TEXT NOT NULL,
                    run_as_principal_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    output_contract_hash TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    parameters_hash TEXT NOT NULL DEFAULT '',
                    requested_format TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    idempotency_key TEXT,
                    error_json TEXT,
                    subscription_id TEXT,
                    alert_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS consumption_jobs_idempotency
                ON consumption_jobs(requested_by_principal_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER,
                    page_count INTEGER,
                    classification TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT NOT NULL,
                    deleted_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_subscriptions (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    output_id TEXT NOT NULL,
                    owner_principal_id TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    requested_format TEXT NOT NULL,
                    schedule_expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    revision_policy TEXT NOT NULL DEFAULT 'follow_live',
                    delivery_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'enabled',
                    pause_reason TEXT,
                    misfire_policy TEXT NOT NULL DEFAULT 'coalesce_one',
                    next_run_at TEXT,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_alerts (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    output_id TEXT NOT NULL,
                    owner_principal_id TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    schedule_expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    condition_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'enabled',
                    state TEXT NOT NULL DEFAULT 'unknown',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    next_run_at TEXT,
                    last_evaluated_at TEXT,
                    last_notified_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_delivery_attempts (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    job_id TEXT,
                    artifact_id TEXT,
                    subscription_id TEXT,
                    alert_id TEXT,
                    recipient_principal_id TEXT,
                    recipient_email_normalized TEXT,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_message_id TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    delivered_at TEXT,
                    failed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_principal_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    job_id TEXT,
                    artifact_id TEXT,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        self._ensure_column("consumption_jobs", "context_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("consumption_jobs", "output_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("consumption_jobs", "parameters_redacted_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("consumption_jobs", "effective_limits_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("consumption_jobs", "cancel_requested_at", "TEXT")
        self._ensure_column("consumption_jobs", "expires_at", "TEXT")

    def _apply_phase2_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumption_coordinator (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    owner TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS consumption_jobs_status ON consumption_jobs(status)"
            )
            connection.commit()

    def create_job(
        self,
        *,
        job: ConsumptionJob,
        encoded_parameters: str,
        context: dict[str, Any],
        redacted_parameters: dict[str, Any],
    ) -> ConsumptionJob:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO consumption_jobs (
                    id, app_name, output_id, requested_by_principal_id,
                    run_as_principal_id, revision_number, output_contract_hash,
                    policy_version, parameters_json, parameters_hash,
                    parameters_redacted_json, requested_format, status,
                    progress_json, idempotency_key, context_json, output_json,
                    effective_limits_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.app_name,
                    job.output_id,
                    job.requested_by_principal_id,
                    job.run_as_principal_id,
                    job.revision_number,
                    job.output_contract_hash,
                    job.policy_version,
                    encoded_parameters,
                    job.parameters_hash,
                    json.dumps(redacted_parameters, sort_keys=True),
                    job.requested_format,
                    job.status,
                    json.dumps(job.progress, sort_keys=True),
                    job.idempotency_key,
                    json.dumps(context, sort_keys=True),
                    json.dumps(job.output, sort_keys=True),
                    json.dumps(job.effective_limits, sort_keys=True),
                    job.created_at,
                ),
            )
            connection.commit()
        return job

    def get_job(self, job_id: str, *, decoded_parameters: dict[str, Any] | None = None) -> ConsumptionJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM consumption_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row, decoded_parameters=decoded_parameters) if row else None

    def get_encoded_parameters(self, job_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT parameters_json FROM consumption_jobs WHERE id = ?", (job_id,)).fetchone()
        return str(row["parameters_json"]) if row else None

    def find_by_idempotency(self, principal_id: str, key: str) -> ConsumptionJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM consumption_jobs
                WHERE requested_by_principal_id = ? AND idempotency_key = ?
                """,
                (principal_id, key),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, principal_id: str, *, app_name: str | None = None, limit: int = 50) -> list[ConsumptionJob]:
        sql = "SELECT * FROM consumption_jobs WHERE requested_by_principal_id = ?"
        params: list[Any] = [principal_id]
        if app_name is not None:
            sql += " AND app_name = ?"
            params.append(app_name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def transition_job(
        self,
        job_id: str,
        *,
        expected: tuple[str, ...],
        status: str,
        progress: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_expires_at: str | None = None,
    ) -> bool:
        assignments = ["status = ?"]
        values: list[Any] = [status]
        if progress is not None:
            assignments.append("progress_json = ?")
            values.append(json.dumps(progress, sort_keys=True))
        if error is not None:
            assignments.append("error_json = ?")
            values.append(json.dumps(error, sort_keys=True))
        if status == "running":
            assignments.extend(["started_at = ?", "attempt_count = attempt_count + 1"])
            values.append(now_iso())
        if lease_owner is not None:
            assignments.extend(["lease_owner = ?", "lease_expires_at = ?"])
            values.extend([lease_owner, lease_expires_at])
        if status in _TERMINAL_STATUSES or status == "queued":
            assignments.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
        if status in _TERMINAL_STATUSES:
            assignments.append("finished_at = ?")
            values.append(now_iso())
        placeholders = ",".join("?" for _ in expected)
        values.extend([job_id, *expected])
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE consumption_jobs SET {', '.join(assignments)} WHERE id = ? AND status IN ({placeholders})",
                values,
            )
            connection.commit()
        return cursor.rowcount == 1

    def update_progress(
        self,
        job_id: str,
        progress: dict[str, Any],
        *,
        lease_owner: str,
        lease_expires_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE consumption_jobs
                SET progress_json = ?, lease_owner = ?, lease_expires_at = ?
                WHERE id = ? AND status IN ('running', 'cancel_requested')
                """,
                (json.dumps(progress, sort_keys=True), lease_owner, lease_expires_at, job_id),
            )
            connection.commit()

    def count_active_jobs(self, *, principal_id: str | None = None, app_name: str | None = None) -> int:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        sql = f"SELECT COUNT(*) AS active FROM consumption_jobs WHERE status IN ({placeholders})"
        params: list[Any] = list(_ACTIVE_STATUSES)
        if principal_id is not None:
            sql += " AND requested_by_principal_id = ?"
            params.append(principal_id)
        if app_name is not None:
            sql += " AND app_name = ?"
            params.append(app_name)
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["active"])

    def list_incomplete_jobs(self) -> list[ConsumptionJob]:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM consumption_jobs WHERE status IN ({placeholders}) ORDER BY created_at",
                _ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_app_jobs(self, app_name: str, *, limit: int = 200) -> list[tuple[ConsumptionJob, dict[str, Any]]]:
        """All principals' jobs for one app, with redacted parameter summaries."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM consumption_jobs WHERE app_name = ? ORDER BY created_at DESC LIMIT ?",
                (app_name, limit),
            ).fetchall()
        results: list[tuple[ConsumptionJob, dict[str, Any]]] = []
        for row in rows:
            redacted = json.loads(row["parameters_redacted_json"] or "{}")
            results.append((self._row_to_job(row), redacted if isinstance(redacted, dict) else {}))
        return results

    def prune_expired_jobs(self, *, finished_before: str, audit_before: str) -> tuple[int, list[ConsumptionArtifact]]:
        """Delete terminal jobs (releasing their idempotency keys) and old audit rows.

        Returns the pruned-job count and the artifact rows removed alongside
        their jobs so the caller can delete any remaining stored files.
        """
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        with self._connect() as connection:
            job_rows = connection.execute(
                f"""
                SELECT id FROM consumption_jobs
                WHERE status IN ({placeholders}) AND finished_at IS NOT NULL AND finished_at <= ?
                """,
                (*_TERMINAL_STATUSES, finished_before),
            ).fetchall()
            job_ids = [row["id"] for row in job_rows]
            artifacts: list[ConsumptionArtifact] = []
            for job_id in job_ids:
                artifact_rows = connection.execute(
                    "SELECT * FROM consumption_artifacts WHERE job_id = ?", (job_id,)
                ).fetchall()
                artifacts.extend(self._row_to_artifact(row) for row in artifact_rows)
                connection.execute("DELETE FROM consumption_artifacts WHERE job_id = ?", (job_id,))
                connection.execute("DELETE FROM consumption_jobs WHERE id = ?", (job_id,))
            connection.execute("DELETE FROM consumption_audit_events WHERE created_at <= ?", (audit_before,))
            connection.commit()
        return len(job_ids), artifacts

    def claim_coordinator(
        self,
        *,
        owner: str,
        pid: int,
        stale_after_seconds: int,
        is_pid_alive: Any,
    ) -> None:
        """Claim the single-process coordinator slot or raise if another live process holds it."""
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM consumption_coordinator WHERE slot = 1").fetchone()
            if row is not None and row["owner"] != owner and int(row["pid"]) != pid:
                heartbeat = parse_iso8601(row["heartbeat_at"])
                fresh = heartbeat is not None and (now - heartbeat).total_seconds() < stale_after_seconds
                if fresh and bool(is_pid_alive(int(row["pid"]))):
                    raise RuntimeError(
                        "Another dash-server process "
                        f"(pid {row['pid']}) already runs the local consumption coordinator against "
                        f"{self.db_path}. The Phase 1/2 coordinator is single-process only; refusing "
                        "to start a duplicate runner."
                    )
            connection.execute(
                """
                INSERT INTO consumption_coordinator (slot, owner, pid, started_at, heartbeat_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT (slot) DO UPDATE SET
                    owner = excluded.owner,
                    pid = excluded.pid,
                    started_at = excluded.started_at,
                    heartbeat_at = excluded.heartbeat_at
                """,
                (owner, pid, now_iso(), now_iso()),
            )
            connection.commit()

    def heartbeat_coordinator(self, *, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE consumption_coordinator SET heartbeat_at = ? WHERE slot = 1 AND owner = ?",
                (now_iso(), owner),
            )
            connection.commit()

    def coordinator_snapshot(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM consumption_coordinator WHERE slot = 1").fetchone()
        if row is None:
            return None
        return {
            "owner": row["owner"],
            "pid": row["pid"],
            "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"],
        }

    def request_cancel(self, job_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE consumption_jobs
                SET status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancel_requested' END,
                    cancel_requested_at = ?,
                    finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now_iso(), now_iso(), job_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM consumption_jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["status"] in {"cancel_requested", "cancelled"})

    def create_artifact(self, artifact: ConsumptionArtifact) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO consumption_artifacts (
                    id, job_id, app_name, format, storage_key, content_type,
                    filename, sha256, byte_size, row_count, classification,
                    created_at, expires_at
                ) SELECT ?, ?, app_name, requested_format, ?, ?, ?, ?, ?, ?, ?, ?, ?
                  FROM consumption_jobs WHERE id = ?
                """,
                (
                    artifact.id,
                    artifact.job_id,
                    artifact.storage_key,
                    artifact.content_type,
                    artifact.filename,
                    artifact.sha256,
                    artifact.byte_size,
                    artifact.row_count,
                    artifact.classification,
                    artifact.created_at,
                    artifact.expires_at,
                    artifact.job_id,
                ),
            )
            connection.commit()

    def mark_artifact_deleted(self, artifact_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE consumption_artifacts SET deleted_at = ? WHERE id = ?",
                (now_iso(), artifact_id),
            )
            connection.commit()

    def list_expired_artifacts(self, *, now: str) -> list[ConsumptionArtifact]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM consumption_artifacts
                WHERE deleted_at IS NULL AND expires_at <= ?
                ORDER BY expires_at
                """,
                (now,),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get_context(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT context_json FROM consumption_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["context_json"] or "{}")
        return payload if isinstance(payload, dict) else None

    def get_artifact_for_job(self, job_id: str) -> ConsumptionArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM consumption_artifacts
                WHERE job_id = ? AND deleted_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_artifact(row) if row is not None else None

    def record_audit(
        self,
        event_type: str,
        *,
        actor_principal_id: str,
        app_name: str,
        decision: str,
        job_id: str | None = None,
        artifact_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO consumption_audit_events (
                    event_type, actor_principal_id, app_name, job_id,
                    artifact_id, decision, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    actor_principal_id,
                    app_name,
                    job_id,
                    artifact_id,
                    decision,
                    json.dumps(details or {}, sort_keys=True),
                    now_iso(),
                ),
            )
            connection.commit()

    def _row_to_job(
        self,
        row: sqlite3.Row,
        *,
        decoded_parameters: dict[str, Any] | None = None,
    ) -> ConsumptionJob:
        return ConsumptionJob(
            id=row["id"],
            app_name=row["app_name"],
            output_id=row["output_id"],
            requested_by_principal_id=row["requested_by_principal_id"],
            run_as_principal_id=row["run_as_principal_id"],
            revision_number=row["revision_number"],
            output_contract_hash=row["output_contract_hash"],
            requested_format=row["requested_format"],
            status=row["status"],
            policy_version=row["policy_version"],
            parameters=decoded_parameters or {},
            parameters_hash=row["parameters_hash"],
            effective_limits=json.loads(row["effective_limits_json"] or "{}"),
            output=json.loads(row["output_json"] or "{}"),
            progress=json.loads(row["progress_json"] or "{}"),
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cancel_requested_at=row["cancel_requested_at"],
            attempt_count=row["attempt_count"] or 0,
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    def _row_to_artifact(self, row: sqlite3.Row) -> ConsumptionArtifact:
        return ConsumptionArtifact(
            id=row["id"],
            job_id=row["job_id"],
            storage_key=row["storage_key"],
            content_type=row["content_type"],
            filename=row["filename"],
            sha256=row["sha256"],
            byte_size=row["byte_size"],
            row_count=row["row_count"] or 0,
            classification=row["classification"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            deleted_at=row["deleted_at"],
        )

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        ensure_column(self.db_path, table_name, column_name, column_type, foreign_keys=True)

    def _connect(self):
        return open_connection(self.db_path, foreign_keys=True)


__all__ = ["ConsumptionStore", "now_iso"]
