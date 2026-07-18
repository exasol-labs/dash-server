"""Domain models for governed dashboard consumption outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class ConsumptionPolicy:
    """Server policy intersected with app-declared output capabilities."""

    enabled: bool
    allowed_formats: tuple[str, ...]
    max_rows: int
    max_bytes: int
    public_exports_enabled: bool

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ConsumptionPolicy:
        raw_formats = config.get(
            "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS",
            ("csv", "xlsx", "pdf", "png", "pptx"),
        )
        if isinstance(raw_formats, str):
            formats = tuple(item.strip().lower() for item in raw_formats.split(",") if item.strip())
        else:
            formats = tuple(
                str(item).strip().lower()
                for item in raw_formats
                if str(item).strip()
            )
        unknown_formats = sorted(set(formats) - {"csv", "xlsx", "pdf", "png", "pptx"})
        if unknown_formats:
            raise RuntimeError(
                "DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS contains unsupported values: "
                + ", ".join(unknown_formats)
            )
        max_rows = int(config.get("DASH_SERVER_CONSUMPTION_MAX_ROWS", 100_000))
        max_bytes = int(config.get("DASH_SERVER_CONSUMPTION_MAX_BYTES", 50 * 1024 * 1024))
        if max_rows <= 0 or max_bytes <= 0:
            raise RuntimeError(
                "DASH_SERVER_CONSUMPTION_MAX_ROWS and DASH_SERVER_CONSUMPTION_MAX_BYTES "
                "must be positive integers."
            )
        return cls(
            enabled=bool(config.get("DASH_SERVER_CONSUMPTION_ENABLED", True)),
            allowed_formats=formats,
            max_rows=max_rows,
            max_bytes=max_bytes,
            public_exports_enabled=bool(
                config.get("DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED", False)
            ),
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
    """Initial persisted job shape reserved for Phase 1 execution."""

    id: str
    app_name: str
    output_id: str
    requested_by_principal_id: str
    run_as_principal_id: str
    revision_number: int
    output_contract_hash: str
    requested_format: str
    status: str


@dataclass(frozen=True)
class ConsumptionArtifact:
    """Initial artifact metadata shape reserved for Phase 1 formatters."""

    id: str
    job_id: str
    storage_key: str
    content_type: str
    byte_size: int
    expires_at: str


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
