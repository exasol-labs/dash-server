"""Domain models for hosted apps and revisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AppManifest:
    """Minimal manifest for a hosted Dash app."""

    name: str
    title: str
    route: str
    description: str
    template: str
    data_sources: dict[str, Any] | None = None
    consumption: dict[str, Any] | None = None
    consumption_contract_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppExposure:
    """Exposure metadata for a hosted app."""

    mount_path: str
    visibility: str
    auth_policy: str
    enabled: bool
    permissions: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HostedApp:
    """Registry representation for a hosted Dash app."""

    name: str
    title: str
    route: str
    status: str
    visibility: str
    auth_policy: str
    enabled: bool
    permissions: dict[str, Any]
    current_revision_id: int | None
    current_revision_number: int | None
    preview_revision_id: int | None
    preview_revision_number: int | None
    rollback_revision_id: int | None
    rollback_revision_number: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def exposure(self) -> AppExposure:
        return AppExposure(
            mount_path=self.route,
            visibility=self.visibility,
            auth_policy=self.auth_policy,
            enabled=self.enabled,
            permissions=self.permissions,
        )


@dataclass(frozen=True)
class AppRevision:
    """Stored revision for a hosted app."""

    id: int
    app_name: str
    revision_number: int
    manifest: dict[str, Any]
    bundle: dict[str, Any]
    lifecycle_state: str
    artifact_path: str
    source_hash: str
    dependency_lock_hash: str
    commit_sha: str
    git_tag: str
    git_branch: str
    release_manifest_path: str
    rollout_metadata: dict[str, Any]
    created_at: str
    # Phase 4a additions: the env identity and interpreter the revision was built against.
    # Empty strings for revisions built before the column existed; backfilled on first use.
    dependency_environment_id: str = ""
    env_python_executable: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppEvent:
    """Stored event log entry for a hosted app."""

    id: int
    app_name: str
    event_type: str
    revision_id: int | None
    data: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
