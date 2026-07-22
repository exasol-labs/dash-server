"""SQLite-backed registry used for revisioned deployments."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from dash_server.db import ensure_column, open_connection

from .models import AppEvent, AppManifest, AppRevision, HostedApp


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    """Return ``cursor.lastrowid`` as a non-None int, asserting the post-INSERT invariant.

    sqlite3 types `lastrowid` as `int | None` because a cursor that hasn't executed
    an INSERT has none. Every call site here is right after an INSERT, so a None
    here is a programming error, not a runtime condition.
    """

    rowid = cursor.lastrowid
    assert rowid is not None, "cursor.lastrowid is None after INSERT"
    return rowid


class SQLiteAppRegistry:
    """Persist app metadata, immutable revisions, and deployment events."""

    _default_permissions = {
        "filesystem": {"mode": "workspace-write"},
        "network": {"mode": "inherit"},
        "env": {"mode": "inherit"},
    }

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS apps (
                    name TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    route TEXT NOT NULL,
                    status TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'private',
                    auth_policy TEXT NOT NULL DEFAULT 'inherited',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    permissions_json TEXT NOT NULL DEFAULT '{}',
                    current_revision_id INTEGER,
                    preview_revision_id INTEGER,
                    rollback_revision_id INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    bundle_json TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL DEFAULT 'validated',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL DEFAULT '',
                    dependency_lock_hash TEXT NOT NULL DEFAULT '',
                    commit_sha TEXT NOT NULL DEFAULT '',
                    git_tag TEXT NOT NULL DEFAULT '',
                    git_branch TEXT NOT NULL DEFAULT '',
                    release_manifest_path TEXT NOT NULL DEFAULT '',
                    rollout_metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(app_name, revision_number)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    revision_id INTEGER,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    principal_id TEXT NOT NULL UNIQUE,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT,
                    email_normalized TEXT,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    display_name TEXT NOT NULL,
                    user_type TEXT NOT NULL DEFAULT 'internal',
                    status TEXT NOT NULL DEFAULT 'active',
                    tenant_id TEXT,
                    last_login_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(issuer, subject)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    email TEXT,
                    source TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS group_memberships (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'local',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(group_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_acl_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    principal_type TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'live',
                    created_by_principal_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_share_policies (
                    app_name TEXT PRIMARY KEY,
                    link_scope TEXT NOT NULL DEFAULT 'restricted',
                    allowed_domain TEXT,
                    default_link_role TEXT NOT NULL DEFAULT 'viewer',
                    allow_preview_link INTEGER NOT NULL DEFAULT 0,
                    public_catalog_visible INTEGER NOT NULL DEFAULT 0,
                    external_sharing_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_by_principal_id TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS access_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_principal_id TEXT,
                    target_principal_id TEXT,
                    app_name TEXT,
                    principal_type TEXT,
                    principal_id TEXT,
                    decision TEXT,
                    reason TEXT,
                    request_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS share_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL DEFAULT 'live',
                    role TEXT NOT NULL DEFAULT 'viewer',
                    recipient_email TEXT,
                    recipient_note TEXT,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_by_principal_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    redeemed_at TEXT,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_invitations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    recipient_email TEXT NOT NULL,
                    email_normalized TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'live',
                    role TEXT NOT NULL DEFAULT 'viewer',
                    message TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivery_status TEXT NOT NULL DEFAULT 'pending_manual_delivery',
                    delivery_provider TEXT,
                    delivery_message_id TEXT,
                    delivery_error TEXT,
                    expires_at TEXT NOT NULL,
                    accepted_principal_id TEXT,
                    grant_id INTEGER,
                    created_by_principal_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TEXT,
                    accepted_at TEXT,
                    revoked_at TEXT
                )
                """
            )
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
            connection.commit()

        self._ensure_column("apps", "current_revision_id", "INTEGER")
        self._ensure_column("apps", "preview_revision_id", "INTEGER")
        self._ensure_column("apps", "rollback_revision_id", "INTEGER")
        self._ensure_column("apps", "visibility", "TEXT NOT NULL DEFAULT 'private'")
        self._ensure_column("apps", "auth_policy", "TEXT NOT NULL DEFAULT 'inherited'")
        self._ensure_column("apps", "enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("apps", "permissions_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("app_share_policies", "allowed_domain", "TEXT")

        self._ensure_column("app_revisions", "lifecycle_state", "TEXT NOT NULL DEFAULT 'validated'")
        self._ensure_column("app_revisions", "artifact_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("app_revisions", "source_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            "app_revisions", "dependency_lock_hash", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column("app_revisions", "commit_sha", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("app_revisions", "git_tag", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("app_revisions", "git_branch", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(
            "app_revisions", "release_manifest_path", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(
            "app_revisions", "rollout_metadata_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        # Phase 4a: persist the env id + python executable on the revision row so
        # _mount_revision_isolated doesn't have to recompute from requirements.txt at
        # mount time and Phase 4d env GC can join revisions against envs by id.
        self._ensure_column(
            "app_revisions", "dependency_environment_id", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column(
            "app_revisions", "env_python_executable", "TEXT NOT NULL DEFAULT ''"
        )
        self._ensure_column("app_invitations", "delivery_provider", "TEXT")
        self._ensure_column("app_invitations", "delivery_message_id", "TEXT")
        self._ensure_column("app_invitations", "delivery_error", "TEXT")
        self._ensure_column("app_invitations", "sent_at", "TEXT")


    def list_apps(self) -> list[HostedApp]:
        with self._connect() as connection:
            rows = connection.execute(self._app_select_sql("ORDER BY apps.name")).fetchall()
        return [self._row_to_app(row) for row in rows]

    def get_app(self, name: str) -> HostedApp | None:
        with self._connect() as connection:
            row = connection.execute(
                self._app_select_sql("WHERE apps.name = ?"),
                (name,),
            ).fetchone()
        return self._row_to_app(row) if row else None

    def get_app_by_route(self, route: str) -> HostedApp | None:
        with self._connect() as connection:
            row = connection.execute(
                self._app_select_sql("WHERE apps.route = ?"),
                (route,),
            ).fetchone()
        return self._row_to_app(row) if row else None

    def delete_app(self, name: str) -> bool:
        """Delete one app and all app-scoped projection rows atomically.

        Git remains the durable audit trail; this method only removes the local
        SQLite projection and hosted-sharing state for the deleted app.
        """

        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM apps WHERE name = ?",
                (name,),
            ).fetchone()
            if exists is None:
                return False
            for table in (
                "consumption_delivery_attempts",
                "consumption_artifacts",
                "consumption_jobs",
                "consumption_subscriptions",
                "consumption_alerts",
                "app_invitations",
                "share_links",
                "app_acl_entries",
                "app_share_policies",
                "app_events",
                "app_revisions",
            ):
                connection.execute(f"DELETE FROM {table} WHERE app_name = ?", (name,))
            connection.execute("DELETE FROM apps WHERE name = ?", (name,))
            connection.commit()
        return True

    def get_current_revision(self, app_name: str) -> AppRevision | None:
        return self.get_revision_by_pointer(app_name, "current_revision_id")

    def get_preview_revision(self, app_name: str) -> AppRevision | None:
        return self.get_revision_by_pointer(app_name, "preview_revision_id")

    def get_rollback_revision(self, app_name: str) -> AppRevision | None:
        return self.get_revision_by_pointer(app_name, "rollback_revision_id")

    def get_revision_by_number(self, app_name: str, revision_number: int) -> AppRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    app_name,
                    revision_number,
                    manifest_json,
                    bundle_json,
                    lifecycle_state,
                    artifact_path,
                    source_hash,
                    dependency_lock_hash,
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    rollout_metadata_json,
                    created_at
                FROM app_revisions
                WHERE app_name = ? AND revision_number = ?
                """,
                (app_name, revision_number),
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def list_revisions(self, app_name: str) -> list[AppRevision]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    app_name,
                    revision_number,
                    manifest_json,
                    bundle_json,
                    lifecycle_state,
                    artifact_path,
                    source_hash,
                    dependency_lock_hash,
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    rollout_metadata_json,
                    created_at
                FROM app_revisions
                WHERE app_name = ?
                ORDER BY revision_number
                """,
                (app_name,),
            ).fetchall()
        return [self._row_to_revision(row) for row in rows]

    def list_events(self, app_name: str) -> list[AppEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, app_name, event_type, revision_id, data_json, created_at
                FROM app_events
                WHERE app_name = ?
                ORDER BY id
                """,
                (app_name,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def upsert_principal_user(self, principal: Any, *, user_type: str = "internal") -> dict[str, Any] | None:
        principal_id = getattr(principal, "principal_id", None)
        issuer = getattr(principal, "issuer", None)
        subject = getattr(principal, "subject", None)
        if not getattr(principal, "is_authenticated", False) or not principal_id or not issuer or not subject:
            return None
        email = getattr(principal, "email", None)
        display_name = getattr(principal, "display_name", None) or str(principal_id)
        tenant_id = getattr(principal, "tenant_id", None)
        email_normalized = email.lower() if isinstance(email, str) else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    principal_id,
                    issuer,
                    subject,
                    email,
                    email_normalized,
                    email_verified,
                    display_name,
                    user_type,
                    status,
                    tenant_id,
                    last_login_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(principal_id) DO UPDATE SET
                    email = excluded.email,
                    email_normalized = excluded.email_normalized,
                    email_verified = excluded.email_verified,
                    display_name = excluded.display_name,
                    user_type = CASE
                        WHEN users.user_type = 'external' AND excluded.user_type = 'internal'
                        THEN users.user_type
                        ELSE excluded.user_type
                    END,
                    status = 'active',
                    tenant_id = excluded.tenant_id,
                    last_login_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    str(principal_id),
                    str(issuer),
                    str(subject),
                    email if isinstance(email, str) else None,
                    email_normalized,
                    1 if getattr(principal, "email_verified", False) else 0,
                    str(display_name),
                    user_type,
                    tenant_id if isinstance(tenant_id, str) else None,
                ),
            )
            connection.commit()
        return self.get_user_by_principal_id(str(principal_id))

    def get_user_by_principal_id(self, principal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    principal_id,
                    issuer,
                    subject,
                    email,
                    email_normalized,
                    email_verified,
                    display_name,
                    user_type,
                    status,
                    tenant_id,
                    last_login_at,
                    created_at,
                    updated_at
                FROM users
                WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def upsert_group(
        self,
        *,
        external_id: str,
        display_name: str | None = None,
        source: str = "local",
        email: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO groups (external_id, display_name, email, source, status, updated_at)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
                ON CONFLICT(external_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    email = excluded.email,
                    source = excluded.source,
                    status = 'active',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (external_id, display_name or external_id, email, source),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT id, external_id, display_name, email, source, status, created_at, updated_at
                FROM groups
                WHERE external_id = ?
                """,
                (external_id,),
            ).fetchone()
        group = self._row_to_group(row)
        assert group is not None
        return group

    def get_share_policy(self, app_name: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    app_name,
                    link_scope,
                    allowed_domain,
                    default_link_role,
                    allow_preview_link,
                    public_catalog_visible,
                    external_sharing_enabled,
                    updated_by_principal_id,
                    updated_at
                FROM app_share_policies
                WHERE app_name = ?
                """,
                (app_name,),
            ).fetchone()
        if row:
            return self._row_to_share_policy(row)
        return {
            "app_name": app_name,
            "link_scope": "restricted",
            "allowed_domain": None,
            "default_link_role": "viewer",
            "allow_preview_link": False,
            "public_catalog_visible": False,
            "external_sharing_enabled": False,
            "updated_by_principal_id": None,
            "updated_at": None,
        }

    def upsert_share_policy(
        self,
        app_name: str,
        *,
        link_scope: str,
        allowed_domain: str | None = None,
        default_link_role: str = "viewer",
        allow_preview_link: bool = False,
        public_catalog_visible: bool = False,
        external_sharing_enabled: bool = False,
        updated_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_share_policies (
                    app_name,
                    link_scope,
                    allowed_domain,
                    default_link_role,
                    allow_preview_link,
                    public_catalog_visible,
                    external_sharing_enabled,
                    updated_by_principal_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(app_name) DO UPDATE SET
                    link_scope = excluded.link_scope,
                    allowed_domain = excluded.allowed_domain,
                    default_link_role = excluded.default_link_role,
                    allow_preview_link = excluded.allow_preview_link,
                    public_catalog_visible = excluded.public_catalog_visible,
                    external_sharing_enabled = excluded.external_sharing_enabled,
                    updated_by_principal_id = excluded.updated_by_principal_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    app_name,
                    link_scope,
                    allowed_domain,
                    default_link_role,
                    1 if allow_preview_link else 0,
                    1 if public_catalog_visible else 0,
                    1 if external_sharing_enabled else 0,
                    updated_by_principal_id,
                ),
            )
            connection.commit()
        return self.get_share_policy(app_name)

    def grant_app_access(
        self,
        app_name: str,
        *,
        principal_type: str,
        principal_id: str,
        role: str,
        scope: str = "live",
        created_by_principal_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_acl_entries (
                    app_name,
                    principal_type,
                    principal_id,
                    role,
                    scope,
                    created_by_principal_id,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_name,
                    principal_type,
                    principal_id,
                    role,
                    scope,
                    created_by_principal_id,
                    expires_at,
                ),
            )
            grant_id = _last_row_id(cursor)
            connection.commit()
        grant = self.get_acl_entry(grant_id)
        assert grant is not None
        return grant

    def get_acl_entry(self, grant_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    app_name,
                    principal_type,
                    principal_id,
                    role,
                    scope,
                    created_by_principal_id,
                    created_at,
                    expires_at,
                    revoked_at
                FROM app_acl_entries
                WHERE id = ?
                """,
                (grant_id,),
            ).fetchone()
        return self._row_to_acl_entry(row) if row else None

    def list_acl_entries(self, app_name: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        where = "WHERE app_name = ?"
        if not include_revoked:
            where += " AND revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    app_name,
                    principal_type,
                    principal_id,
                    role,
                    scope,
                    created_by_principal_id,
                    created_at,
                    expires_at,
                    revoked_at
                FROM app_acl_entries
                {where}
                ORDER BY id
                """,
                (app_name,),
            ).fetchall()
        return [self._row_to_acl_entry(row) for row in rows]

    def revoke_app_access(
        self,
        app_name: str,
        *,
        grant_id: int | None = None,
        principal_type: str | None = None,
        principal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["app_name = ?", "revoked_at IS NULL"]
        values: list[Any] = [app_name]
        if grant_id is not None:
            clauses.append("id = ?")
            values.append(grant_id)
        if principal_type is not None:
            clauses.append("principal_type = ?")
            values.append(principal_type)
        if principal_id is not None:
            clauses.append("principal_id = ?")
            values.append(principal_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    app_name,
                    principal_type,
                    principal_id,
                    role,
                    scope,
                    created_by_principal_id,
                    created_at,
                    expires_at,
                    revoked_at
                FROM app_acl_entries
                WHERE {' AND '.join(clauses)}
                ORDER BY id
                """,
                values,
            ).fetchall()
            revoked_ids = [int(row["id"]) for row in rows]
            if revoked_ids:
                placeholders = ", ".join("?" for _ in revoked_ids)
                connection.execute(
                    f"""
                    UPDATE app_acl_entries
                    SET revoked_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    revoked_ids,
                )
                connection.commit()
        revoked_entries = []
        for revoked_id in revoked_ids:
            entry = self.get_acl_entry(revoked_id)
            if entry is not None:
                revoked_entries.append(entry)
        return revoked_entries

    def create_share_link(
        self,
        app_name: str,
        *,
        token_hash: str,
        scope: str,
        role: str,
        expires_at: str,
        max_uses: int = 1,
        recipient_email: str | None = None,
        recipient_note: str | None = None,
        created_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO share_links (
                    app_name,
                    token_hash,
                    scope,
                    role,
                    recipient_email,
                    recipient_note,
                    expires_at,
                    max_uses,
                    created_by_principal_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_name,
                    token_hash,
                    scope,
                    role,
                    recipient_email,
                    recipient_note,
                    expires_at,
                    max_uses,
                    created_by_principal_id,
                ),
            )
            link_id = _last_row_id(cursor)
            connection.commit()
        link = self.get_share_link(link_id)
        assert link is not None
        return link

    def get_share_link(self, link_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                self._share_link_select_sql("WHERE id = ?"),
                (link_id,),
            ).fetchone()
        return self._row_to_share_link(row) if row else None

    def get_share_link_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                self._share_link_select_sql("WHERE token_hash = ?"),
                (token_hash,),
            ).fetchone()
        return self._row_to_share_link(row) if row else None

    def list_share_links(self, app_name: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        where = "WHERE app_name = ?"
        if not include_revoked:
            where += " AND revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                self._share_link_select_sql(f"{where} ORDER BY id"),
                (app_name,),
            ).fetchall()
        return [self._row_to_share_link(row) for row in rows]

    def mark_share_link_redeemed(self, link_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE share_links
                SET use_count = use_count + 1,
                    redeemed_at = COALESCE(redeemed_at, CURRENT_TIMESTAMP)
                WHERE id = ?
                    AND revoked_at IS NULL
                    AND use_count < max_uses
                """,
                (link_id,),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get_share_link(link_id)

    def revoke_share_link(self, link_id: int) -> dict[str, Any] | None:
        link = self.get_share_link(link_id)
        if link is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE share_links
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revoked_at IS NULL
                """,
                (link_id,),
            )
            connection.commit()
        self.revoke_app_access(
            link["app_name"],
            principal_type="link",
            principal_id=f"share_link:{link_id}",
        )
        return self.get_share_link(link_id)

    def create_invitation(
        self,
        app_name: str,
        *,
        token_hash: str,
        recipient_email: str,
        email_normalized: str,
        scope: str,
        role: str,
        expires_at: str,
        message: str | None = None,
        delivery_status: str = "pending_manual_delivery",
        created_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_invitations (
                    app_name,
                    token_hash,
                    recipient_email,
                    email_normalized,
                    scope,
                    role,
                    message,
                    delivery_status,
                    expires_at,
                    created_by_principal_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_name,
                    token_hash,
                    recipient_email,
                    email_normalized,
                    scope,
                    role,
                    message,
                    delivery_status,
                    expires_at,
                    created_by_principal_id,
                ),
            )
            invitation_id = _last_row_id(cursor)
            connection.commit()
        invitation = self.get_invitation(invitation_id)
        assert invitation is not None
        return invitation

    def get_invitation(self, invitation_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                self._invitation_select_sql("WHERE id = ?"),
                (invitation_id,),
            ).fetchone()
        return self._row_to_invitation(row) if row else None

    def get_invitation_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                self._invitation_select_sql("WHERE token_hash = ?"),
                (token_hash,),
            ).fetchone()
        return self._row_to_invitation(row) if row else None

    def list_invitations(self, app_name: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        where = "WHERE app_name = ?"
        if not include_revoked:
            where += " AND revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                self._invitation_select_sql(f"{where} ORDER BY id"),
                (app_name,),
            ).fetchall()
        return [self._row_to_invitation(row) for row in rows]

    def mark_invitation_accepted(
        self,
        invitation_id: int,
        *,
        accepted_principal_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE app_invitations
                SET status = 'accepted',
                    accepted_principal_id = ?,
                    accepted_at = CURRENT_TIMESTAMP
                WHERE id = ?
                    AND status = 'pending'
                    AND revoked_at IS NULL
                    AND accepted_at IS NULL
                """,
                (accepted_principal_id, invitation_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get_invitation(invitation_id)

    def attach_invitation_grant(self, invitation_id: int, grant_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE app_invitations
                SET grant_id = ?
                WHERE id = ?
                """,
                (grant_id, invitation_id),
            )
            connection.commit()
        return self.get_invitation(invitation_id)

    def update_invitation_delivery(
        self,
        invitation_id: int,
        *,
        delivery_status: str,
        delivery_provider: str | None = None,
        delivery_message_id: str | None = None,
        delivery_error: str | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE app_invitations
                SET delivery_status = ?,
                    delivery_provider = ?,
                    delivery_message_id = ?,
                    delivery_error = ?,
                    sent_at = CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE sent_at END
                WHERE id = ?
                """,
                (
                    delivery_status,
                    delivery_provider,
                    delivery_message_id,
                    delivery_error,
                    delivery_status,
                    invitation_id,
                ),
            )
            connection.commit()
        return self.get_invitation(invitation_id)

    def revoke_invitation(self, invitation_id: int) -> dict[str, Any] | None:
        invitation = self.get_invitation(invitation_id)
        if invitation is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE app_invitations
                SET status = 'revoked',
                    revoked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revoked_at IS NULL
                """,
                (invitation_id,),
            )
            connection.commit()
        grant_id = invitation.get("grant_id")
        if isinstance(grant_id, int):
            self.revoke_app_access(invitation["app_name"], grant_id=grant_id)
        return self.get_invitation(invitation_id)

    def create_app(
        self,
        manifest: AppManifest,
        bundle: dict[str, Any],
        *,
        status: str,
        artifact_path: str,
        source_hash: str,
        dependency_lock_hash: str,
        commit_sha: str = "",
        git_tag: str = "",
        git_branch: str = "",
        release_manifest_path: str = "",
        lifecycle_state: str = "live",
    ) -> tuple[HostedApp, AppRevision]:
        revision_id = self._insert_revision(
            manifest.name,
            revision_number=1,
            manifest=manifest.to_dict(),
            bundle=bundle,
            lifecycle_state=lifecycle_state,
            artifact_path=artifact_path,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            commit_sha=commit_sha,
            git_tag=git_tag,
            git_branch=git_branch,
            release_manifest_path=release_manifest_path,
            rollout_metadata={"created_via": "app_create"},
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO apps (
                    name,
                    title,
                    route,
                    status,
                    visibility,
                    auth_policy,
                    enabled,
                    permissions_json,
                    current_revision_id,
                    preview_revision_id,
                    rollback_revision_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    manifest.name,
                    manifest.title,
                    manifest.route,
                    status,
                    "private",
                    "inherited",
                    1,
                    json.dumps(self._default_permissions),
                    revision_id,
                ),
            )
            connection.commit()

        app = self.get_app(manifest.name)
        revision = self.get_current_revision(manifest.name)
        assert app is not None
        assert revision is not None
        return app, revision

    def create_revision(
        self,
        app_name: str,
        manifest: AppManifest,
        bundle: dict[str, Any],
        *,
        artifact_path: str,
        source_hash: str,
        dependency_lock_hash: str,
        commit_sha: str = "",
        git_tag: str = "",
        git_branch: str = "",
        release_manifest_path: str = "",
    ) -> AppRevision:
        next_revision_number = self.next_revision_number(app_name)
        self._insert_revision(
            app_name,
            revision_number=next_revision_number,
            manifest=manifest.to_dict(),
            bundle=bundle,
            lifecycle_state="validated",
            artifact_path=artifact_path,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            commit_sha=commit_sha,
            git_tag=git_tag,
            git_branch=git_branch,
            release_manifest_path=release_manifest_path,
            rollout_metadata={"created_via": "app_build"},
        )
        revision = self.get_revision_by_number(app_name, next_revision_number)
        assert revision is not None
        return revision

    def upsert_revision_cache(
        self,
        app_name: str,
        *,
        revision_number: int,
        manifest: dict[str, Any],
        bundle: dict[str, Any],
        lifecycle_state: str,
        artifact_path: str,
        source_hash: str,
        dependency_lock_hash: str,
        commit_sha: str = "",
        git_tag: str = "",
        git_branch: str = "",
        release_manifest_path: str = "",
        rollout_metadata: dict[str, Any] | None = None,
    ) -> AppRevision:
        existing = self.get_revision_by_number(app_name, revision_number)
        payload = rollout_metadata or {}
        if existing is None:
            self._insert_revision(
                app_name,
                revision_number=revision_number,
                manifest=manifest,
                bundle=bundle,
                lifecycle_state=lifecycle_state,
                artifact_path=artifact_path,
                source_hash=source_hash,
                dependency_lock_hash=dependency_lock_hash,
                commit_sha=commit_sha,
                git_tag=git_tag,
                git_branch=git_branch,
                release_manifest_path=release_manifest_path,
                rollout_metadata=payload,
            )
        else:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE app_revisions
                    SET manifest_json = ?,
                        bundle_json = ?,
                        lifecycle_state = ?,
                        artifact_path = ?,
                        source_hash = ?,
                        dependency_lock_hash = ?,
                        commit_sha = ?,
                        git_tag = ?,
                        git_branch = ?,
                        release_manifest_path = ?,
                        rollout_metadata_json = ?
                    WHERE app_name = ? AND revision_number = ?
                    """,
                    (
                        json.dumps(manifest),
                        json.dumps(bundle),
                        lifecycle_state,
                        artifact_path,
                        source_hash,
                        dependency_lock_hash,
                        commit_sha,
                        git_tag,
                        git_branch,
                        release_manifest_path,
                        json.dumps(payload),
                        app_name,
                        revision_number,
                    ),
                )
                connection.commit()
        revision = self.get_revision_by_number(app_name, revision_number)
        assert revision is not None
        return revision

    def upsert_app_cache(
        self,
        *,
        name: str,
        title: str,
        route: str,
        status: str,
        visibility: str,
        auth_policy: str,
        enabled: bool,
        permissions: dict[str, Any],
        current_revision_id: int | None,
        preview_revision_id: int | None,
        rollback_revision_id: int | None,
    ) -> HostedApp:
        existing = self.get_app(name)
        with self._connect() as connection:
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO apps (
                        name,
                        title,
                        route,
                        status,
                        visibility,
                        auth_policy,
                        enabled,
                        permissions_json,
                        current_revision_id,
                        preview_revision_id,
                        rollback_revision_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        title,
                        route,
                        status,
                        visibility,
                        auth_policy,
                        1 if enabled else 0,
                        json.dumps(permissions),
                        current_revision_id,
                        preview_revision_id,
                        rollback_revision_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE apps
                    SET title = ?,
                        route = ?,
                        status = ?,
                        visibility = ?,
                        auth_policy = ?,
                        enabled = ?,
                        permissions_json = ?,
                        current_revision_id = ?,
                        preview_revision_id = ?,
                        rollback_revision_id = ?
                    WHERE name = ?
                    """,
                    (
                        title,
                        route,
                        status,
                        visibility,
                        auth_policy,
                        1 if enabled else 0,
                        json.dumps(permissions),
                        current_revision_id,
                        preview_revision_id,
                        rollback_revision_id,
                        name,
                    ),
                )
            connection.commit()
        app = self.get_app(name)
        assert app is not None
        return app

    def next_revision_number(self, app_name: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) AS max_revision FROM app_revisions WHERE app_name = ?",
                (app_name,),
            ).fetchone()
        return int(row["max_revision"]) + 1

    def set_status(self, app_name: str, status: str) -> HostedApp | None:
        with self._connect() as connection:
            connection.execute("UPDATE apps SET status = ? WHERE name = ?", (status, app_name))
            connection.commit()
        return self.get_app(app_name)

    def update_exposure(
        self,
        app_name: str,
        *,
        route: str | None = None,
        visibility: str | None = None,
        auth_policy: str | None = None,
        enabled: bool | None = None,
        permissions: dict[str, Any] | None = None,
    ) -> HostedApp | None:
        assignments: list[str] = []
        values: list[Any] = []
        if route is not None:
            assignments.append("route = ?")
            values.append(route)
        if visibility is not None:
            assignments.append("visibility = ?")
            values.append(visibility)
        if auth_policy is not None:
            assignments.append("auth_policy = ?")
            values.append(auth_policy)
        if enabled is not None:
            assignments.append("enabled = ?")
            values.append(1 if enabled else 0)
        if permissions is not None:
            assignments.append("permissions_json = ?")
            values.append(json.dumps(permissions))
        if not assignments:
            return self.get_app(app_name)
        values.append(app_name)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE apps SET {', '.join(assignments)} WHERE name = ?",
                values,
            )
            connection.commit()
        return self.get_app(app_name)

    def set_preview_revision(self, app_name: str, revision_id: int | None) -> HostedApp | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE apps SET preview_revision_id = ? WHERE name = ?",
                (revision_id, app_name),
            )
            connection.commit()
        return self.get_app(app_name)

    def promote_revision(self, app_name: str, revision_id: int) -> HostedApp | None:
        current_revision = self.get_current_revision(app_name)
        target_revision = self._get_revision_by_id(revision_id)
        assert target_revision is not None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE apps
                SET title = ?,
                    current_revision_id = ?,
                    rollback_revision_id = ?,
                    preview_revision_id = CASE WHEN preview_revision_id = ? THEN NULL ELSE preview_revision_id END
                WHERE name = ?
                """,
                (
                    target_revision.manifest["title"],
                    revision_id,
                    current_revision.id if current_revision is not None else None,
                    revision_id,
                    app_name,
                ),
            )
            connection.commit()
        return self.get_app(app_name)

    def update_revision_state(
        self,
        revision_id: int,
        lifecycle_state: str,
        rollout_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            if rollout_metadata is None:
                connection.execute(
                    "UPDATE app_revisions SET lifecycle_state = ? WHERE id = ?",
                    (lifecycle_state, revision_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE app_revisions
                    SET lifecycle_state = ?, rollout_metadata_json = ?
                    WHERE id = ?
                    """,
                    (lifecycle_state, json.dumps(rollout_metadata), revision_id),
                )
            connection.commit()

    def update_revision_git_metadata(
        self,
        revision_id: int,
        *,
        commit_sha: str,
        git_tag: str,
        git_branch: str,
        release_manifest_path: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE app_revisions
                SET commit_sha = ?,
                    git_tag = ?,
                    git_branch = ?,
                    release_manifest_path = ?
                WHERE id = ?
                """,
                (
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    revision_id,
                ),
            )
            connection.commit()

    def update_revision_environment(
        self,
        revision_id: int,
        *,
        dependency_environment_id: str,
        env_python_executable: str,
    ) -> None:
        """Phase 4a: record the env identity used to build this revision.

        Called from the build path once ``DependencyEnvironmentService.ensure_requirements``
        produced a record. After this, ``_mount_revision_isolated`` reads the env id directly
        from the revision row and Phase 4d GC joins revisions ↔ envs by id (no recomputation).
        """

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE app_revisions
                SET dependency_environment_id = ?,
                    env_python_executable = ?
                WHERE id = ?
                """,
                (
                    dependency_environment_id,
                    env_python_executable,
                    revision_id,
                ),
            )
            connection.commit()

    def list_referenced_environment_ids(self) -> set[str]:
        """Phase 4d GC helper: env ids referenced by any live/preview/rollback revision.

        An env is eligible for eviction when its id is *not* in this set. Empty strings —
        the default for pre-4a revisions that never got an env id recorded — are excluded
        from the result (they represent "unknown", not "referenced").
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT r.dependency_environment_id AS env_id
                FROM app_revisions r
                JOIN apps a
                  ON r.id = a.current_revision_id
                  OR r.id = a.preview_revision_id
                  OR r.id = a.rollback_revision_id
                WHERE r.dependency_environment_id IS NOT NULL
                  AND r.dependency_environment_id != ''
                """
            ).fetchall()
        return {row["env_id"] for row in rows if row["env_id"]}

    def append_event(
        self,
        app_name: str,
        event_type: str,
        *,
        revision_id: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> AppEvent:
        payload = data or {}
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_events (app_name, event_type, revision_id, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (app_name, event_type, revision_id, json.dumps(payload)),
            )
            event_id = _last_row_id(cursor)
            connection.commit()
            row = connection.execute(
                """
                SELECT id, app_name, event_type, revision_id, data_json, created_at
                FROM app_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_event(row)

    def ensure_event(
        self,
        app_name: str,
        event_type: str,
        *,
        revision_id: int | None = None,
        data: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> AppEvent:
        payload = data or {}
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, app_name, event_type, revision_id, data_json, created_at
                FROM app_events
                WHERE app_name = ? AND event_type = ?
                  AND ((revision_id = ?) OR (revision_id IS NULL AND ? IS NULL))
                  AND data_json = ? AND (? IS NULL OR created_at = ?)
                ORDER BY id
                LIMIT 1
                """,
                (
                    app_name,
                    event_type,
                    revision_id,
                    revision_id,
                    json.dumps(payload),
                    created_at,
                    created_at,
                ),
            ).fetchone()
            if existing is not None:
                return self._row_to_event(existing)

            if created_at is None:
                cursor = connection.execute(
                    """
                    INSERT INTO app_events (app_name, event_type, revision_id, data_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (app_name, event_type, revision_id, json.dumps(payload)),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO app_events (app_name, event_type, revision_id, data_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (app_name, event_type, revision_id, json.dumps(payload), created_at),
                )
            event_id = _last_row_id(cursor)
            connection.commit()
            row = connection.execute(
                """
                SELECT id, app_name, event_type, revision_id, data_json, created_at
                FROM app_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_event(row)

    def get_revision_by_pointer(self, app_name: str, pointer_column: str) -> AppRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    app_revisions.id,
                    app_revisions.app_name,
                    app_revisions.revision_number,
                    app_revisions.manifest_json,
                    app_revisions.bundle_json,
                    app_revisions.lifecycle_state,
                    app_revisions.artifact_path,
                    app_revisions.source_hash,
                    app_revisions.dependency_lock_hash,
                    app_revisions.commit_sha,
                    app_revisions.git_tag,
                    app_revisions.git_branch,
                    app_revisions.release_manifest_path,
                    app_revisions.rollout_metadata_json,
                    app_revisions.created_at
                FROM app_revisions
                JOIN apps ON apps.{pointer_column} = app_revisions.id
                WHERE apps.name = ?
                """,
                (app_name,),
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def _get_revision_by_id(self, revision_id: int) -> AppRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    app_name,
                    revision_number,
                    manifest_json,
                    bundle_json,
                    lifecycle_state,
                    artifact_path,
                    source_hash,
                    dependency_lock_hash,
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    rollout_metadata_json,
                    created_at
                FROM app_revisions
                WHERE id = ?
                """,
                (revision_id,),
            ).fetchone()
        return self._row_to_revision(row) if row else None

    def _insert_revision(
        self,
        app_name: str,
        *,
        revision_number: int,
        manifest: dict[str, Any],
        bundle: dict[str, Any],
        lifecycle_state: str,
        artifact_path: str,
        source_hash: str,
        dependency_lock_hash: str,
        commit_sha: str,
        git_tag: str,
        git_branch: str,
        release_manifest_path: str,
        rollout_metadata: dict[str, Any],
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO app_revisions (
                    app_name,
                    revision_number,
                    manifest_json,
                    bundle_json,
                    lifecycle_state,
                    artifact_path,
                    source_hash,
                    dependency_lock_hash,
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    rollout_metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_name,
                    revision_number,
                    json.dumps(manifest),
                    json.dumps(bundle),
                    lifecycle_state,
                    artifact_path,
                    source_hash,
                    dependency_lock_hash,
                    commit_sha,
                    git_tag,
                    git_branch,
                    release_manifest_path,
                    json.dumps(rollout_metadata),
                ),
            )
            revision_id = _last_row_id(cursor)
            connection.commit()
        return revision_id

    def _app_select_sql(self, suffix: str) -> str:
        return f"""
            SELECT
                apps.name,
                apps.title,
                apps.route,
                apps.status,
                apps.visibility,
                apps.auth_policy,
                apps.enabled,
                apps.permissions_json,
                apps.current_revision_id,
                current_revisions.revision_number AS current_revision_number,
                apps.preview_revision_id,
                preview_revisions.revision_number AS preview_revision_number,
                apps.rollback_revision_id,
                rollback_revisions.revision_number AS rollback_revision_number
            FROM apps
            LEFT JOIN app_revisions AS current_revisions
                ON current_revisions.id = apps.current_revision_id
            LEFT JOIN app_revisions AS preview_revisions
                ON preview_revisions.id = apps.preview_revision_id
            LEFT JOIN app_revisions AS rollback_revisions
                ON rollback_revisions.id = apps.rollback_revision_id
            {suffix}
        """

    def _share_link_select_sql(self, suffix: str) -> str:
        return f"""
            SELECT
                id,
                app_name,
                token_hash,
                scope,
                role,
                recipient_email,
                recipient_note,
                expires_at,
                max_uses,
                use_count,
                created_by_principal_id,
                created_at,
                redeemed_at,
                revoked_at
            FROM share_links
            {suffix}
        """

    def _invitation_select_sql(self, suffix: str) -> str:
        return f"""
            SELECT
                id,
                app_name,
                token_hash,
                recipient_email,
                email_normalized,
                scope,
                role,
                message,
                status,
                delivery_status,
                delivery_provider,
                delivery_message_id,
                delivery_error,
                expires_at,
                accepted_principal_id,
                grant_id,
                created_by_principal_id,
                created_at,
                sent_at,
                accepted_at,
                revoked_at
            FROM app_invitations
            {suffix}
        """

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        ensure_column(self.db_path, table_name, column_name, column_type)

    def _row_to_revision(self, row: sqlite3.Row) -> AppRevision:
        # The two new env-identity columns may not exist when reading old DBs that haven't
        # been migrated yet (PRAGMA table_info has the answer, but accessing by name on a
        # Row will KeyError instead of returning None). Probe via row.keys() so we keep
        # working transparently against pre-4a databases that someone forgot to migrate.
        keys = set(row.keys())
        return AppRevision(
            id=row["id"],
            app_name=row["app_name"],
            revision_number=row["revision_number"],
            manifest=json.loads(row["manifest_json"]),
            bundle=json.loads(row["bundle_json"]),
            lifecycle_state=row["lifecycle_state"],
            artifact_path=row["artifact_path"],
            source_hash=row["source_hash"],
            dependency_lock_hash=row["dependency_lock_hash"],
            commit_sha=row["commit_sha"],
            git_tag=row["git_tag"],
            git_branch=row["git_branch"],
            release_manifest_path=row["release_manifest_path"],
            rollout_metadata=json.loads(row["rollout_metadata_json"]),
            created_at=row["created_at"],
            dependency_environment_id=(
                row["dependency_environment_id"] if "dependency_environment_id" in keys else ""
            ),
            env_python_executable=(
                row["env_python_executable"] if "env_python_executable" in keys else ""
            ),
        )

    def _row_to_app(self, row: sqlite3.Row) -> HostedApp:
        permissions_payload = row["permissions_json"] or "{}"
        permissions = json.loads(permissions_payload)
        if permissions == {}:
            permissions = json.loads(json.dumps(self._default_permissions))
        return HostedApp(
            name=row["name"],
            title=row["title"],
            route=row["route"],
            status=row["status"],
            visibility=row["visibility"],
            auth_policy=row["auth_policy"],
            enabled=bool(row["enabled"]),
            permissions=permissions,
            current_revision_id=row["current_revision_id"],
            current_revision_number=row["current_revision_number"],
            preview_revision_id=row["preview_revision_id"],
            preview_revision_number=row["preview_revision_number"],
            rollback_revision_id=row["rollback_revision_id"],
            rollback_revision_number=row["rollback_revision_number"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> AppEvent:
        return AppEvent(
            id=row["id"],
            app_name=row["app_name"],
            event_type=row["event_type"],
            revision_id=row["revision_id"],
            data=json.loads(row["data_json"]),
            created_at=row["created_at"],
        )

    def _row_to_user(self, row: sqlite3.Row) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "principal_id": row["principal_id"],
            "issuer": row["issuer"],
            "subject": row["subject"],
            "email": row["email"],
            "email_normalized": row["email_normalized"],
            "email_verified": bool(row["email_verified"]),
            "display_name": row["display_name"],
            "user_type": row["user_type"],
            "status": row["status"],
            "tenant_id": row["tenant_id"],
            "last_login_at": row["last_login_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_group(self, row: sqlite3.Row) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "external_id": row["external_id"],
            "display_name": row["display_name"],
            "email": row["email"],
            "source": row["source"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_share_policy(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "app_name": row["app_name"],
            "link_scope": row["link_scope"],
            "allowed_domain": row["allowed_domain"],
            "default_link_role": row["default_link_role"],
            "allow_preview_link": bool(row["allow_preview_link"]),
            "public_catalog_visible": bool(row["public_catalog_visible"]),
            "external_sharing_enabled": bool(row["external_sharing_enabled"]),
            "updated_by_principal_id": row["updated_by_principal_id"],
            "updated_at": row["updated_at"],
        }

    def _row_to_acl_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "app_name": row["app_name"],
            "principal_type": row["principal_type"],
            "principal_id": row["principal_id"],
            "role": row["role"],
            "scope": row["scope"],
            "created_by_principal_id": row["created_by_principal_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
        }

    def _row_to_share_link(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "app_name": row["app_name"],
            "token_hash": row["token_hash"],
            "scope": row["scope"],
            "role": row["role"],
            "recipient_email": row["recipient_email"],
            "recipient_note": row["recipient_note"],
            "expires_at": row["expires_at"],
            "max_uses": row["max_uses"],
            "use_count": row["use_count"],
            "created_by_principal_id": row["created_by_principal_id"],
            "created_at": row["created_at"],
            "redeemed_at": row["redeemed_at"],
            "revoked_at": row["revoked_at"],
        }

    def _row_to_invitation(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "app_name": row["app_name"],
            "token_hash": row["token_hash"],
            "recipient_email": row["recipient_email"],
            "email_normalized": row["email_normalized"],
            "scope": row["scope"],
            "role": row["role"],
            "message": row["message"],
            "status": row["status"],
            "delivery_status": row["delivery_status"],
            "delivery_provider": row["delivery_provider"],
            "delivery_message_id": row["delivery_message_id"],
            "delivery_error": row["delivery_error"],
            "expires_at": row["expires_at"],
            "accepted_principal_id": row["accepted_principal_id"],
            "grant_id": row["grant_id"],
            "created_by_principal_id": row["created_by_principal_id"],
            "created_at": row["created_at"],
            "sent_at": row["sent_at"],
            "accepted_at": row["accepted_at"],
            "revoked_at": row["revoked_at"],
        }

    def _connect(self):
        return open_connection(self.db_path)
