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


@dataclass(frozen=True)
class ShareLink:
    """One-time (or few-use) manually shared dashboard access link."""

    id: int
    app_name: str
    token_hash: str
    scope: str
    role: str
    recipient_email: str | None
    recipient_note: str | None
    expires_at: str | None
    max_uses: int
    use_count: int
    created_by_principal_id: str | None
    created_at: str
    redeemed_at: str | None
    revoked_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Invitation:
    """Hashed-token email invitation for an external user."""

    id: int
    app_name: str
    token_hash: str
    recipient_email: str
    email_normalized: str
    scope: str
    role: str
    message: str | None
    status: str
    delivery_status: str
    delivery_provider: str | None
    delivery_message_id: str | None
    delivery_error: str | None
    expires_at: str | None
    accepted_principal_id: str | None
    grant_id: int | None
    created_by_principal_id: str | None
    created_at: str
    sent_at: str | None
    accepted_at: str | None
    revoked_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegistryUser:
    """Persisted control-plane user record."""

    id: int
    principal_id: str
    issuer: str
    subject: str
    email: str | None
    email_normalized: str | None
    email_verified: bool
    display_name: str
    user_type: str
    status: str
    tenant_id: str | None
    last_login_at: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Group:
    """Persisted group record used for group-scoped grants."""

    id: int
    external_id: str
    display_name: str | None
    email: str | None
    source: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AclEntry:
    """A single access-control grant on an app (a sharing grant)."""

    id: int
    app_name: str
    principal_type: str
    principal_id: str
    role: str
    scope: str
    created_by_principal_id: str | None
    created_at: str
    expires_at: str | None
    revoked_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SharePolicy:
    """App-level sharing policy governing link scope and catalog visibility."""

    app_name: str
    link_scope: str
    allowed_domain: str | None
    default_link_role: str
    allow_preview_link: bool
    public_catalog_visible: bool
    external_sharing_enabled: bool
    updated_by_principal_id: str | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
