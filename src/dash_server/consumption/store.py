"""SQLite persistence for consumption jobs, artifacts, and audit events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import ConsumptionArtifact, ConsumptionJob


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ConsumptionStore:
    """Small transactional store over the registry database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        with self._connect() as connection:
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
        if status in {"succeeded", "failed", "cancelled", "expired"}:
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
        return [
            ConsumptionArtifact(
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
            for row in rows
        ]

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
        if row is None:
            return None
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
        )

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        with self._connect() as connection:
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            if column_name not in {row["name"] for row in rows}:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


__all__ = ["ConsumptionStore", "now_iso"]
