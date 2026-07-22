"""Domain models for governed dashboard consumption outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

from dash_server.config import coerce_bool


# Default consumption formats, defined once and shared by the dataclass field
# default and the format validator.
_DEFAULT_CONSUMPTION_FORMATS = ("csv", "xlsx", "pdf", "png", "pptx")
_SUPPORTED_CONSUMPTION_FORMATS = frozenset(_DEFAULT_CONSUMPTION_FORMATS)


@dataclass(frozen=True)
class ConsumptionPolicy:
    """Server policy intersected with app-declared output capabilities.

    The field defaults ARE the source of truth for every consumption default.
    ``Config`` parses the same keys from the environment; the empty-environment
    equivalence of the two is asserted by ``test_config_single_source``. Because
    the defaults live here, ``from_config`` reads ``config.get(KEY)`` with no
    second argument (satisfying the "no duplicate default literal" rule) and
    falls back to the field default when a key is absent — so a partial dict
    (tests, embedders) is still valid.
    """

    enabled: bool = True
    exports_enabled: bool = False
    allowed_formats: tuple[str, ...] = _DEFAULT_CONSUMPTION_FORMATS
    max_rows: int = 100_000
    max_bytes: int = 50 * 1024 * 1024
    public_exports_enabled: bool = False
    max_runtime_seconds: int = 300
    artifact_ttl_seconds: int = 86400
    download_token_ttl_seconds: int = 300
    fetch_batch_size: int = 1000
    max_concurrent_jobs: int = 2
    max_attempts: int = 2
    job_retention_seconds: int = 7 * 86400
    audit_retention_seconds: int = 90 * 86400
    max_active_jobs_per_principal: int = 5
    max_active_jobs_per_app: int = 20

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ConsumptionPolicy:
        defaults = cls()

        def _int(key: str, default: int) -> int:
            value = config.get(key)
            return int(value) if value is not None else default

        raw_formats = config.get("DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS")
        if raw_formats is None:
            formats = defaults.allowed_formats
        elif isinstance(raw_formats, str):
            formats = tuple(item.strip().lower() for item in raw_formats.split(",") if item.strip())
        else:
            formats = tuple(str(item).strip().lower() for item in raw_formats if str(item).strip())
        unknown_formats = sorted(set(formats) - _SUPPORTED_CONSUMPTION_FORMATS)
        if unknown_formats:
            raise RuntimeError(
                "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS contains unsupported values: " + ", ".join(unknown_formats)
            )
        max_rows = _int("DASH_SERVER_CONSUMPTION_MAX_ROWS", defaults.max_rows)
        max_bytes = _int("DASH_SERVER_CONSUMPTION_MAX_BYTES", defaults.max_bytes)
        max_runtime_seconds = _int("DASH_SERVER_CONSUMPTION_MAX_RUNTIME_SECONDS", defaults.max_runtime_seconds)
        artifact_ttl_seconds = _int("DASH_SERVER_CONSUMPTION_ARTIFACT_TTL_SECONDS", defaults.artifact_ttl_seconds)
        download_token_ttl_seconds = _int(
            "DASH_SERVER_CONSUMPTION_DOWNLOAD_TOKEN_TTL_SECONDS", defaults.download_token_ttl_seconds
        )
        fetch_batch_size = _int("DASH_SERVER_CONSUMPTION_FETCH_BATCH_SIZE", defaults.fetch_batch_size)
        max_concurrent_jobs = _int("DASH_SERVER_CONSUMPTION_MAX_CONCURRENT_JOBS", defaults.max_concurrent_jobs)
        max_attempts = _int("DASH_SERVER_CONSUMPTION_MAX_ATTEMPTS", defaults.max_attempts)
        job_retention_seconds = _int("DASH_SERVER_CONSUMPTION_JOB_RETENTION_SECONDS", defaults.job_retention_seconds)
        audit_retention_seconds = _int(
            "DASH_SERVER_CONSUMPTION_AUDIT_RETENTION_SECONDS", defaults.audit_retention_seconds
        )
        max_active_jobs_per_principal = _int(
            "DASH_SERVER_CONSUMPTION_MAX_ACTIVE_JOBS_PER_PRINCIPAL", defaults.max_active_jobs_per_principal
        )
        max_active_jobs_per_app = _int(
            "DASH_SERVER_CONSUMPTION_MAX_ACTIVE_JOBS_PER_APP", defaults.max_active_jobs_per_app
        )
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
            enabled=coerce_bool(config.get("DASH_SERVER_CONSUMPTION_ENABLED"), default=defaults.enabled),
            exports_enabled=coerce_bool(
                config.get("DASH_SERVER_CONSUMPTION_EXPORTS_ENABLED"), default=defaults.exports_enabled
            ),
            allowed_formats=formats,
            max_rows=max_rows,
            max_bytes=max_bytes,
            public_exports_enabled=coerce_bool(
                config.get("DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED"),
                default=defaults.public_exports_enabled,
            ),
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
