"""Domain models for governed dashboard consumption outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

from dash_server.config import coerce_bool


@dataclass(frozen=True)
class ConsumptionPolicy:
    """Server policy intersected with app-declared output capabilities."""

    enabled: bool
    exports_enabled: bool
    allowed_formats: tuple[str, ...]
    max_rows: int
    max_bytes: int
    public_exports_enabled: bool
    max_runtime_seconds: int
    artifact_ttl_seconds: int
    download_token_ttl_seconds: int
    fetch_batch_size: int
    max_concurrent_jobs: int
    max_attempts: int
    job_retention_seconds: int
    audit_retention_seconds: int
    max_active_jobs_per_principal: int
    max_active_jobs_per_app: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ConsumptionPolicy:
        raw_formats = config.get(
            "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS",
            ("csv", "xlsx", "pdf", "png", "pptx"),
        )
        if isinstance(raw_formats, str):
            formats = tuple(item.strip().lower() for item in raw_formats.split(",") if item.strip())
        else:
            formats = tuple(str(item).strip().lower() for item in raw_formats if str(item).strip())
        unknown_formats = sorted(set(formats) - {"csv", "xlsx", "pdf", "png", "pptx"})
        if unknown_formats:
            raise RuntimeError(
                "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS contains unsupported values: " + ", ".join(unknown_formats)
            )
        max_rows = int(config.get("DASH_SERVER_CONSUMPTION_MAX_ROWS", 100_000))
        max_bytes = int(config.get("DASH_SERVER_CONSUMPTION_MAX_BYTES", 50 * 1024 * 1024))
        max_runtime_seconds = int(config.get("DASH_SERVER_CONSUMPTION_MAX_RUNTIME_SECONDS", 300))
        artifact_ttl_seconds = int(config.get("DASH_SERVER_CONSUMPTION_ARTIFACT_TTL_SECONDS", 86400))
        download_token_ttl_seconds = int(config.get("DASH_SERVER_CONSUMPTION_DOWNLOAD_TOKEN_TTL_SECONDS", 300))
        fetch_batch_size = int(config.get("DASH_SERVER_CONSUMPTION_FETCH_BATCH_SIZE", 1000))
        max_concurrent_jobs = int(config.get("DASH_SERVER_CONSUMPTION_MAX_CONCURRENT_JOBS", 2))
        max_attempts = int(config.get("DASH_SERVER_CONSUMPTION_MAX_ATTEMPTS", 2))
        job_retention_seconds = int(config.get("DASH_SERVER_CONSUMPTION_JOB_RETENTION_SECONDS", 7 * 86400))
        audit_retention_seconds = int(config.get("DASH_SERVER_CONSUMPTION_AUDIT_RETENTION_SECONDS", 90 * 86400))
        max_active_jobs_per_principal = int(config.get("DASH_SERVER_CONSUMPTION_MAX_ACTIVE_JOBS_PER_PRINCIPAL", 5))
        max_active_jobs_per_app = int(config.get("DASH_SERVER_CONSUMPTION_MAX_ACTIVE_JOBS_PER_APP", 20))
        if (
            min(
                max_rows,
                max_bytes,
                max_runtime_seconds,
                artifact_ttl_seconds,
                download_token_ttl_seconds,
                fetch_batch_size,
                max_concurrent_jobs,
                max_attempts,
                job_retention_seconds,
                audit_retention_seconds,
                max_active_jobs_per_principal,
                max_active_jobs_per_app,
            )
            <= 0
        ):
            raise RuntimeError(
                "Consumption row, byte, runtime, retention, batch, concurrency, attempt, and quota "
                "limits must be positive integers."
            )
        if job_retention_seconds < artifact_ttl_seconds:
            raise RuntimeError(
                "DASH_SERVER_CONSUMPTION_JOB_RETENTION_SECONDS must be at least the artifact TTL so "
                "job pruning never outruns artifact expiry."
            )
        return cls(
            enabled=coerce_bool(config.get("DASH_SERVER_CONSUMPTION_ENABLED"), default=True),
            exports_enabled=coerce_bool(config.get("DASH_SERVER_CONSUMPTION_EXPORTS_ENABLED")),
            allowed_formats=formats,
            max_rows=max_rows,
            max_bytes=max_bytes,
            public_exports_enabled=coerce_bool(config.get("DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED")),
            max_runtime_seconds=max_runtime_seconds,
            artifact_ttl_seconds=artifact_ttl_seconds,
            download_token_ttl_seconds=download_token_ttl_seconds,
            fetch_batch_size=fetch_batch_size,
            max_concurrent_jobs=max_concurrent_jobs,
            max_attempts=max_attempts,
            job_retention_seconds=job_retention_seconds,
            audit_retention_seconds=audit_retention_seconds,
            max_active_jobs_per_principal=max_active_jobs_per_principal,
            max_active_jobs_per_app=max_active_jobs_per_app,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def version(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"consumption-v1:{digest[:16]}"


@dataclass(frozen=True)
class ConsumptionExecutionContext:
    """Identity and immutable revision context for a future consumption job."""

    principal_id: str
    principal_type: str
    app_name: str
    revision_number: int
    output_contract_hash: str
    policy_version: str
    groups: tuple[str, ...] = ()
    datasource_security_scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConsumptionJob:
    """Persisted on-demand export job."""

    id: str
    app_name: str
    output_id: str
    requested_by_principal_id: str
    run_as_principal_id: str
    revision_number: int
    output_contract_hash: str
    requested_format: str
    status: str
    policy_version: str
    parameters: dict[str, Any]
    parameters_hash: str
    effective_limits: dict[str, int]
    output: dict[str, Any]
    progress: dict[str, Any]
    error: dict[str, Any] | None
    idempotency_key: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested_at: str | None
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expires_at: str | None = None

    def to_dict(self, *, include_parameters: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if not include_parameters:
            payload.pop("parameters", None)
        return payload


@dataclass(frozen=True)
class ConsumptionArtifact:
    """Published export artifact metadata."""

    id: str
    job_id: str
    storage_key: str
    content_type: str
    filename: str
    sha256: str
    byte_size: int
    row_count: int
    classification: str
    created_at: str
    expires_at: str
    deleted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConsumptionSubscription:
    """Initial recurring-delivery shape reserved for Phase 3."""

    id: str
    app_name: str
    output_id: str
    owner_principal_id: str
    schedule: str
    timezone: str
    status: str


@dataclass(frozen=True)
class ConsumptionAlert:
    """Initial threshold-alert shape reserved for Phase 5."""

    id: str
    app_name: str
    output_id: str
    owner_principal_id: str
    condition: dict[str, Any]
    status: str
