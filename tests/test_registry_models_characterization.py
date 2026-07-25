"""Characterization pins for the registry entity refactor (Wave 3 / P7).

These tests freeze the observable behavior of the sharing/authorization flow and
the share-link redeem flow *before* the six dict-returning registry entities are
promoted to frozen dataclasses. They must stay green before and after the
refactor, proving it is behavior-preserving.

The key invariant is the serialization boundary: entities that flow into
``AuthorizationDecision.matched_grant`` / ``matched_policy`` (and thus into
``to_dict()`` -> ``json.dumps``) or into MCP ``structured_content`` must remain
plain, JSON-serializable dicts with the exact same key set and shape.

The assertions use dict-style ``entity["key"]`` indexing on purpose: it exercises
the same read shape whether the registry returns a raw dict (pre-refactor) or a
frozen dataclass with a mapping-compatibility shim (post-refactor).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _helpers import hosted_test_config

from dash_server.app_factory import create_app
from dash_server.auth.authorization import AuthorizationService
from dash_server.auth.models import AuthContext, Principal
from dash_server.registry.models import AppManifest
from dash_server.registry.sqlite_registry import SQLiteAppRegistry


def _make_registry(tmp_path: Path) -> SQLiteAppRegistry:
    registry = SQLiteAppRegistry(str(tmp_path / "registry.sqlite3"))
    registry.initialize()
    return registry


def _seed_app(registry: SQLiteAppRegistry, name: str = "demo") -> None:
    manifest = AppManifest(
        name=name,
        title="Demo Dashboard",
        route=f"/apps/{name}",
        description="A demo app.",
        template="blank",
    )
    registry.create_app(
        manifest,
        bundle={"files": []},
        status="running",
        artifact_path="",
        source_hash="source-hash",
        dependency_lock_hash="lock-hash",
    )


def _hosted_context(principal: Principal) -> AuthContext:
    return AuthContext(
        mode="hosted",
        auth_enabled=True,
        provider="trusted_proxy",
        principal=principal,
    )


_DECISION_KEYS = {
    "allowed",
    "status_code",
    "reason",
    "capability",
    "principal_id",
    "target_type",
    "app_name",
    "effective_role",
    "matched_grant",
    "matched_policy",
}

_GRANT_KEYS = {
    "id",
    "app_name",
    "principal_type",
    "principal_id",
    "role",
    "scope",
    "created_by_principal_id",
    "created_at",
    "expires_at",
    "revoked_at",
}


def test_grant_then_authorize_allow_decision_shape(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    _seed_app(registry)
    app = registry.get_app("demo")
    assert app is not None

    grant = registry.grant_app_access(
        "demo",
        principal_type="user",
        principal_id="oidc:alice",
        role="viewer",
        scope="live",
        created_by_principal_id="oidc:admin",
    )
    # The registry now returns a typed AclEntry (attribute access, not dict keys).
    assert grant.role == "viewer"
    assert grant.scope == "live"
    assert grant.principal_type == "user"
    assert grant.principal_id == "oidc:alice"

    authz = AuthorizationService(registry, {"DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED": False})
    principal = Principal.authenticated_user(
        issuer="oidc",
        subject="alice",
        email="alice@example.test",
        roles=(),
    )
    decision = authz.authorize_app(_hosted_context(principal), app, "dashboard.view_live")

    assert decision.allowed is True
    assert decision.reason == "matched_grant"
    assert decision.effective_role == "viewer"

    serialized = decision.to_dict()
    assert set(serialized) == _DECISION_KEYS
    # Serialization boundary: matched_grant must be a plain, JSON-serializable dict.
    matched = serialized["matched_grant"]
    assert isinstance(matched, dict)
    assert set(matched) == _GRANT_KEYS
    assert matched["role"] == "viewer"
    assert matched["scope"] == "live"
    assert matched["principal_type"] == "user"
    assert matched["principal_id"] == "oidc:alice"
    # The whole decision must round-trip through JSON unchanged.
    assert json.loads(json.dumps(serialized)) == serialized


def test_authorize_deny_decision_shape(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    _seed_app(registry)
    app = registry.get_app("demo")
    assert app is not None

    authz = AuthorizationService(registry, {"DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED": False})
    principal = Principal.authenticated_user(
        issuer="oidc",
        subject="stranger",
        email="stranger@example.test",
        roles=(),
    )
    decision = authz.authorize_app(_hosted_context(principal), app, "dashboard.view_live")

    assert decision.allowed is False
    assert decision.status_code == 403
    assert decision.reason == "missing_capability"

    serialized = decision.to_dict()
    assert set(serialized) == _DECISION_KEYS
    assert serialized["matched_grant"] is None
    assert serialized["matched_policy"] is None
    assert json.loads(json.dumps(serialized)) == serialized


def test_public_policy_matched_policy_is_plain_dict(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    _seed_app(registry)
    registry.upsert_share_policy(
        "demo",
        link_scope="public",
        default_link_role="viewer",
        public_catalog_visible=True,
        updated_by_principal_id="oidc:admin",
    )
    app = registry.get_app("demo")
    assert app is not None

    authz = AuthorizationService(registry, {"DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED": True})
    decision = authz.authorize_app(
        _hosted_context(Principal.anonymous()), app, "dashboard.view_live"
    )

    assert decision.allowed is True
    assert decision.reason == "public_dashboard"
    serialized = decision.to_dict()
    matched_policy = serialized["matched_policy"]
    assert isinstance(matched_policy, dict)
    assert matched_policy["link_scope"] == "public"
    assert matched_policy["public_catalog_visible"] is True
    assert json.loads(json.dumps(serialized)) == serialized


@pytest.mark.slow
def test_share_link_redeem_flow(tmp_path: Path) -> None:
    app = create_app(hosted_test_config(tmp_path))
    registry = app.extensions["registry"]
    _seed_app(registry, "shared-demo")

    raw_token = "characterization-token"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    # Real share links store a naive UTC stamp (the MCP handler uses
    # datetime.utcnow().isoformat()); mirror that so the redeem path behaves as
    # in production rather than tripping the separate tz-comparison edge case.
    expires_at = (datetime.utcnow() + timedelta(hours=24)).replace(microsecond=0).isoformat()
    created_link = registry.create_share_link(
        "shared-demo",
        token_hash=token_hash,
        scope="live",
        role="viewer",
        expires_at=expires_at,
        max_uses=1,
        created_by_principal_id="oidc:admin",
    )
    assert created_link.app_name == "shared-demo"
    assert created_link.use_count == 0

    client = app.test_client()
    first = client.get(f"/share/links/{raw_token}")
    assert first.status_code == 302
    assert first.headers["Location"] == "/apps/shared-demo"

    # Redemption creates exactly one link-derived ACL grant.
    grants = registry.list_acl_entries("shared-demo")
    link_grants = [g for g in grants if g.principal_type == "link"]
    assert len(link_grants) == 1
    assert link_grants[0].role == "viewer"
    assert link_grants[0].scope == "live"

    # One-time link cannot be redeemed twice.
    second = client.get(f"/share/links/{raw_token}")
    assert second.status_code == 410
    assert second.get_json()["error"]["category"] == "share_link_used"
