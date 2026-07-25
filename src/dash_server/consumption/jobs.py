"""Worker-side runner for consumption export jobs.

Extracted from ``service.py`` so the export lifecycle reads as an explicit
``resolve -> authorize -> execute -> publish`` pipeline instead of a single
~180-line nested try/except. ``ConsumptionService`` keeps the
authorization-aware API surface and delegates worker execution and restart
recovery to :class:`ConsumptionJobRunner`.

Behavior is identical to the previous in-service implementation: the runner
reads its collaborators (store, registry, executor, policy, artifact store)
live off the owning service so mid-lifetime reassignment of ``executor`` and
``policy`` behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from dash_server.auth import AuthContext, Principal
from dash_server.exceptions import DashServerError
from dash_server.registry.models import AppRevision
from dash_server.timestamps import to_iso

from .formats import get_dataset_format
from .models import ConsumptionArtifact, ConsumptionJob
from .security import redact_parameters
from .store import now_iso

if TYPE_CHECKING:
    from .service import ConsumptionService


_LEASE_TTL_SECONDS = 120


@dataclass
class _Attempt:
    """Mutable state threaded through the pipeline steps of one attempt."""

    job_id: str
    job: ConsumptionJob
    audit_job: ConsumptionJob
    parameters: dict[str, Any] = field(default_factory=dict)
    revision: AppRevision | None = None
    temporary_path: Path | None = None


@dataclass
class _Execution:
    """The written-and-published output handed from execute to publish."""

    storage_key: str
    filename: str
    export_format: Any
    result: dict[str, Any]


class ConsumptionJobRunner:
    """Run queued export jobs to a terminal state on the coordinator's pool."""

    def __init__(self, service: ConsumptionService) -> None:
        self._service = service

    # -- collaborator accessors (read live off the owning service) --------

    @property
    def _store(self):
        return self._service.store

    @property
    def _registry(self):
        return self._service.registry

    @property
    def _artifact_store(self):
        return self._service.artifact_store

    @property
    def _parameter_codec(self):
        return self._service.parameter_codec

    # -- coordinator entry point ------------------------------------------

    def run(self, job_id: str) -> None:
        """Coordinator runner: retry transient failures in place."""
        while self._run_attempt(job_id) == "retry":
            pass

    def recover_incomplete_jobs(self) -> None:
        """Strand nothing across restarts: requeue or fail-close leftover work."""
        for job in self._store.list_incomplete_jobs():
            if job.status == "queued":
                self._service.coordinator.submit(job.id)
                continue
            if job.status == "cancel_requested":
                self._store.transition_job(
                    job.id,
                    expected=("cancel_requested",),
                    status="cancelled",
                    progress={"phase": "cancelled"},
                )
                self._store.record_audit(
                    "export.cancelled",
                    actor_principal_id=job.run_as_principal_id,
                    app_name=job.app_name,
                    job_id=job.id,
                    decision="allowed",
                    details={"reason": "reconciled_after_restart"},
                )
                continue
            # A running job at claim time was stranded by a previous process.
            if job.attempt_count < self._service.policy.max_attempts:
                requeued = self._store.transition_job(
                    job.id,
                    expected=("running",),
                    status="queued",
                    progress={"phase": "requeued", "rows": 0, "bytes": 0},
                )
                if requeued:
                    self._store.record_audit(
                        "export.requeued",
                        actor_principal_id=job.run_as_principal_id,
                        app_name=job.app_name,
                        job_id=job.id,
                        decision="allowed",
                        details={"reason": "stranded_by_restart", "attempt_count": job.attempt_count},
                    )
                    self._service.coordinator.submit(job.id)
                continue
            failed = self._store.transition_job(
                job.id,
                expected=("running",),
                status="failed",
                progress={"phase": "failed"},
                error={
                    "category": "consumption_job_stranded",
                    "summary": "The export was interrupted by a server restart and has no attempts left.",
                    "details": {"attempt_count": job.attempt_count},
                },
            )
            if failed:
                self._store.record_audit(
                    "export.failed",
                    actor_principal_id=job.run_as_principal_id,
                    app_name=job.app_name,
                    job_id=job.id,
                    decision="failed",
                    details={"category": "consumption_job_stranded"},
                )

    # -- single attempt as a resolve -> authorize -> execute -> publish pipeline

    def _run_attempt(self, job_id: str) -> str | None:
        if not self._claim(job_id):
            return None
        job = self._store.get_job(job_id)
        if job is None:
            return None
        attempt = _Attempt(job_id=job_id, job=job, audit_job=job)
        try:
            self._resolve(attempt)
            self._authorize(attempt)
            execution = self._execute(attempt)
            self._publish(attempt, execution)
        except DashServerError as exc:
            self._record_domain_failure(attempt, exc)
        except Exception as exc:
            # Unexpected failures are the retryable class; structured domain
            # errors above are deterministic and never retried.
            if self._requeue_for_retry(attempt, exc):
                return "retry"
            self._record_unexpected_failure(attempt, exc)
        finally:
            if attempt.temporary_path is not None:
                self._artifact_store.discard(attempt.temporary_path)
        return None

    def _claim(self, job_id: str) -> bool:
        return bool(
            self._store.transition_job(
                job_id,
                expected=("queued",),
                status="running",
                progress={"phase": "querying", "rows": 0, "bytes": 0},
                lease_owner=self._service.instance_id,
                lease_expires_at=self._lease_expiry(),
            )
        )

    def _resolve(self, attempt: _Attempt) -> None:
        """Load, decrypt, and revision-pin the queued job."""
        encoded = self._store.get_encoded_parameters(attempt.job_id)
        if encoded is None:
            raise RuntimeError("Stored export parameters are missing.")
        parameters = self._parameter_codec.decode(encoded)
        job = self._store.get_job(attempt.job_id, decoded_parameters=parameters)
        assert job is not None
        revision = self._registry.get_revision_by_number(job.app_name, job.revision_number)
        if revision is None:
            raise DashServerError(
                category="app_revision_not_found",
                summary="The pinned app revision is unavailable.",
                details={"app": job.app_name, "revision_number": job.revision_number},
            )
        self._verify_job_contract(job, revision)
        attempt.job = job
        attempt.parameters = parameters
        attempt.revision = revision

    def _authorize(self, attempt: _Attempt) -> None:
        """Re-check that the queued run-as principal still has export access."""
        auth_context = self._context_for_job(attempt.job_id)
        self._authorize_execution(attempt.job, auth_context)

    def _execute(self, attempt: _Attempt) -> _Execution:
        """Stream the dataset, write the formatted artifact, and stage storage."""
        job = attempt.job
        job_id = attempt.job_id
        if self._service.executor is None:
            raise DashServerError(
                category="consumption_executor_unavailable",
                summary="The Exasol export executor is not configured.",
                details={},
            )
        export_format = get_dataset_format(job.requested_format)
        if export_format is None:
            raise DashServerError(
                category="consumption_format_unavailable",
                summary=f"Format {job.requested_format!r} has no registered dataset writer.",
                details={"format": job.requested_format},
            )
        stream = self._service.executor.stream(
            attempt.revision,
            job.output,
            attempt.parameters,
            cancelled=lambda: self._store.is_cancel_requested(job_id),
        )
        attempt.temporary_path = self._artifact_store.temporary_path(job_id)
        current_limits = self._service._effective_limits(job.output)
        limits = {
            "max_rows": min(job.effective_limits["max_rows"], current_limits["max_rows"]),
            "max_bytes": min(job.effective_limits["max_bytes"], current_limits["max_bytes"]),
        }
        provenance = {
            "app": job.app_name,
            "output_id": job.output_id,
            "output_title": job.output.get("title"),
            "revision_number": job.revision_number,
            "contract_hash": job.output_contract_hash,
            "policy_version": job.policy_version,
            "generated_at": now_iso(),
            "format": job.requested_format,
            "classification": str(job.output.get("classification", "internal")),
            "parameters": redact_parameters(attempt.parameters),
            "effective_limits": limits,
        }

        def _report_progress(rows: int, bytes_written: int) -> None:
            self._store.update_progress(
                job_id,
                {"phase": "writing", "rows": rows, "bytes": bytes_written},
                lease_owner=self._service.instance_id,
                lease_expires_at=self._lease_expiry(),
            )

        result = export_format.writer(
            attempt.temporary_path,
            columns=stream.columns,
            batches=stream.batches,
            max_rows=limits["max_rows"],
            max_bytes=limits["max_bytes"],
            cancelled=lambda: self._store.is_cancel_requested(job_id),
            provenance=provenance,
            on_progress=_report_progress,
        )
        if self._store.is_cancel_requested(job_id):
            raise DashServerError(
                category="consumption_job_cancelled",
                summary="Export cancellation was requested.",
                details={},
            )
        filename = f"{job.app_name}-{job.output_id}.{export_format.extension}"
        storage_key = self._artifact_store.publish(job.id, attempt.temporary_path, filename)
        attempt.temporary_path = None
        return _Execution(
            storage_key=storage_key,
            filename=filename,
            export_format=export_format,
            result=result,
        )

    def _publish(self, attempt: _Attempt, execution: _Execution) -> None:
        """Record the artifact and flip the job to its succeeded terminal state."""
        job = attempt.job
        job_id = attempt.job_id
        audit_job = attempt.audit_job
        created_at = datetime.now(timezone.utc)
        artifact = ConsumptionArtifact(
            id=str(uuid4()),
            job_id=job.id,
            storage_key=execution.storage_key,
            content_type=execution.export_format.content_type,
            filename=execution.filename,
            sha256=execution.result["sha256"],
            byte_size=execution.result["byte_size"],
            row_count=execution.result["row_count"],
            classification=str(job.output.get("classification", "internal")),
            created_at=to_iso(created_at),
            expires_at=to_iso(created_at + timedelta(seconds=self._service.policy.artifact_ttl_seconds)),
        )
        self._store.create_artifact(artifact)
        completed = self._store.transition_job(
            job_id,
            expected=("running",),
            status="succeeded",
            progress={
                "phase": "complete",
                "rows": artifact.row_count,
                "bytes": artifact.byte_size,
            },
        )
        if not completed:
            self._artifact_store.delete(artifact.storage_key)
            self._store.mark_artifact_deleted(artifact.id)
            self._store.transition_job(
                job_id,
                expected=("cancel_requested",),
                status="cancelled",
                progress={"phase": "cancelled"},
            )
            self._store.record_audit(
                "export.cancelled",
                actor_principal_id=audit_job.run_as_principal_id,
                app_name=audit_job.app_name,
                job_id=audit_job.id,
                decision="allowed",
            )
            return
        self._store.record_audit(
            "export.succeeded",
            actor_principal_id=audit_job.run_as_principal_id,
            app_name=audit_job.app_name,
            job_id=audit_job.id,
            artifact_id=artifact.id,
            decision="allowed",
            details={"rows": artifact.row_count, "bytes": artifact.byte_size},
        )

    # -- failure handling -------------------------------------------------

    def _record_domain_failure(self, attempt: _Attempt, exc: DashServerError) -> None:
        audit_job = attempt.audit_job
        cancelled = exc.category == "consumption_job_cancelled"
        self._store.transition_job(
            attempt.job_id,
            expected=("running", "cancel_requested"),
            status="cancelled" if cancelled else "failed",
            progress={"phase": "cancelled" if cancelled else "failed"},
            error={"category": exc.category, "summary": exc.summary, "details": exc.details},
        )
        self._store.record_audit(
            "export.cancelled" if cancelled else "export.failed",
            actor_principal_id=audit_job.run_as_principal_id,
            app_name=audit_job.app_name,
            job_id=audit_job.id,
            decision="allowed" if cancelled else "failed",
            details={"category": exc.category},
        )

    def _requeue_for_retry(self, attempt: _Attempt, exc: Exception) -> bool:
        audit_job = attempt.audit_job
        if audit_job.attempt_count < self._service.policy.max_attempts and not self._store.is_cancel_requested(
            attempt.job_id
        ):
            retried = self._store.transition_job(
                attempt.job_id,
                expected=("running",),
                status="queued",
                progress={"phase": "requeued", "rows": 0, "bytes": 0},
            )
            if retried:
                self._store.record_audit(
                    "export.retried",
                    actor_principal_id=audit_job.run_as_principal_id,
                    app_name=audit_job.app_name,
                    job_id=audit_job.id,
                    decision="allowed",
                    details={"attempt_count": audit_job.attempt_count, "reason": type(exc).__name__},
                )
                return True
        return False

    def _record_unexpected_failure(self, attempt: _Attempt, exc: Exception) -> None:
        audit_job = attempt.audit_job
        self._store.transition_job(
            attempt.job_id,
            expected=("running", "cancel_requested"),
            status="failed",
            progress={"phase": "failed"},
            error={
                "category": "consumption_internal_error",
                "summary": "The export failed unexpectedly.",
                "details": {"reason": type(exc).__name__, "attempt_count": audit_job.attempt_count},
            },
        )
        self._store.record_audit(
            "export.failed",
            actor_principal_id=audit_job.run_as_principal_id,
            app_name=audit_job.app_name,
            job_id=audit_job.id,
            decision="failed",
            details={"category": "consumption_internal_error", "reason": type(exc).__name__},
        )

    # -- job-context helpers ----------------------------------------------

    def _lease_expiry(self) -> str:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=_LEASE_TTL_SECONDS)
        return to_iso(expiry)

    def _context_for_job(self, job_id: str) -> AuthContext:
        payload = self._store.get_context(job_id)
        if payload is None:
            raise RuntimeError("Consumption job context is missing.")
        return AuthContext(
            mode=str(payload.get("mode", "hosted")),
            auth_enabled=bool(payload.get("auth_enabled", True)),
            provider=str(payload.get("provider", "disabled")),
            principal=Principal.from_dict(payload.get("principal", {})),
        )

    def _verify_job_contract(self, job: ConsumptionJob, revision: AppRevision) -> None:
        consumption, contract_hash = self._service._revision_contract(revision)
        if contract_hash != job.output_contract_hash:
            raise DashServerError(
                category="consumption_contract_hash_mismatch",
                summary="The pinned job contract no longer matches its immutable revision.",
                details={"job_id": job.id},
            )
        declared = next(
            (item for item in (consumption or {}).get("outputs", []) if item["id"] == job.output_id),
            None,
        )
        if declared is None or declared != job.output:
            raise DashServerError(
                category="consumption_contract_hash_mismatch",
                summary="The pinned output declaration no longer matches the job snapshot.",
                details={"job_id": job.id, "output_id": job.output_id},
            )

    def _authorize_execution(self, job: ConsumptionJob, auth_context: AuthContext) -> None:
        authorized = self._service._authorized_job(job.id, auth_context)
        if authorized.run_as_principal_id != auth_context.principal.principal_id:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="The export execution principal no longer matches the queued job.",
                details={},
            )


__all__ = ["ConsumptionJobRunner"]
