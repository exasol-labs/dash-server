"""Shared authorization-aware consumption discovery and export service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from dash_server.auth import AuthContext, AuthorizationService
from dash_server.exceptions import DashServerError
from dash_server.exasol.service import ExasolDashboardService
from dash_server.registry.models import AppRevision
from dash_server.registry.sqlite_registry import SQLiteAppRegistry
from dash_server.timestamps import parse_iso8601, to_iso

from .artifacts import LocalArtifactStore
from .contract import consumption_contract_hash, normalize_consumption_contract
from .coordinator import LocalJobCoordinator
from .execution import ExasolDatasetExecutor
from .formats import get_dataset_format
from .jobs import ConsumptionJobRunner
from .models import (
    ConsumptionArtifact,
    ConsumptionExecutionContext,
    ConsumptionJob,
    ConsumptionPolicy,
)
from .security import (
    ConsumptionTokenSigner,
    ParameterCodec,
    parameter_hash,
    redact_parameters,
)
from .store import ConsumptionStore, now_iso


_COORDINATOR_STALE_SECONDS = 300


class ConsumptionService:
    """Expose one governed consumption domain to MCP and web adapters."""

    def __init__(
        self,
        registry: SQLiteAppRegistry,
        authorization_service: AuthorizationService,
        config: dict[str, Any],
        *,
        exasol_service: ExasolDashboardService | None = None,
        artifacts_root: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.authorization_service = authorization_service
        self.policy = ConsumptionPolicy.from_config(config)
        self.policy_version = self.policy.version
        root = Path(artifacts_root or config.get("ARTIFACTS_ROOT") or "instance/artifacts")
        self.store = ConsumptionStore(registry.db_path)
        self.store.initialize()
        self.artifact_store = LocalArtifactStore(root)
        self.parameter_codec = ParameterCodec.from_config(config, root)
        token_secret = self._token_secret(config, root)
        self.token_signer = ConsumptionTokenSigner(token_secret)
        self.executor = (
            ExasolDatasetExecutor(
                exasol_service,
                batch_size=self.policy.fetch_batch_size,
                max_runtime_seconds=self.policy.max_runtime_seconds,
            )
            if exasol_service is not None
            else None
        )
        self._preflighted: set[tuple[str, int, str, str]] = set()
        self._preflight_lock = Lock()
        self.instance_id = str(uuid4())
        self._job_runner = ConsumptionJobRunner(self)
        self.coordinator = LocalJobCoordinator(
            self._job_runner.run,
            max_workers=self.policy.max_concurrent_jobs,
        )

    def start(self) -> None:
        """Claim the single-process coordinator slot and reconcile prior state.

        Called once by `create_app()` after wiring; raises when another live
        process already coordinates against the same database.
        """
        self.store.claim_coordinator(
            owner=self.instance_id,
            pid=os.getpid(),
            stale_after_seconds=_COORDINATOR_STALE_SECONDS,
            is_pid_alive=_pid_alive,
        )
        self._job_runner.recover_incomplete_jobs()
        self.run_maintenance()

    def run_maintenance(self) -> dict[str, int]:
        """Expire artifacts, prune retained rows, and refresh the coordinator heartbeat."""
        expired = self.cleanup_expired_artifacts()
        now = datetime.now(timezone.utc)
        job_cutoff = to_iso(now - timedelta(seconds=self.policy.job_retention_seconds))
        audit_cutoff = (
            to_iso(now - timedelta(seconds=self.policy.audit_retention_seconds))
        )
        pruned_jobs, orphaned_artifacts = self.store.prune_expired_jobs(
            finished_before=job_cutoff,
            audit_before=audit_cutoff,
        )
        for artifact in orphaned_artifacts:
            self.artifact_store.delete(artifact.storage_key)
        self.store.heartbeat_coordinator(owner=self.instance_id)
        return {"expired_artifacts": expired, "pruned_jobs": pruned_jobs}

    def coordinator_status(self) -> dict[str, Any]:
        """Operational visibility for the deliberately single-process local coordinator."""
        return {
            "mode": "local-single-process",
            "multi_process_supported": False,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "max_workers": self.policy.max_concurrent_jobs,
            "exports_enabled": self.policy.exports_enabled,
            "claim": self.store.coordinator_snapshot(),
        }

    def list_outputs(self, name: str, auth_context: AuthContext) -> dict[str, Any]:
        app, revision, decision = self._authorized_revision(name, auth_context)
        consumption, contract_hash = self._revision_contract(revision)
        outputs = [
            self._output_payload(output, contract_hash=contract_hash)
            for output in (consumption or {}).get("outputs", [])
        ]
        return {
            "app": {"name": app.name, "title": app.title, "route": app.route},
            "revision": {
                "revision_number": revision.revision_number,
                "commit_sha": revision.commit_sha,
                "git_tag": revision.git_tag,
            },
            "contract_hash": contract_hash,
            "outputs": outputs,
            "output_count": len(outputs),
            "policy": {**self.policy.to_dict(), "version": self.policy_version},
            "authorization": decision.to_dict(),
        }

    def get_output(self, name: str, output_id: str, auth_context: AuthContext) -> dict[str, Any]:
        payload = self.list_outputs(name, auth_context)
        for output in payload["outputs"]:
            if output["id"] == output_id:
                return {**payload, "output": output}
        raise self._output_not_found(name, output_id)

    def execution_context(
        self,
        name: str,
        output_id: str,
        auth_context: AuthContext,
    ) -> ConsumptionExecutionContext:
        payload = self.get_output(name, output_id, auth_context)
        return ConsumptionExecutionContext(
            principal_id=auth_context.principal.principal_id,
            principal_type=auth_context.principal.principal_type,
            groups=auth_context.principal.groups,
            app_name=name,
            revision_number=int(payload["revision"]["revision_number"]),
            output_contract_hash=str(payload["contract_hash"]),
            policy_version=self.policy_version,
        )

    def create_export(
        self,
        name: str,
        output_id: str,
        requested_format: str,
        parameters: dict[str, Any] | None,
        auth_context: AuthContext,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require_authenticated(auth_context)
        self.run_maintenance()
        if not self.policy.enabled or not self.policy.exports_enabled:
            raise DashServerError(
                category="consumption_exports_disabled",
                summary="On-demand exports are disabled by server policy.",
                details={},
            )
        payload = self.get_output(name, output_id, auth_context)
        output = payload["output"]
        format_name = requested_format.strip().lower()
        availability = output["policy"]["format_availability"].get(format_name)
        if not availability or not availability["executable"]:
            raise DashServerError(
                category="consumption_format_unavailable",
                summary=f"Format {format_name!r} is not executable for this output.",
                details={"format": format_name, "reason": (availability or {}).get("reason")},
            )
        normalized_parameters = self._normalize_parameters(output["parameters"], parameters or {})
        params_hash = parameter_hash(normalized_parameters)
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        principal_id = auth_context.principal.principal_id
        if normalized_key is not None:
            existing = self.store.find_by_idempotency(principal_id, normalized_key)
            if existing is not None:
                if (
                    existing.app_name != name
                    or existing.output_id != output_id
                    or existing.requested_format != format_name
                    or existing.parameters_hash != params_hash
                ):
                    raise DashServerError(
                        category="consumption_idempotency_conflict",
                        summary="The idempotency key was already used for a different export request.",
                        details={"idempotency_key": normalized_key},
                    )
                return self._job_payload(existing)
        self._enforce_quotas(principal_id, name)
        revision_number = int(payload["revision"]["revision_number"])
        revision = self.registry.get_revision_by_number(name, revision_number)
        if revision is None:
            raise DashServerError(
                category="app_revision_not_found",
                summary="The live revision disappeared before the export could be pinned.",
                details={"app": name, "revision_number": revision_number},
            )
        self._preflight(revision, output)
        effective_limits = dict(output["policy"]["effective_limits"])
        job = ConsumptionJob(
            id=str(uuid4()),
            app_name=name,
            output_id=output_id,
            requested_by_principal_id=principal_id,
            run_as_principal_id=principal_id,
            revision_number=revision_number,
            output_contract_hash=str(payload["contract_hash"]),
            requested_format=format_name,
            status="queued",
            policy_version=self.policy_version,
            parameters=normalized_parameters,
            parameters_hash=params_hash,
            effective_limits=effective_limits,
            output=self._strip_runtime_policy(output),
            progress={"phase": "queued", "rows": 0, "bytes": 0},
            error=None,
            idempotency_key=normalized_key,
            created_at=now_iso(),
            started_at=None,
            finished_at=None,
            cancel_requested_at=None,
        )
        try:
            self.store.create_job(
                job=job,
                encoded_parameters=self.parameter_codec.encode(normalized_parameters),
                context=auth_context.to_dict(),
                redacted_parameters=redact_parameters(normalized_parameters),
            )
        except sqlite3.IntegrityError as exc:
            existing = (
                self.store.find_by_idempotency(principal_id, normalized_key) if normalized_key is not None else None
            )
            if existing is not None and (
                existing.app_name == name
                and existing.output_id == output_id
                and existing.requested_format == format_name
                and existing.parameters_hash == params_hash
            ):
                return self._job_payload(existing)
            raise DashServerError(
                category="consumption_idempotency_conflict",
                summary="A concurrent export request used the same idempotency key.",
                details={"idempotency_key": normalized_key},
            ) from exc
        self.store.record_audit(
            "export.created",
            actor_principal_id=principal_id,
            app_name=name,
            job_id=job.id,
            decision="allowed",
            details={
                "output_id": output_id,
                "format": format_name,
                "parameters_hash": params_hash,
                "revision_number": revision_number,
                "contract_hash": job.output_contract_hash,
                "policy_version": job.policy_version,
            },
        )
        self.coordinator.submit(job.id)
        return self._job_payload(job)

    def get_export(self, job_id: str, auth_context: AuthContext) -> dict[str, Any]:
        job = self._authorized_job(job_id, auth_context)
        return self._job_payload(job)

    def list_exports(self, auth_context: AuthContext, *, app_name: str | None = None) -> dict[str, Any]:
        self._require_authenticated(auth_context)
        self.run_maintenance()
        if app_name is not None:
            self._authorized_revision(app_name, auth_context)
        jobs = self.store.list_jobs(auth_context.principal.principal_id, app_name=app_name)
        return {"jobs": [self._job_payload(job) for job in jobs], "job_count": len(jobs)}

    def can_manage_consumption(self, name: str, auth_context: AuthContext) -> bool:
        """Silent capability probe for adapter navigation; no audit, no errors."""
        if not auth_context.principal.is_authenticated:
            return False
        app = self.registry.get_app(name)
        if app is None:
            return False
        return self.authorization_service.authorize_app(auth_context, app, "dashboard.manage_consumption").allowed

    def list_app_jobs(self, name: str, auth_context: AuthContext) -> dict[str, Any]:
        """Owner/admin app-wide job view; exposes redacted parameter summaries only."""
        self._require_authenticated(auth_context)
        self.run_maintenance()
        app = self.registry.get_app(name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {name} was not found.",
                details={"app": name},
            )
        decision = self.authorization_service.authorize_app(auth_context, app, "dashboard.manage_consumption")
        if not decision.allowed:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary=f"Principal cannot manage consumption workflows for app {name}.",
                details=decision.to_dict(),
                http_status=decision.status_code,
            )
        entries = []
        for job, redacted_parameters in self.store.list_app_jobs(name):
            artifact = self.store.get_artifact_for_job(job.id)
            entries.append(
                {
                    "job": job.to_dict(),
                    "parameters_redacted": redacted_parameters,
                    "artifact": artifact.to_dict() if artifact is not None else None,
                }
            )
        self.store.record_audit(
            "export.admin_listed",
            actor_principal_id=auth_context.principal.principal_id,
            app_name=name,
            decision="allowed",
            details={"job_count": len(entries)},
        )
        return {
            "app": name,
            "jobs": entries,
            "job_count": len(entries),
            "coordinator": self.coordinator_status(),
            "authorization": decision.to_dict(),
        }

    def cancel_export(self, job_id: str, auth_context: AuthContext) -> dict[str, Any]:
        job = self._authorized_job(job_id, auth_context)
        changed = self.store.request_cancel(job_id)
        current = self.store.get_job(job_id)
        assert current is not None
        self.store.record_audit(
            "export.cancel_requested",
            actor_principal_id=auth_context.principal.principal_id,
            app_name=job.app_name,
            job_id=job.id,
            decision="allowed",
            details={"changed": changed},
        )
        return self._job_payload(current)

    def create_download_link(self, job_id: str, auth_context: AuthContext) -> dict[str, Any]:
        job = self._authorized_job(job_id, auth_context)
        artifact = self._available_artifact(job)
        token = self.token_signer.issue(
            "download",
            {
                "job_id": job.id,
                "artifact_id": artifact.id,
                "principal_id": auth_context.principal.principal_id,
            },
        )
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.policy.download_token_ttl_seconds)
        artifact_expires_at = parse_iso8601(artifact.expires_at)
        if artifact_expires_at is not None:
            token_expires_at = min(token_expires_at, artifact_expires_at)
        return {
            "job_id": job.id,
            "artifact": artifact.to_dict(),
            "download_url": f"/downloads/{token}",
            "expires_at": to_iso(token_expires_at),
        }

    def resolve_download(self, token: str, auth_context: AuthContext) -> tuple[Path, ConsumptionArtifact]:
        self._require_authenticated(auth_context)
        self.run_maintenance()
        payload = self.token_signer.verify(
            token,
            "download",
            max_age=self.policy.download_token_ttl_seconds,
        )
        if payload.get("principal_id") != auth_context.principal.principal_id:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="The download link belongs to a different principal.",
                details={},
            )
        job = self._authorized_job(str(payload.get("job_id")), auth_context)
        artifact = self._available_artifact(job)
        if payload.get("artifact_id") != artifact.id:
            raise DashServerError(
                category="consumption_artifact_not_found",
                summary="The download link no longer identifies the current artifact.",
                details={},
            )
        path = self.artifact_store.resolve(artifact.storage_key)
        self.store.record_audit(
            "artifact.downloaded",
            actor_principal_id=auth_context.principal.principal_id,
            app_name=job.app_name,
            job_id=job.id,
            artifact_id=artifact.id,
            decision="allowed",
        )
        return path, artifact

    def cleanup_expired_artifacts(self) -> int:
        expired = self.store.list_expired_artifacts(now=now_iso())
        for artifact in expired:
            job = self.store.get_job(artifact.job_id)
            self.artifact_store.delete(artifact.storage_key)
            self.store.mark_artifact_deleted(artifact.id)
            self.store.transition_job(
                artifact.job_id,
                expected=("succeeded",),
                status="expired",
                progress={"phase": "expired"},
            )
            if job is not None:
                self.store.record_audit(
                    "artifact.expired",
                    actor_principal_id=job.run_as_principal_id,
                    app_name=job.app_name,
                    job_id=job.id,
                    artifact_id=artifact.id,
                    decision="allowed",
                )
        return len(expired)

    def issue_csrf_token(self, auth_context: AuthContext, action: str) -> str:
        self._require_authenticated(auth_context)
        return self.token_signer.issue(
            "csrf",
            {"principal_id": auth_context.principal.principal_id, "action": action},
        )

    def verify_csrf_token(self, token: str, auth_context: AuthContext, action: str) -> None:
        payload = self.token_signer.verify(token, "csrf", max_age=3600)
        if payload.get("principal_id") != auth_context.principal.principal_id or payload.get("action") != action:
            raise DashServerError(
                category="consumption_csrf_invalid",
                summary="The form security token is invalid for this action.",
                details={},
            )

    def peek_job_app(self, job_id: str) -> str | None:
        job = self.store.get_job(job_id)
        return job.app_name if job is not None else None

    def _run_job(self, job_id: str) -> None:
        """Coordinator runner; delegates to the extracted job runner."""
        self._job_runner.run(job_id)

    def _authorized_revision(self, name: str, auth_context: AuthContext):
        app = self.registry.get_app(name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {name} was not found.",
                details={"app": name},
            )
        decision = self.authorization_service.authorize_app(auth_context, app, "dashboard.export")
        if not decision.allowed:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary=f"Principal cannot access consumption outputs for app {name}.",
                details=decision.to_dict(),
                http_status=decision.status_code,
            )
        if not auth_context.principal.is_authenticated and not self.policy.public_exports_enabled:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="Public consumption output discovery is disabled by server policy.",
                details={**decision.to_dict(), "reason": "public_exports_disabled"},
            )
        revision = self.registry.get_current_revision(name)
        if revision is None:
            raise DashServerError(
                category="app_revision_not_found",
                summary=f"App {name} has no current revision.",
                details={"app": name},
            )
        return app, revision, decision

    def _revision_contract(self, revision: AppRevision) -> tuple[dict[str, Any] | None, str]:
        consumption = normalize_consumption_contract(
            revision.manifest.get("consumption"),
            data_sources=revision.manifest.get("data_sources"),
        )
        contract_hash = consumption_contract_hash(consumption)
        stored_hash = revision.manifest.get("consumption_contract_hash")
        if isinstance(stored_hash, str) and stored_hash and stored_hash != contract_hash:
            raise DashServerError(
                category="consumption_contract_hash_mismatch",
                summary=(f"Stored consumption contract hash does not match app {revision.app_name} revision content."),
                details={
                    "app": revision.app_name,
                    "revision_number": revision.revision_number,
                    "stored_hash": stored_hash,
                    "computed_hash": contract_hash,
                },
            )
        return consumption, contract_hash

    def _output_payload(self, output: dict[str, Any], *, contract_hash: str) -> dict[str, Any]:
        declared_formats = list(output["formats"])
        effective_formats = (
            [item for item in declared_formats if item in self.policy.allowed_formats] if self.policy.enabled else []
        )
        availability: dict[str, dict[str, Any]] = {}
        for format_name in declared_formats:
            if format_name not in effective_formats:
                availability[format_name] = {"executable": False, "reason": "blocked_by_policy"}
            elif not self.policy.exports_enabled:
                availability[format_name] = {"executable": False, "reason": "exports_disabled"}
            elif output.get("kind") != "dataset":
                availability[format_name] = {"executable": False, "reason": "renderer_not_available"}
            elif get_dataset_format(format_name) is None:
                availability[format_name] = {"executable": False, "reason": "format_not_implemented"}
            else:
                availability[format_name] = {"executable": True, "reason": "available"}
        executable = any(item["executable"] for item in availability.values())
        return {
            **output,
            "contract_hash": contract_hash,
            "policy": {
                "enabled": self.policy.enabled and bool(effective_formats),
                "effective_formats": effective_formats,
                "blocked_formats": sorted(set(declared_formats) - set(effective_formats)),
                "effective_limits": self._effective_limits(output),
                "format_availability": availability,
                "phase": "on_demand_exports" if self.policy.exports_enabled else "discovery_only",
                "executable": executable,
                "reason": "available" if executable else "no_executable_format",
            },
        }

    def _effective_limits(self, output: dict[str, Any]) -> dict[str, int]:
        declared = output.get("limits", {})
        return {
            "max_rows": min(int(declared.get("max_rows", self.policy.max_rows)), self.policy.max_rows),
            "max_bytes": min(int(declared.get("max_bytes", self.policy.max_bytes)), self.policy.max_bytes),
        }

    def _normalize_parameters(self, schema: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parameters, dict):
            raise DashServerError(
                category="consumption_parameter_validation_error",
                summary="Export parameters must be an object.",
                details={},
            )
        normalized = dict(parameters)
        properties = schema.get("properties", {})
        for key, definition in properties.items():
            if key not in normalized and "default" in definition:
                normalized[key] = definition["default"]
        errors = sorted(Draft202012Validator(schema).iter_errors(normalized), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            raise DashServerError(
                category="consumption_parameter_validation_error",
                summary="Export parameters do not match the registered schema.",
                details={"path": [str(item) for item in first.path], "reason": first.message},
            )
        secret_like = sorted(
            key
            for key in normalized
            if any(token in key.lower() for token in ("password", "secret", "token", "credential"))
        )
        if secret_like:
            raise DashServerError(
                category="consumption_parameter_validation_error",
                summary="Secret-like values are not accepted as export parameters.",
                details={"parameters": secret_like},
            )
        return normalized

    def _enforce_quotas(self, principal_id: str, app_name: str) -> None:
        active_for_principal = self.store.count_active_jobs(principal_id=principal_id)
        if active_for_principal >= self.policy.max_active_jobs_per_principal:
            raise DashServerError(
                category="consumption_quota_exceeded",
                summary="You already have the maximum number of active export jobs.",
                details={"scope": "principal", "limit": self.policy.max_active_jobs_per_principal},
            )
        active_for_app = self.store.count_active_jobs(app_name=app_name)
        if active_for_app >= self.policy.max_active_jobs_per_app:
            raise DashServerError(
                category="consumption_quota_exceeded",
                summary=f"App {app_name} already has the maximum number of active export jobs.",
                details={"scope": "app", "limit": self.policy.max_active_jobs_per_app},
            )

    def _preflight(self, revision: AppRevision, output: dict[str, Any]) -> None:
        if self.executor is None:
            raise DashServerError(
                category="consumption_executor_unavailable",
                summary="The Exasol export executor is not configured.",
                details={},
            )
        key = (
            revision.app_name,
            revision.revision_number,
            str(output["id"]),
            str(revision.manifest.get("consumption_contract_hash", "")),
        )
        with self._preflight_lock:
            if key in self._preflighted:
                return
            self.executor.preflight(revision, output)
            self._preflighted.add(key)

    def _authorized_job(self, job_id: str, auth_context: AuthContext) -> ConsumptionJob:
        self._require_authenticated(auth_context)
        job = self.store.get_job(job_id)
        if job is None:
            raise DashServerError(
                category="consumption_job_not_found",
                summary="The export job was not found.",
                details={"job_id": job_id},
            )
        if job.requested_by_principal_id != auth_context.principal.principal_id:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="The export job belongs to a different principal.",
                details={"job_id": job_id},
            )
        app = self.registry.get_app(job.app_name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {job.app_name} was not found.",
                details={"app": job.app_name},
            )
        decision = self.authorization_service.authorize_app(auth_context, app, "dashboard.export")
        if not decision.allowed:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="The principal no longer has export access to this app.",
                details=decision.to_dict(),
                http_status=decision.status_code,
            )
        return job

    def _job_payload(self, job: ConsumptionJob) -> dict[str, Any]:
        current = self.store.get_job(job.id) or job
        artifact = self.store.get_artifact_for_job(job.id)
        return {
            "job": current.to_dict(),
            "artifact": artifact.to_dict() if artifact is not None else None,
        }

    def _available_artifact(self, job: ConsumptionJob) -> ConsumptionArtifact:
        if job.status != "succeeded":
            raise DashServerError(
                category="consumption_artifact_not_ready",
                summary="The export artifact is not ready for download.",
                details={"job_id": job.id, "status": job.status},
            )
        artifact = self.store.get_artifact_for_job(job.id)
        expires_at = parse_iso8601(artifact.expires_at) if artifact is not None else None
        if artifact is None or expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise DashServerError(
                category="consumption_artifact_expired",
                summary="The export artifact has expired or is unavailable.",
                details={"job_id": job.id},
            )
        return artifact

    def _require_authenticated(self, auth_context: AuthContext) -> None:
        if not auth_context.principal.is_authenticated:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="Authentication is required for export workflows.",
                details={},
                http_status=401,
            )

    def _normalize_idempotency_key(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise DashServerError(
                category="consumption_idempotency_key_invalid",
                summary="Idempotency keys must contain 1 to 128 characters.",
                details={},
            )
        return normalized

    def _strip_runtime_policy(self, output: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in output.items() if key not in {"policy", "contract_hash"}}

    def _output_not_found(self, name: str, output_id: str) -> DashServerError:
        return DashServerError(
            category="consumption_output_not_found",
            summary=f"Consumption output {output_id} was not found for app {name}.",
            details={"app": name, "output_id": output_id},
        )

    def _token_secret(self, config: dict[str, Any], root: Path) -> str:
        configured = config.get("SECRET_KEY") or config.get("DASH_SERVER_CONSUMPTION_PARAMETER_KEY")
        if isinstance(configured, str) and configured:
            return configured
        path = root / "consumption" / ".token-key"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        return value


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = ["ConsumptionService"]
