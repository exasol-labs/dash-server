"""Centralized authorization decisions for hosted dashboard routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from dash_server.config import coerce_bool
from dash_server.timestamps import parse_iso8601
import json
from typing import Any

from dash_server.registry.models import AclEntry, HostedApp, SharePolicy
from dash_server.registry.sqlite_registry import SQLiteAppRegistry

from .capabilities import ROLE_CAPABILITIES
from .models import AuthContext, Principal


@dataclass(frozen=True)
class RouteTarget:
    """Classified request target used by authorization checks."""

    target_type: str
    capability: str | None
    app: HostedApp | None = None
    revision_number: int | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    """Result of one authorization check."""

    allowed: bool
    status_code: int
    reason: str
    capability: str | None
    principal_id: str
    target_type: str
    app_name: str | None = None
    effective_role: str | None = None
    matched_grant: dict[str, Any] | None = None
    matched_policy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status_code": self.status_code,
            "reason": self.reason,
            "capability": self.capability,
            "principal_id": self.principal_id,
            "target_type": self.target_type,
            "app_name": self.app_name,
            "effective_role": self.effective_role,
            "matched_grant": self.matched_grant,
            "matched_policy": self.matched_policy,
        }


class AuthorizationService:
    """Evaluate dashboard capabilities for the current request principal."""

    # The role→capability matrix now lives in ``auth.capabilities`` so the MCP
    # transport gate can derive from the same source. ``editor``/``owner`` carry
    # ``mcp.use_control_plane`` there, matching the roles the ``/mcp`` gate has
    # always admitted.
    _role_capabilities: dict[str, frozenset[str]] = ROLE_CAPABILITIES

    def __init__(self, registry: SQLiteAppRegistry, config: dict[str, Any]) -> None:
        self.registry = registry
        self.public_dashboards_enabled = coerce_bool(
            config.get("DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED")
        )

    def authorize_path(
        self,
        auth_context: AuthContext,
        *,
        path: str,
        mount_prefix: str | None = None,
    ) -> AuthorizationDecision:
        target = self.classify_path(path, mount_prefix=mount_prefix)
        return self.authorize(auth_context, target)

    def authorize_app(
        self,
        auth_context: AuthContext,
        app: HostedApp,
        capability: str,
        *,
        target: str = "live",
    ) -> AuthorizationDecision:
        target_type = "preview_app" if target == "preview" else "live_app"
        route_target = RouteTarget(
            target_type=target_type,
            capability=capability,
            app=app,
            revision_number=app.preview_revision_number if target == "preview" else app.current_revision_number,
        )
        return self.authorize(auth_context, route_target)

    def classify_path(self, path: str, *, mount_prefix: str | None = None) -> RouteTarget:
        normalized = path or "/"
        prefix = mount_prefix or normalized
        if prefix.startswith("/preview/"):
            return self._classify_preview_prefix(prefix)
        app = self.registry.get_app_by_route(prefix)
        if app is not None:
            return RouteTarget(target_type="live_app", capability="dashboard.view_live", app=app)
        if normalized == "/":
            return RouteTarget(target_type="catalog", capability="dashboard.discover")
        if normalized == "/mcp":
            return RouteTarget(target_type="mcp", capability="mcp.use_control_plane")
        if normalized.startswith("/auth/"):
            return RouteTarget(target_type="auth", capability=None)
        return RouteTarget(target_type="server_route", capability=None)

    def authorize(self, auth_context: AuthContext, target: RouteTarget) -> AuthorizationDecision:
        principal = auth_context.principal
        if auth_context.mode == "local":
            return self._allow(auth_context, target, reason="local_mode")
        if target.target_type in {"auth", "catalog", "server_route", "mcp"}:
            return self._allow(auth_context, target, reason=f"{target.target_type}_deferred")
        if target.target_type == "live_app" and target.app is not None:
            if target.capability == "dashboard.discover":
                return self._authorize_discover_app(auth_context, target)
            return self._authorize_live_app(auth_context, target)
        if target.target_type == "preview_app" and target.app is not None:
            if target.capability == "dashboard.discover":
                return self._authorize_discover_app(auth_context, target)
            return self._authorize_preview_app(auth_context, target)
        return AuthorizationDecision(
            allowed=False,
            status_code=404,
            reason="target_not_found",
            capability=target.capability,
            principal_id=principal.principal_id,
            target_type=target.target_type,
            app_name=target.app.name if target.app is not None else None,
        )

    def denial_wsgi_response(self, decision: AuthorizationDecision) -> tuple[str, list[tuple[str, str]], bytes]:
        status_text = "Unauthorized" if decision.status_code == 401 else "Forbidden"
        if decision.status_code == 404:
            status_text = "Not Found"
        body = {
            "error": {
                "category": "authorization_denied",
                "message": self._denial_message(decision),
                "details": decision.to_dict(),
            }
        }
        encoded = json.dumps(body, sort_keys=True).encode("utf-8")
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(encoded))),
        ]
        if decision.status_code == 401:
            headers.append(("WWW-Authenticate", 'Bearer realm="dash-server"'))
        return f"{decision.status_code} {status_text}", headers, encoded

    def _authorize_live_app(
        self,
        auth_context: AuthContext,
        target: RouteTarget,
    ) -> AuthorizationDecision:
        app = target.app
        if app is None:
            return self._deny(auth_context, target, status_code=404, reason="app_not_found")
        if not app.enabled:
            return self._deny(auth_context, target, status_code=404, reason="app_not_published")
        capability = target.capability
        if capability is None:
            return self._deny(auth_context, target, status_code=403, reason="missing_capability")
        self._persist_authenticated_user(auth_context)
        public_policy = self._public_live_policy(app)
        if public_policy is not None and capability in self._role_capabilities["viewer"]:
            return self._allow(
                auth_context,
                target,
                reason="public_dashboard",
                effective_role="viewer",
                matched_policy=public_policy.to_dict(),
            )
        if not auth_context.principal.is_authenticated:
            return self._deny(auth_context, target, status_code=401, reason="authentication_required")
        if self._principal_has_global_capability(auth_context, capability):
            return self._allow(auth_context, target, reason="global_role")
        matched_grant = self._matching_grant(auth_context.principal, app, capability)
        if matched_grant is not None:
            return self._allow(
                auth_context,
                target,
                reason="matched_grant",
                effective_role=matched_grant.role,
                matched_grant=matched_grant.to_dict(),
            )
        return self._deny(auth_context, target, status_code=403, reason="missing_capability")

    def _authorize_discover_app(
        self,
        auth_context: AuthContext,
        target: RouteTarget,
    ) -> AuthorizationDecision:
        app = target.app
        if app is None:
            return self._deny(auth_context, target, status_code=404, reason="app_not_found")
        self._persist_authenticated_user(auth_context)
        public_policy = self._public_catalog_policy(app)
        if public_policy is not None:
            return self._allow(
                auth_context,
                target,
                reason="public_catalog",
                effective_role="viewer",
                matched_policy=public_policy.to_dict(),
            )
        if not auth_context.principal.is_authenticated:
            return self._deny(auth_context, target, status_code=401, reason="authentication_required")
        if self._principal_has_global_capability(auth_context, "dashboard.discover"):
            return self._allow(auth_context, target, reason="global_role")
        matched_grant = self._matching_grant(auth_context.principal, app, "dashboard.discover")
        if matched_grant is not None:
            return self._allow(
                auth_context,
                target,
                reason="matched_grant",
                effective_role=matched_grant.role,
                matched_grant=matched_grant.to_dict(),
            )
        matched_policy = self._authenticated_catalog_policy(auth_context.principal, app)
        if matched_policy is not None:
            return self._allow(
                auth_context,
                target,
                reason="matched_share_policy",
                effective_role=matched_policy.default_link_role,
                matched_policy=matched_policy.to_dict(),
            )
        return self._deny(auth_context, target, status_code=403, reason="missing_discover_capability")

    def _authorize_preview_app(
        self,
        auth_context: AuthContext,
        target: RouteTarget,
    ) -> AuthorizationDecision:
        app = target.app
        if app is None:
            return self._deny(auth_context, target, status_code=404, reason="app_not_found")
        if app.preview_revision_number != target.revision_number:
            return self._deny(auth_context, target, status_code=404, reason="preview_not_active")
        if not auth_context.principal.is_authenticated:
            return self._deny(auth_context, target, status_code=401, reason="authentication_required")
        self._persist_authenticated_user(auth_context)
        if self._principal_has_global_capability(auth_context, "dashboard.view_preview"):
            return self._allow(auth_context, target, reason="global_role")
        matched_grant = self._matching_grant(auth_context.principal, app, "dashboard.view_preview")
        if matched_grant is not None:
            return self._allow(
                auth_context,
                target,
                reason="matched_grant",
                effective_role=matched_grant.role,
                matched_grant=matched_grant.to_dict(),
            )
        return self._deny(auth_context, target, status_code=403, reason="missing_preview_capability")

    def _classify_preview_prefix(self, prefix: str) -> RouteTarget:
        parts = prefix.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "preview":
            return RouteTarget(target_type="preview_app", capability="dashboard.view_preview")
        app_name = parts[1]
        try:
            revision_number = int(parts[2])
        except ValueError:
            revision_number = None
        app = self.registry.get_app(app_name)
        return RouteTarget(
            target_type="preview_app",
            capability="dashboard.view_preview",
            app=app,
            revision_number=revision_number,
        )

    def _public_live_policy(self, app: HostedApp) -> SharePolicy | None:
        if not self.public_dashboards_enabled or app.auth_policy == "required":
            return None
        policy = self.registry.get_share_policy(app.name)
        if app.visibility != "public" and policy.link_scope != "public":
            return None
        if policy.link_scope != "public":
            return None
        return policy

    def _public_catalog_policy(self, app: HostedApp) -> SharePolicy | None:
        policy = self._public_live_policy(app)
        if policy is None or not policy.public_catalog_visible:
            return None
        return policy

    def _authenticated_catalog_policy(self, principal: Principal, app: HostedApp) -> SharePolicy | None:
        policy = self.registry.get_share_policy(app.name)
        if policy.link_scope == "organization" and principal.tenant_id:
            return policy
        allowed_domain = policy.allowed_domain
        if (
            policy.link_scope == "domain"
            and isinstance(allowed_domain, str)
            and allowed_domain
            and principal.email
            and "@" in principal.email
            and principal.email.rsplit("@", 1)[1].lower() == allowed_domain.lower()
        ):
            return policy
        return None

    def _principal_has_global_capability(self, auth_context: AuthContext, capability: str) -> bool:
        principal = auth_context.principal
        for role in principal.roles:
            if role in {"viewer", "preview_viewer"}:
                continue
            if capability in self._role_capabilities.get(role, set()):
                return True
        return False

    def _matching_grant(
        self,
        principal: Principal,
        app: HostedApp,
        capability: str,
    ) -> AclEntry | None:
        for grant in self.registry.list_acl_entries(app.name):
            if grant.principal_type == "link" and capability == "dashboard.discover":
                continue
            if not self._grant_scope_matches(grant, capability):
                continue
            if capability not in self._role_capabilities.get(grant.role, set()):
                continue
            if self._grant_expired(grant):
                continue
            if self._grant_matches_principal(grant, principal):
                return grant
        return None

    def _grant_scope_matches(self, grant: AclEntry, capability: str) -> bool:
        scope = grant.scope
        if scope == "all":
            return True
        if capability == "dashboard.discover":
            return scope in {"live", "preview", "manage", "all"}
        if capability == "dashboard.view_live":
            return scope in {"live", "all"}
        if capability == "dashboard.view_preview":
            return scope in {"preview", "all"}
        return scope in {"manage", "all"}

    def _grant_expired(self, grant: AclEntry) -> bool:
        expires_at = parse_iso8601(grant.expires_at)
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)

    def _grant_matches_principal(self, grant: AclEntry, principal: Principal) -> bool:
        principal_type = grant.principal_type
        principal_id = grant.principal_id
        if principal_type == "user":
            return principal_id == principal.principal_id
        if principal_type == "group":
            return principal_id in principal.groups
        if principal_type == "organization":
            return principal.tenant_id is not None and principal_id in {principal.tenant_id, "*"}
        if principal_type == "domain":
            if principal.email is None or "@" not in principal.email:
                return False
            return principal.email.rsplit("@", 1)[1].lower() == principal_id.lower()
        if principal_type == "public":
            return True
        if principal_type == "link":
            return principal.principal_type == "link" and principal_id == principal.principal_id
        return False

    def _persist_authenticated_user(self, auth_context: AuthContext) -> None:
        if auth_context.principal.is_authenticated and auth_context.principal.principal_type == "user":
            self.registry.upsert_principal_user(auth_context.principal)

    def _allow(
        self,
        auth_context: AuthContext,
        target: RouteTarget,
        *,
        reason: str,
        effective_role: str | None = None,
        matched_grant: dict[str, Any] | None = None,
        matched_policy: dict[str, Any] | None = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=True,
            status_code=200,
            reason=reason,
            capability=target.capability,
            principal_id=auth_context.principal.principal_id,
            target_type=target.target_type,
            app_name=target.app.name if target.app is not None else None,
            effective_role=effective_role or self._first_matching_role(auth_context, target.capability),
            matched_grant=matched_grant,
            matched_policy=matched_policy,
        )

    def _deny(
        self,
        auth_context: AuthContext,
        target: RouteTarget,
        *,
        status_code: int,
        reason: str,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            status_code=status_code,
            reason=reason,
            capability=target.capability,
            principal_id=auth_context.principal.principal_id,
            target_type=target.target_type,
            app_name=target.app.name if target.app is not None else None,
        )

    def _first_matching_role(self, auth_context: AuthContext, capability: str | None) -> str | None:
        if capability is None:
            return None
        for role in auth_context.principal.roles:
            if capability in self._role_capabilities.get(role, set()):
                return role
        return None

    def _denial_message(self, decision: AuthorizationDecision) -> str:
        if decision.status_code == 401:
            return "Authentication is required to access this dashboard."
        if decision.status_code == 404:
            return "The requested dashboard target is not available."
        return "You do not have permission to access this dashboard target."
