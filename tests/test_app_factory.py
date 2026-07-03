from __future__ import annotations

from pathlib import Path
import hashlib
import json
import subprocess
from urllib.parse import parse_qs, urlparse

from flask import abort, request
import pytest

from dash_server.app_factory import create_app


def test_create_app_builds_flask_app(app):
    assert app.name == "dash_server.app_factory"
    assert "dispatcher" in app.extensions
    assert "registry" in app.extensions
    assert "diagnostics_service" in app.extensions
    assert "runtime_service" in app.extensions
    assert "git_repo_service" in app.extensions
    assert "git_worktree_service" in app.extensions
    assert "mcp_server" in app.extensions
    assert "auth_context" in app.extensions
    assert "authorization_service" in app.extensions


def test_create_app_defaults_to_local_mode_without_login(app, client):
    auth_context = app.extensions["auth_context"]

    assert app.config["DASH_SERVER_MODE"] == "local"
    assert app.config["DASH_SERVER_AUTH_ENABLED"] is False
    assert auth_context.mode == "local"
    assert auth_context.auth_enabled is False
    assert auth_context.principal.principal_id == "local-admin"
    assert auth_context.principal.is_authenticated is True
    assert "admin" in auth_context.principal.roles
    assert client.get("/").status_code == 200
    assert client.get("/apps/demo").status_code == 200


def _hosted_mode_test_config(tmp_path: Path) -> dict[str, object]:
    return {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
        "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets"),
        "AUTO_INSTALL_DEPENDENCIES": False,
        "DASH_SERVER_MODE": "hosted",
        "SECRET_KEY": "test-secret-key",
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "DASH_SERVER_PUBLIC_BASE_URL": "https://dash.example.test",
        "DASH_SERVER_AUTH_PROVIDER": "trusted_proxy",
        "DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED": True,
        "DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS": ("127.0.0.1/32",),
        "DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS": ("trusted_proxy:admin-123",),
        # Hosted mode normally requires per_app/isolated runtime; these tests predate that work
        # and exercise other hosted-mode behavior, so we explicitly opt into the development
        # override here.
        "DASH_SERVER_ALLOW_UNSAFE_INPROCESS": True,
    }


def _trusted_proxy_headers(subject: str = "user-123") -> dict[str, str]:
    return {
        "X-Forwarded-User": subject,
        "X-Forwarded-Email": f"{subject}@example.test",
        "X-Forwarded-Groups": "finance, exec",
    }


def _public_base_url() -> str:
    return "https://dash.example.test"


def _call_mcp(client, tool_name: str, arguments: dict[str, object], headers: dict[str, str] | None = None):
    return client.post(
        "/mcp",
        headers=_trusted_proxy_headers("admin-123") if headers is None else headers,
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )


def _start_demo_preview(client) -> None:
    build_response = _call_mcp(
        client,
        "app_build",
        {
            "name": "demo",
            "bundle": {
                "manifest": {
                    "name": "demo",
                    "title": "Demo Dashboard",
                    "route": "/apps/demo",
                    "description": "Demo preview revision.",
                    "template": "metric-cards",
                },
                "dashboard": {
                    "headline": "Demo Preview",
                    "summary": "Preview-only test revision.",
                    "metrics": [{"label": "Revenue", "value": "$1.2M"}],
                },
            },
        },
    )
    assert build_response.status_code == 200
    preview_response = _call_mcp(
        client,
        "app_start_preview",
        {"name": "demo", "revision_number": 2},
    )
    assert preview_response.status_code == 200


def test_hosted_mode_rejects_missing_secret_key(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["SECRET_KEY"] = None

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(config)


def test_hosted_mode_rejects_insecure_cookie_config(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["SESSION_COOKIE_SECURE"] = False

    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        create_app(config)


def test_hosted_mode_rejects_disabled_auth(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_AUTH_ENABLED"] = False

    with pytest.raises(RuntimeError, match="DASH_SERVER_AUTH_ENABLED"):
        create_app(config)


def test_hosted_mode_accepts_secure_baseline_config(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    auth_context = app.extensions["auth_context"]

    assert app.config["DASH_SERVER_MODE"] == "hosted"
    assert app.config["DASH_SERVER_AUTH_ENABLED"] is True
    assert auth_context.mode == "hosted"
    assert auth_context.auth_enabled is True
    assert auth_context.principal.principal_id == "anonymous"
    assert auth_context.principal.is_authenticated is False


def test_hosted_mode_rejects_oidc_without_provider_settings(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_AUTH_PROVIDER"] = "oidc"

    with pytest.raises(RuntimeError, match="DASH_SERVER_OIDC_ISSUER"):
        create_app(config)


def test_hosted_trusted_proxy_whoami_resolves_request_principal(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    anonymous = client.get("/auth/whoami")
    authenticated = client.get(
        "/auth/whoami",
        headers={
            "X-Forwarded-User": "user-123",
            "X-Forwarded-Email": "analyst@example.test",
            "X-Forwarded-Groups": "finance, exec",
        },
    )

    assert anonymous.status_code == 200
    assert anonymous.get_json()["auth"]["principal"]["principal_id"] == "anonymous"
    assert authenticated.status_code == 200
    principal = authenticated.get_json()["auth"]["principal"]
    assert principal["principal_id"] == "trusted_proxy:user-123"
    assert principal["email"] == "analyst@example.test"
    assert principal["groups"] == ["finance", "exec"]
    assert principal["is_authenticated"] is True


def test_hosted_dispatcher_denies_anonymous_dashboard_endpoints(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    for method, path in (
        ("get", "/apps/demo"),
        ("get", "/apps/demo/_dash-layout"),
        ("get", "/apps/demo/_dash-dependencies"),
        ("get", "/apps/demo/assets/missing.css"),
        ("post", "/apps/demo/_dash-update-component"),
    ):
        response = getattr(client, method)(path, json={})

        assert response.status_code == 401
        payload = response.get_json()
        assert payload["error"]["category"] == "authorization_denied"
        assert payload["error"]["details"]["target_type"] == "live_app"
        assert payload["error"]["details"]["capability"] == "dashboard.view_live"


def test_hosted_dispatcher_allows_granted_live_dashboard_without_flask_before_request(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    app.extensions["runtime_service"].registry.grant_app_access(
        "demo",
        principal_type="user",
        principal_id="trusted_proxy:user-123",
        role="viewer",
        scope="live",
        created_by_principal_id="test",
    )

    @app.before_request
    def _fail_if_flask_handles_mounted_dash_route():
        if request.path.startswith("/apps/demo"):
            abort(418)

    client = app.test_client()
    response = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())

    assert response.status_code == 200
    assert "Demo Dashboard" in json.dumps(response.get_json())


def test_hosted_authenticated_user_without_grant_cannot_view_restricted_dashboard(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    response = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"]["details"]["reason"] == "missing_capability"
    assert payload["error"]["details"]["matched_grant"] is None


def test_hosted_bootstrap_admin_can_view_restricted_and_preview_routes_without_grant(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS"] = ("trusted_proxy:user-123",)
    app = create_app(config)
    client = app.test_client()
    build_response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "demo",
                    "bundle": {
                        "manifest": {
                            "name": "demo",
                            "title": "Demo Dashboard",
                            "route": "/apps/demo",
                            "description": "Demo preview revision.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Demo Preview",
                            "summary": "Preview-only test revision.",
                            "metrics": [{"label": "Revenue", "value": "$1.2M"}],
                        },
                    },
                },
            },
        },
    )
    assert build_response.status_code == 200
    preview_response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "demo", "revision_number": 2},
            },
        },
    )
    assert preview_response.status_code == 200

    live = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())
    preview = client.get("/preview/demo/2/_dash-layout", headers=_trusted_proxy_headers())
    whoami = client.get("/auth/whoami", headers=_trusted_proxy_headers())

    assert live.status_code == 200
    assert preview.status_code == 200
    assert "admin" in whoami.get_json()["auth"]["principal"]["roles"]


def test_hosted_group_grant_allows_live_dashboard_and_revoke_blocks_immediately(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    grant_response = _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "group",
            "principal_id": "finance",
            "role": "viewer",
            "scope": "live",
        },
    )
    grant_payload = grant_response.get_json()["result"]["structuredContent"]

    allowed = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())
    revoke_response = _call_mcp(
        client,
        "app_share_revoke",
        {"name": "demo", "grant_id": grant_payload["grant"]["id"]},
    )
    denied = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())

    assert grant_response.status_code == 200
    assert grant_payload["grant"]["principal_type"] == "group"
    assert allowed.status_code == 200
    assert "Demo Dashboard" in json.dumps(allowed.get_json())
    assert revoke_response.status_code == 200
    assert revoke_response.get_json()["result"]["structuredContent"]["revoked_grants"][0]["revoked_at"]
    assert denied.status_code == 403


def test_app_share_grants_are_not_written_to_git_desired_state(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "user",
            "principal_id": "trusted_proxy:user-123",
            "role": "viewer",
            "scope": "live",
        },
    )

    repo_root = Path(app.extensions["git_repo_service"].repo_root)
    desired_state = "\n".join(path.read_text() for path in (repo_root / "desired-state").rglob("*.yaml"))

    assert "trusted_proxy:user-123" not in desired_state
    assert "app_acl_entries" not in desired_state


def test_app_share_explain_access_reports_matched_grant(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "user",
            "principal_id": "trusted_proxy:user-123",
            "role": "viewer",
            "scope": "live",
        },
    )

    explain = _call_mcp(
        client,
        "app_share_explain_access",
        {"name": "demo", "principal_id": "trusted_proxy:user-123", "target": "live"},
    )

    payload = explain.get_json()["result"]["structuredContent"]
    assert explain.status_code == 200
    assert payload["decision"]["allowed"] is True
    assert payload["decision"]["reason"] == "matched_grant"
    assert payload["decision"]["matched_grant"]["principal_id"] == "trusted_proxy:user-123"


def test_hosted_mcp_coarse_gate_denies_anonymous_viewer_and_link_principals(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    anonymous = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    viewer = client.post(
        "/mcp",
        headers=_trusted_proxy_headers("viewer-123"),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    admin = client.post(
        "/mcp",
        headers=_trusted_proxy_headers("admin-123"),
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )

    link_response = _call_mcp(
        client,
        "app_share_create_one_time_link",
        {"name": "demo", "scope": "live", "ttl_hours": 1},
    )
    link = link_response.get_json()["result"]["structuredContent"]["one_time_link"]
    assert client.get(link["url"]).status_code == 302
    link_mcp = client.post(
        "/mcp",
        base_url=_public_base_url(),
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
    )

    assert anonymous.status_code == 401
    assert anonymous.get_json()["error"]["data"]["category"] == "mcp_authorization_denied"
    assert viewer.status_code == 403
    assert viewer.get_json()["error"]["data"]["principal_id"] == "trusted_proxy:viewer-123"
    assert admin.status_code == 200
    assert "tools" in admin.get_json()["result"]
    assert link_mcp.status_code == 403
    assert link_mcp.get_json()["error"]["data"]["principal_type"] == "link"


def test_hosted_app_owner_grant_allows_scoped_sharing_mcp_tools(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    app.extensions["runtime_service"].registry.grant_app_access(
        "demo",
        principal_type="user",
        principal_id="trusted_proxy:user-123",
        role="owner",
        scope="all",
        created_by_principal_id="test",
    )
    client = app.test_client()

    share_get = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_share_get", "arguments": {"name": "demo"}},
        },
    )
    grant_peer = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_share_grant",
                "arguments": {
                    "name": "demo",
                    "principal_type": "user",
                    "principal_id": "trusted_proxy:peer-123",
                    "role": "viewer",
                    "scope": "live",
                },
            },
        },
    )
    tools_list = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    peer_live = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers("peer-123"))

    assert share_get.status_code == 200
    assert share_get.get_json()["result"]["structuredContent"]["app"]["name"] == "demo"
    assert grant_peer.status_code == 200
    assert grant_peer.get_json()["result"]["structuredContent"]["grant"]["principal_id"] == "trusted_proxy:peer-123"
    assert peer_live.status_code == 200
    assert tools_list.status_code == 403
    assert tools_list.get_json()["error"]["data"]["category"] == "mcp_authorization_denied"


def test_hosted_app_viewer_grant_cannot_use_scoped_sharing_mcp_tools(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    app.extensions["runtime_service"].registry.grant_app_access(
        "demo",
        principal_type="user",
        principal_id="trusted_proxy:user-123",
        role="viewer",
        scope="live",
        created_by_principal_id="test",
    )
    client = app.test_client()

    response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_share_get", "arguments": {"name": "demo"}},
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["data"]["category"] == "mcp_authorization_denied"


def test_hosted_public_dashboard_does_not_allow_scoped_sharing_mcp_tools(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED"] = True
    app = create_app(config)
    app.extensions["runtime_service"].update_visibility("demo", "public")
    client = app.test_client()
    _call_mcp(client, "app_share_set_link_scope", {"name": "demo", "link_scope": "public"})

    live = client.get("/apps/demo/_dash-layout")
    response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "app_share_get", "arguments": {"name": "demo"}},
        },
    )

    assert live.status_code == 200
    assert response.status_code == 403
    assert response.get_json()["error"]["data"]["category"] == "mcp_authorization_denied"


def test_one_time_link_can_be_redeemed_once_and_grants_url_only_session(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    create_response = _call_mcp(
        client,
        "app_share_create_one_time_link",
        {
            "name": "demo",
            "role": "viewer",
            "scope": "live",
            "ttl_hours": 1,
            "recipient_email": "external@example.test",
            "recipient_note": "manual handoff",
        },
    )
    payload = create_response.get_json()["result"]["structuredContent"]
    one_time_link = payload["one_time_link"]

    share_get = _call_mcp(client, "app_share_get", {"name": "demo"})
    share_payload = share_get.get_json()["result"]["structuredContent"]
    redeem_response = client.get(one_time_link["url"])
    layout_response = client.get("/apps/demo/_dash-layout", base_url=_public_base_url())
    homepage = client.get("/", base_url=_public_base_url())
    second_client = app.test_client()
    second_redeem = second_client.get(one_time_link["url"])

    assert create_response.status_code == 200
    assert one_time_link["display_once"] is True
    assert one_time_link["raw_token"]
    assert "token_hash" not in one_time_link
    assert one_time_link["recipient_email"] == "external@example.test"
    assert share_get.status_code == 200
    assert share_payload["share_policy"]["link_scope"] == "anyone_with_link"
    assert share_payload["share_policy"]["public_catalog_visible"] is False
    assert share_payload["one_time_links"][0]["id"] == one_time_link["id"]
    assert "raw_token" not in share_payload["one_time_links"][0]
    assert "token_hash" not in share_payload["one_time_links"][0]
    assert redeem_response.status_code == 302
    assert redeem_response.headers["Location"] == "/apps/demo"
    assert layout_response.status_code == 200
    assert "Demo Dashboard" in json.dumps(layout_response.get_json())
    assert b"Demo Dashboard" not in homepage.data
    assert second_redeem.status_code == 410
    assert second_redeem.get_json()["error"]["category"] == "share_link_used"


def test_one_time_preview_link_redirects_to_active_preview(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _start_demo_preview(client)
    create_response = _call_mcp(
        client,
        "app_share_create_one_time_link",
        {"name": "demo", "role": "preview_viewer", "scope": "preview", "ttl_hours": 1},
    )
    one_time_link = create_response.get_json()["result"]["structuredContent"]["one_time_link"]

    redeem_response = client.get(one_time_link["url"])
    preview_response = client.get("/preview/demo/2/_dash-layout", base_url=_public_base_url())
    live_response = client.get("/apps/demo/_dash-layout", base_url=_public_base_url())

    assert redeem_response.status_code == 302
    assert redeem_response.headers["Location"] == "/preview/demo/2"
    assert preview_response.status_code == 200
    assert live_response.status_code == 403


def test_one_time_link_revoke_blocks_redeemed_session(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    create_response = _call_mcp(
        client,
        "app_share_create_one_time_link",
        {"name": "demo", "scope": "live", "ttl_hours": 1},
    )
    one_time_link = create_response.get_json()["result"]["structuredContent"]["one_time_link"]
    assert client.get(one_time_link["url"]).status_code == 302
    assert client.get("/apps/demo/_dash-layout", base_url=_public_base_url()).status_code == 200

    revoke_response = _call_mcp(
        client,
        "app_share_revoke_one_time_link",
        {"name": "demo", "link_id": one_time_link["id"]},
    )
    blocked_response = client.get("/apps/demo/_dash-layout", base_url=_public_base_url())
    second_client_response = app.test_client().get(one_time_link["url"])

    assert revoke_response.status_code == 200
    assert revoke_response.get_json()["result"]["structuredContent"]["one_time_link"]["revoked_at"]
    assert blocked_response.status_code == 403
    assert second_client_response.status_code == 410
    assert second_client_response.get_json()["error"]["category"] == "share_link_revoked"


def test_expired_one_time_link_cannot_be_redeemed(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    token = "expired-token"
    app.extensions["registry"].create_share_link(
        "demo",
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        scope="live",
        role="viewer",
        expires_at="2000-01-01T00:00:00",
        max_uses=1,
        created_by_principal_id="test",
    )

    response = app.test_client().get(f"/share/links/{token}")

    assert response.status_code == 410
    assert response.get_json()["error"]["category"] == "share_link_expired"


def test_one_time_links_are_not_written_to_git_desired_state(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    create_response = _call_mcp(
        client,
        "app_share_create_one_time_link",
        {"name": "demo", "scope": "live", "ttl_hours": 1},
    )
    one_time_link = create_response.get_json()["result"]["structuredContent"]["one_time_link"]

    repo_root = Path(app.extensions["git_repo_service"].repo_root)
    desired_state = "\n".join(path.read_text() for path in (repo_root / "desired-state").rglob("*.yaml"))

    assert one_time_link["raw_token"] not in desired_state
    assert "share_links" not in desired_state
    assert "one_time_link" not in desired_state


def test_external_invitation_acceptance_creates_verified_user_grant_and_catalog_access(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {
            "name": "demo",
            "recipient_email": "External@Example.Test",
            "role": "viewer",
            "scope": "live",
            "ttl_hours": 1,
            "message": "Quarterly review",
        },
    )
    payload = invite_response.get_json()["result"]["structuredContent"]
    invitation = payload["invitation"]
    share_get = _call_mcp(client, "app_share_get", {"name": "demo"})
    share_payload = share_get.get_json()["result"]["structuredContent"]

    external_client = app.test_client()
    accept_response = external_client.get(invitation["accept_url"])
    layout_response = external_client.get("/apps/demo/_dash-layout", base_url=_public_base_url())
    homepage = external_client.get("/", base_url=_public_base_url())
    second_accept = app.test_client().get(invitation["accept_url"])
    accepted_invitation = app.extensions["registry"].get_invitation(invitation["id"])
    user = app.extensions["registry"].get_user_by_principal_id("dash-server:external:external@example.test")

    assert invite_response.status_code == 200
    assert invitation["raw_token"]
    assert invitation["display_once"] is True
    assert invitation["recipient_email"] == "external@example.test"
    assert invitation["delivery_status"] == "pending_manual_delivery"
    assert "token_hash" not in invitation
    assert "raw_token" not in share_payload["invitations"][0]
    assert "token_hash" not in share_payload["invitations"][0]
    assert accept_response.status_code == 302
    assert accept_response.headers["Location"] == "/apps/demo"
    assert layout_response.status_code == 200
    assert b"Demo Dashboard" in homepage.data
    assert second_accept.status_code == 410
    assert second_accept.get_json()["error"]["category"] == "invitation_used"
    assert accepted_invitation["status"] == "accepted"
    assert isinstance(accepted_invitation["grant_id"], int)
    assert user["user_type"] == "external"
    assert user["email_verified"] is True


def test_external_invitation_console_email_provider_marks_delivery_sent(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_EMAIL_PROVIDER"] = "console"
    config["DASH_SERVER_EMAIL_FROM"] = "dash@example.test"
    app = create_app(config)
    client = app.test_client()

    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {"name": "demo", "recipient_email": "external@example.test", "ttl_hours": 1},
    )
    invitation = invite_response.get_json()["result"]["structuredContent"]["invitation"]
    delivery = invite_response.get_json()["result"]["structuredContent"]["delivery"]

    assert invite_response.status_code == 200
    assert invitation["delivery_status"] == "sent"
    assert invitation["delivery_provider"] == "console"
    assert invitation["sent_at"] is not None
    assert delivery["mode"] == "email"
    assert delivery["provider"] == "console"
    assert delivery["error"] is None


def test_external_invitation_smtp_delivery_success_uses_mock_service(tmp_path, monkeypatch):
    class CapturingSMTP:
        instances = []

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.starttls_called = False
            self.login_args = None
            self.messages = []
            self.__class__.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def starttls(self, context=None):
            self.starttls_called = True
            return None

        def login(self, username, password):
            self.login_args = (username, password)
            return None

        def send_message(self, message):
            self.messages.append(message)
            return {}

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", CapturingSMTP)
    config = _hosted_mode_test_config(tmp_path)
    config.update(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_FROM_NAME": "Dash Server",
            "DASH_SERVER_EMAIL_REPLY_TO": "owners@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp.example.test",
            "DASH_SERVER_EMAIL_SMTP_USERNAME": "dash",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD": "secret",
        }
    )
    app = create_app(config)
    client = app.test_client()

    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {
            "name": "demo",
            "recipient_email": "external@example.test",
            "ttl_hours": 1,
            "message": "Please review the hosted dashboard.",
        },
    )
    payload = invite_response.get_json()["result"]["structuredContent"]
    invitation = payload["invitation"]
    delivery = payload["delivery"]

    assert invite_response.status_code == 200
    assert invitation["delivery_status"] == "sent"
    assert invitation["delivery_provider"] == "smtp"
    assert invitation["delivery_message_id"] is not None
    assert invitation["sent_at"] is not None
    assert delivery["status"] == "sent"
    assert delivery["provider"] == "smtp"
    assert delivery["mode"] == "email"
    assert delivery["error"] is None

    smtp_client = CapturingSMTP.instances[0]
    assert smtp_client.host == "smtp.example.test"
    assert smtp_client.port == 587
    assert smtp_client.starttls_called is True
    assert smtp_client.login_args == ("dash", "secret")
    assert len(smtp_client.messages) == 1
    message = smtp_client.messages[0]
    plain_body = message.get_body(preferencelist=("plain",)).get_content()

    assert message["To"] == "external@example.test"
    assert message["Reply-To"] == "owners@example.test"
    assert invitation["accept_url"] in plain_body
    assert "Invited by: admin-123@example.test" in plain_body
    assert "Please review the hosted dashboard." in plain_body


def test_external_invitation_smtp_delivery_failure_is_visible(tmp_path, monkeypatch):
    class FailingSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            raise RuntimeError("smtp unavailable")

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FailingSMTP)
    config = _hosted_mode_test_config(tmp_path)
    config.update(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp.example.test",
            "DASH_SERVER_EMAIL_SMTP_USERNAME": "dash",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD": "secret",
        }
    )
    app = create_app(config)
    client = app.test_client()

    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {"name": "demo", "recipient_email": "external@example.test", "ttl_hours": 1},
    )
    invitation = invite_response.get_json()["result"]["structuredContent"]["invitation"]
    delivery = invite_response.get_json()["result"]["structuredContent"]["delivery"]

    assert invite_response.status_code == 200
    assert invitation["delivery_status"] == "failed"
    assert invitation["delivery_provider"] == "smtp"
    assert "smtp unavailable" in invitation["delivery_error"]
    assert delivery["mode"] == "email"
    assert "smtp unavailable" in delivery["error"]


def test_hosted_email_provider_requires_secure_smtp_config(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config.update(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp.example.test",
            "DASH_SERVER_EMAIL_SMTP_USERNAME": "dash",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD": "secret",
            "DASH_SERVER_EMAIL_SMTP_USE_TLS": False,
            "DASH_SERVER_EMAIL_SMTP_USE_SSL": False,
        }
    )

    with pytest.raises(RuntimeError, match="TLS or SSL"):
        create_app(config)


def test_external_preview_invitation_grants_preview_only(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _start_demo_preview(client)
    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {
            "name": "demo",
            "recipient_email": "preview@example.test",
            "role": "preview_viewer",
            "scope": "preview",
            "ttl_hours": 1,
        },
    )
    invitation = invite_response.get_json()["result"]["structuredContent"]["invitation"]

    external_client = app.test_client()
    accept_response = external_client.get(invitation["accept_url"])
    preview_response = external_client.get("/preview/demo/2/_dash-layout", base_url=_public_base_url())
    live_response = external_client.get("/apps/demo/_dash-layout", base_url=_public_base_url())

    assert accept_response.status_code == 302
    assert accept_response.headers["Location"] == "/preview/demo/2"
    assert preview_response.status_code == 200
    assert live_response.status_code == 403


def test_external_invitation_revoke_blocks_pending_and_accepted_access(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    pending_invite = _call_mcp(
        client,
        "app_invite_external_user",
        {"name": "demo", "recipient_email": "pending@example.test", "ttl_hours": 1},
    ).get_json()["result"]["structuredContent"]["invitation"]
    revoke_pending = _call_mcp(
        client,
        "app_revoke_external_invitation",
        {"name": "demo", "invitation_id": pending_invite["id"]},
    )
    pending_accept = app.test_client().get(pending_invite["accept_url"])

    accepted_invite = _call_mcp(
        client,
        "app_invite_external_user",
        {"name": "demo", "recipient_email": "accepted@example.test", "ttl_hours": 1},
    ).get_json()["result"]["structuredContent"]["invitation"]
    external_client = app.test_client()
    assert external_client.get(accepted_invite["accept_url"]).status_code == 302
    assert external_client.get("/apps/demo/_dash-layout", base_url=_public_base_url()).status_code == 200
    revoke_accepted = _call_mcp(
        client,
        "app_revoke_external_invitation",
        {"name": "demo", "invitation_id": accepted_invite["id"]},
    )
    blocked = external_client.get("/apps/demo/_dash-layout", base_url=_public_base_url())

    assert revoke_pending.status_code == 200
    assert pending_accept.status_code == 410
    assert pending_accept.get_json()["error"]["category"] == "invitation_revoked"
    assert revoke_accepted.status_code == 200
    assert revoke_accepted.get_json()["result"]["structuredContent"]["invitation"]["status"] == "revoked"
    assert blocked.status_code == 403


def test_expired_external_invitation_cannot_be_accepted(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    token = "expired-invitation"
    app.extensions["registry"].create_invitation(
        "demo",
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        recipient_email="expired@example.test",
        email_normalized="expired@example.test",
        scope="live",
        role="viewer",
        expires_at="2000-01-01T00:00:00",
        created_by_principal_id="test",
    )

    response = app.test_client().get(f"/share/invitations/{token}")

    assert response.status_code == 410
    assert response.get_json()["error"]["category"] == "invitation_expired"


def test_external_invitations_are_not_written_to_git_desired_state(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    invite_response = _call_mcp(
        client,
        "app_invite_external_user",
        {"name": "demo", "recipient_email": "external@example.test", "ttl_hours": 1},
    )
    invitation = invite_response.get_json()["result"]["structuredContent"]["invitation"]

    repo_root = Path(app.extensions["git_repo_service"].repo_root)
    desired_state = "\n".join(path.read_text() for path in (repo_root / "desired-state").rglob("*.yaml"))

    assert invitation["raw_token"] not in desired_state
    assert "app_invitations" not in desired_state
    assert "external@example.test" not in desired_state


def test_hosted_dispatcher_allows_anonymous_public_dashboard_only_when_policy_allows(tmp_path):
    denied_config = _hosted_mode_test_config(tmp_path / "denied")
    denied_app = create_app(denied_config)
    denied_app.extensions["runtime_service"].update_visibility("demo", "public")

    denied_response = denied_app.test_client().get("/apps/demo/_dash-layout")

    allowed_config = _hosted_mode_test_config(tmp_path / "allowed")
    allowed_config["DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED"] = True
    allowed_app = create_app(allowed_config)
    allowed_app.extensions["runtime_service"].update_visibility("demo", "public")
    allowed_client = allowed_app.test_client()
    _call_mcp(
        allowed_client,
        "app_share_set_link_scope",
        {"name": "demo", "link_scope": "public"},
    )

    allowed_response = allowed_client.get("/apps/demo/_dash-layout")

    assert denied_response.status_code == 401
    assert denied_response.get_json()["error"]["details"]["reason"] == "authentication_required"
    assert allowed_response.status_code == 200
    assert "Demo Dashboard" in json.dumps(allowed_response.get_json())


def test_public_dashboard_requires_app_policy_and_tenant_policy(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED"] = True
    app = create_app(config)
    app.extensions["runtime_service"].update_visibility("demo", "public")
    client = app.test_client()

    _call_mcp(client, "app_share_set_link_scope", {"name": "demo", "link_scope": "restricted"})
    denied = client.get("/apps/demo/_dash-layout")
    _call_mcp(client, "app_share_set_link_scope", {"name": "demo", "link_scope": "public"})
    allowed = client.get("/apps/demo/_dash-layout")

    assert denied.status_code == 401
    assert denied.get_json()["error"]["details"]["matched_policy"] is None
    assert allowed.status_code == 200


def test_hosted_catalog_hides_restricted_dashboard_from_anonymous_viewer(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    homepage = client.get("/")

    assert homepage.status_code == 200
    assert b"Demo Dashboard" not in homepage.data
    assert b"/apps/demo" not in homepage.data
    assert b"No dashboards are visible for this viewer." in homepage.data


def test_hosted_catalog_shows_public_dashboard_only_with_public_policy(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config["DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED"] = True
    app = create_app(config)
    app.extensions["runtime_service"].update_visibility("demo", "public")
    client = app.test_client()

    hidden = client.get("/")
    _call_mcp(client, "app_share_set_link_scope", {"name": "demo", "link_scope": "public"})
    visible = client.get("/")

    assert b"Demo Dashboard" not in hidden.data
    assert b"Demo Dashboard" in visible.data
    assert b"/apps/demo" in visible.data
    assert b"public_catalog" in visible.data


def test_domain_share_policy_requires_allowed_domain(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()

    response = _call_mcp(client, "app_share_set_link_scope", {"name": "demo", "link_scope": "domain"})
    result = response.get_json()["result"]

    assert response.status_code == 200
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["category"] == "tool_validation_error"
    assert result["structuredContent"]["error"]["details"]["field"] == "allowed_domain"


def test_hosted_catalog_domain_policy_matches_only_allowed_domain(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    set_policy = _call_mcp(
        client,
        "app_share_set_link_scope",
        {"name": "demo", "link_scope": "domain", "allowed_domain": "Example.Test"},
    )

    same_domain = client.get(
        "/",
        headers={
            "X-Forwarded-User": "same-domain",
            "X-Forwarded-Email": "same@example.test",
            "X-Forwarded-Groups": "",
        },
    )
    other_domain = client.get(
        "/",
        headers={
            "X-Forwarded-User": "other-domain",
            "X-Forwarded-Email": "other@other.test",
            "X-Forwarded-Groups": "",
        },
    )
    same_domain_live = client.get(
        "/apps/demo/_dash-layout",
        headers={
            "X-Forwarded-User": "same-domain",
            "X-Forwarded-Email": "same@example.test",
            "X-Forwarded-Groups": "",
        },
    )

    policy = set_policy.get_json()["result"]["structuredContent"]["share_policy"]
    assert policy["allowed_domain"] == "example.test"
    assert b"Demo Dashboard" in same_domain.data
    assert b"matched_share_policy" in same_domain.data
    assert b"/apps/demo" not in same_domain.data
    assert b"Open live" not in same_domain.data
    assert b"Demo Dashboard" not in other_domain.data
    assert same_domain_live.status_code == 403


def test_hosted_catalog_shows_direct_and_group_grants_and_hides_after_revoke(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    user_grant = _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "user",
            "principal_id": "trusted_proxy:user-123",
            "role": "viewer",
            "scope": "live",
        },
    ).get_json()["result"]["structuredContent"]["grant"]
    visible_by_user = client.get("/", headers=_trusted_proxy_headers())

    _call_mcp(client, "app_share_revoke", {"name": "demo", "grant_id": user_grant["id"]})
    hidden_after_user_revoke = client.get("/", headers=_trusted_proxy_headers())

    group_grant = _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "group",
            "principal_id": "finance",
            "role": "viewer",
            "scope": "live",
        },
    ).get_json()["result"]["structuredContent"]["grant"]
    visible_by_group = client.get("/", headers=_trusted_proxy_headers())

    _call_mcp(client, "app_share_revoke", {"name": "demo", "grant_id": group_grant["id"]})
    hidden_after_group_revoke = client.get("/", headers=_trusted_proxy_headers())

    assert b"Demo Dashboard" in visible_by_user.data
    assert b"matched_grant" in visible_by_user.data
    assert b"Demo Dashboard" not in hidden_after_user_revoke.data
    assert b"Demo Dashboard" in visible_by_group.data
    assert b"Demo Dashboard" not in hidden_after_group_revoke.data


def test_hosted_catalog_hides_preview_link_without_preview_access(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _start_demo_preview(client)
    _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "user",
            "principal_id": "trusted_proxy:user-123",
            "role": "viewer",
            "scope": "live",
        },
    )

    viewer_homepage = client.get("/", headers=_trusted_proxy_headers())

    assert b"Demo Dashboard" in viewer_homepage.data
    assert b"/apps/demo" in viewer_homepage.data
    assert b"/preview/demo/2" not in viewer_homepage.data
    assert b"Open preview" not in viewer_homepage.data


def test_hosted_catalog_hides_live_link_for_preview_only_access(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    _start_demo_preview(client)
    _call_mcp(
        client,
        "app_share_grant",
        {
            "name": "demo",
            "principal_type": "user",
            "principal_id": "trusted_proxy:user-123",
            "role": "preview_viewer",
            "scope": "preview",
        },
    )

    viewer_homepage = client.get("/", headers=_trusted_proxy_headers())
    live_response = client.get("/apps/demo/_dash-layout", headers=_trusted_proxy_headers())
    preview_response = client.get("/preview/demo/2/_dash-layout", headers=_trusted_proxy_headers())

    assert b"Demo Dashboard" in viewer_homepage.data
    assert b"Preview Only" in viewer_homepage.data
    assert b"/apps/demo" not in viewer_homepage.data
    assert b"Open live" not in viewer_homepage.data
    assert b"/preview/demo/2" in viewer_homepage.data
    assert b"Open preview" in viewer_homepage.data
    assert live_response.status_code == 403
    assert preview_response.status_code == 200


def test_hosted_dispatcher_blocks_preview_for_anonymous_and_viewer_principals(tmp_path):
    app = create_app(_hosted_mode_test_config(tmp_path))
    client = app.test_client()
    build_response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers("admin-123"),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "demo",
                    "bundle": {
                        "manifest": {
                            "name": "demo",
                            "title": "Demo Dashboard",
                            "route": "/apps/demo",
                            "description": "Demo preview revision.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Demo Preview",
                            "summary": "Preview-only test revision.",
                            "metrics": [{"label": "Revenue", "value": "$1.2M"}],
                        },
                    },
                },
            },
        },
    )
    assert build_response.status_code == 200
    preview_response = client.post(
        "/mcp",
        headers=_trusted_proxy_headers("admin-123"),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "demo", "revision_number": 2},
            },
        },
    )
    assert preview_response.status_code == 200

    anonymous_response = client.get("/preview/demo/2/_dash-layout")
    viewer_response = client.get(
        "/preview/demo/2/_dash-layout",
        headers=_trusted_proxy_headers(),
    )

    assert anonymous_response.status_code == 401
    assert anonymous_response.get_json()["error"]["details"]["target_type"] == "preview_app"
    assert viewer_response.status_code == 403
    assert viewer_response.get_json()["error"]["details"]["reason"] == "missing_preview_capability"


def test_hosted_oidc_testing_callback_validates_state_and_nonce(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config.update(
        {
            "DASH_SERVER_AUTH_PROVIDER": "oidc",
            "DASH_SERVER_OIDC_ISSUER": "https://idp.example.test",
            "DASH_SERVER_OIDC_CLIENT_ID": "dash-server",
            "DASH_SERVER_OIDC_REDIRECT_URI": "https://dash.example.test/auth/callback",
            "DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS": True,
        }
    )
    app = create_app(config)
    client = app.test_client()

    login = client.get("/auth/login?next=/apps/demo")
    assert login.status_code == 302
    location = login.headers["Location"]
    query = parse_qs(urlparse(location).query)
    state = query["state"][0]
    nonce = query["nonce"][0]

    callback = client.get(
        "/auth/callback",
        query_string={
            "state": state,
            "id_token_payload": json.dumps(
                {
                    "iss": "https://idp.example.test",
                    "sub": "user-456",
                    "email": "viewer@example.test",
                    "email_verified": True,
                    "name": "Viewer Example",
                    "groups": ["sales"],
                    "nonce": nonce,
                }
            ),
        },
    )
    whoami = client.get("/auth/whoami")

    assert callback.status_code == 302
    assert callback.headers["Location"] == "/apps/demo"
    principal = whoami.get_json()["auth"]["principal"]
    assert principal["principal_id"] == "https://idp.example.test:user-456"
    assert principal["display_name"] == "Viewer Example"
    assert principal["email_verified"] is True
    assert principal["groups"] == ["sales"]


def test_hosted_oidc_testing_callback_rejects_bad_nonce(tmp_path):
    config = _hosted_mode_test_config(tmp_path)
    config.update(
        {
            "DASH_SERVER_AUTH_PROVIDER": "oidc",
            "DASH_SERVER_OIDC_ISSUER": "https://idp.example.test",
            "DASH_SERVER_OIDC_CLIENT_ID": "dash-server",
            "DASH_SERVER_OIDC_REDIRECT_URI": "https://dash.example.test/auth/callback",
            "DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS": True,
        }
    )
    app = create_app(config)
    client = app.test_client()

    login = client.get("/auth/login")
    query = parse_qs(urlparse(login.headers["Location"]).query)
    callback = client.get(
        "/auth/callback",
        query_string={
            "state": query["state"][0],
            "id_token_payload": json.dumps({"sub": "user-456", "nonce": "wrong"}),
        },
    )

    assert callback.status_code == 400
    assert callback.get_json()["error"]["message"] == "OIDC callback nonce did not match the login session."


def test_create_app_bootstraps_gitops_repo_and_demo_worktree(app):
    git_repo_service = app.extensions["git_repo_service"]
    repo_root = Path(git_repo_service.repo_root)
    draft = app.extensions["runtime_service"].workspace_service.draft_summary("demo")
    workspace_path = Path(draft["workspace_path"])

    assert (repo_root / ".git").exists()
    assert (repo_root / "apps" / "demo" / "app.py").exists()
    assert (repo_root / "apps" / "demo" / "dash-app.json").exists()
    assert (repo_root / "apps" / "demo" / "requirements.txt").exists()
    live_state = (repo_root / "desired-state" / "live" / "demo.yaml").read_text()
    assert "route: /apps/demo" in live_state

    status = git_repo_service.status()["repo"]
    assert status["initialized"] is True
    assert status["current_branch"] == "main"
    assert status["dirty"] is False
    assert status["tracked_apps"] == ["demo"]
    assert status["desired_live_apps"] == ["demo"]
    assert status["phase"] == "phase4a"
    assert "dash-server/demo/r000001" in status["release_tags"]
    assert draft["storage_backend"] == "git_worktree"
    assert workspace_path.exists()
    assert (workspace_path.parents[1] / ".git").exists()
    assert "draft/demo" in [worktree["branch"] for worktree in status["worktrees"]]
    assert status["dirty_worktrees"] == []
    assert isinstance(status["head_commit"], str)
    assert len(status["head_commit"]) == 40


def test_demo_dash_route_is_reachable(client):
    response = client.get("/apps/demo")
    layout_response = client.get("/apps/demo/_dash-layout")
    layout_payload = layout_response.get_json()
    layout_text = json.dumps(layout_payload)

    assert response.status_code == 200
    assert b"Demo Dashboard" in response.data
    assert b"/apps/demo/" in response.data
    assert layout_response.status_code == 200
    assert "Delivered by" in layout_text
    assert "Exasol" in layout_text
    assert "https://www.exasol.com/" in layout_text


def test_root_dashboard_catalog_lists_live_and_preview_routes(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
        "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets"),
        "AUTO_INSTALL_DEPENDENCIES": False,
    }
    app = create_app(config)
    client = app.test_client()

    create_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Radar",
                            "route": "/apps/sales",
                            "description": "Revenue, funnel, and territory coverage for the sales team.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Radar",
                            "summary": "Primary GTM scorecard.",
                            "metrics": [
                                {"label": "Pipeline", "value": "$3.1M"},
                                {"label": "Win Rate", "value": "29%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    build_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "sales",
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Radar",
                            "route": "/apps/sales",
                            "description": "Revenue, funnel, and territory coverage for the sales team.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Radar",
                            "summary": "Preview revision with a new leaderboard.",
                            "metrics": [
                                {"label": "Pipeline", "value": "$3.4M"},
                                {"label": "Win Rate", "value": "31%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    assert build_response.status_code == 200

    preview_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "sales", "revision_number": 2},
            },
        },
    )
    assert preview_response.status_code == 200

    homepage = client.get("/")

    assert homepage.status_code == 200
    assert b"Available dashboards" in homepage.data
    assert b"Sales Radar" in homepage.data
    assert b"Revenue, funnel, and territory coverage for the sales team." in homepage.data
    assert b"/apps/demo" in homepage.data
    assert b"/apps/sales" in homepage.data
    assert b"/preview/sales/2" in homepage.data
    assert b"Live + Preview" in homepage.data
    assert b"Open live" in homepage.data
    assert b"Open preview" in homepage.data


def test_create_app_bootstraps_exasol_profile_from_server_config(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
            "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
            "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
            "DEPENDENCY_STATE_ROOT": str(tmp_path / "dependency_state"),
            "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
            "EXASOL_SECRETS_ROOT": str(tmp_path / "exasol-secrets"),
            "EXASOL_BOOTSTRAP_PROFILE_NAME": "analytics-prod",
            "EXASOL_BOOTSTRAP_DSN": "demodb.exasol.com:8563",
            "EXASOL_BOOTSTRAP_USER": "sys",
            "EXASOL_BOOTSTRAP_SECRET_ENV_VAR": "EXA_PASSWORD",
            "EXASOL_BOOTSTRAP_DESCRIPTION": "Primary analytics database.",
        }
    )

    profile_path = Path(app.extensions["git_repo_service"].repo_root) / "profiles" / "exasol" / "analytics-prod.json"
    assert profile_path.exists()

    profile_payload = json.loads(profile_path.read_text())
    assert profile_payload["name"] == "analytics-prod"
    assert profile_payload["dsn"] == "demodb.exasol.com:8563"
    assert profile_payload["user"] == "sys"
    assert profile_payload["secret_ref"] == {"provider": "env", "key": "EXA_PASSWORD"}


def test_create_app_bootstraps_existing_workspace_artifacts(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    create_response = first_app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    second_app = create_app(config)
    response = second_app.test_client().get("/apps/sales")
    draft = second_app.extensions["runtime_service"].workspace_service.draft_summary("sales")

    assert response.status_code == 200
    assert b"Sales Dashboard v1" in response.data
    assert draft["storage_backend"] == "git_worktree"
    assert Path(draft["workspace_path"]).exists()


def test_git_desired_preview_state_survives_restart(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    client = first_app.test_client()

    create_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    build_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "sales",
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v2",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v2",
                            "summary": "Preview this revision after restart.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.8M"},
                                {"label": "Conversion", "value": "5.1%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    assert build_response.status_code == 200

    preview_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "sales", "revision_number": 2},
            },
        },
    )
    assert preview_response.status_code == 200

    repo_root = Path(first_app.extensions["git_repo_service"].repo_root)
    preview_state = (repo_root / "desired-state" / "preview" / "sales.yaml").read_text()
    assert "targetRevision: r000002" in preview_state

    second_app = create_app(config)
    preview_layout = second_app.test_client().get("/preview/sales/2/_dash-layout")
    assert preview_layout.status_code == 200
    assert "Sales Dashboard v2" in json.dumps(preview_layout.get_json())

    repo_status = second_app.extensions["git_repo_service"].status()["repo"]
    assert "sales" in repo_status["desired_preview_apps"]


def test_create_app_survives_broken_persisted_artifact_during_bootstrap(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    create_response = first_app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    runtime_service = first_app.extensions["runtime_service"]
    current_revision = runtime_service.registry.get_current_revision("sales")
    assert current_revision is not None
    artifact_app_path = Path(current_revision.artifact_path) / "app.py"
    artifact_app_path.write_text(
        "from dash import Dash, html\n\n"
        "def create_dash_app(server, url_base_pathname, metadata):\n"
        "    prefix = url_base_pathname.rstrip('/') + '/'\n"
        "    app = Dash(__name__, server=server, routes_pathname_prefix=prefix, requests_pathname_prefix=prefix)\n"
        "    app.layout = html.Div([html.H1('Broken Prefix App')])\n"
        "    return app\n"
    )

    second_app = create_app(config)
    demo_response = second_app.test_client().get("/apps/demo")
    sales_response = second_app.test_client().get("/apps/sales")

    assert demo_response.status_code == 200
    assert sales_response.status_code == 404

    diagnostics_service = second_app.extensions["diagnostics_service"]
    latest_error = diagnostics_service.latest_error("sales")
    assert latest_error is not None
    assert latest_error["category"] == "route_misconfiguration"


def test_create_app_rebuilds_registry_cache_from_git_after_registry_deletion(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    client = first_app.test_client()

    create_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    build_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "sales",
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v2",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v2",
                            "summary": "Preview this revision after cache rebuild.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.8M"},
                                {"label": "Conversion", "value": "5.1%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    assert build_response.status_code == 200

    preview_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "sales", "revision_number": 2},
            },
        },
    )
    assert preview_response.status_code == 200
    first_app.extensions["runtime_service"].update_route("sales", "/apps/sales-team")

    Path(config["REGISTRY_DB_PATH"]).unlink()

    second_app = create_app(config)
    second_client = second_app.test_client()
    sales_live = second_client.get("/apps/sales-team")
    sales_preview = second_client.get("/preview/sales/2/_dash-layout")
    registry = second_app.extensions["registry"]
    sales = registry.get_app("sales")
    revisions = registry.list_revisions("sales")

    assert sales_live.status_code == 200
    assert b"Sales Dashboard v1" in sales_live.data
    assert sales_preview.status_code == 200
    assert "Sales Dashboard v2" in json.dumps(sales_preview.get_json())
    assert sales is not None
    assert sales.route == "/apps/sales-team"
    assert sales.current_revision_number == 1
    assert sales.preview_revision_number == 2
    assert len(revisions) == 2


def test_create_app_rebuilds_unpublished_built_revision_history_from_git_after_registry_deletion(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    client = first_app.test_client()

    create_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    assert create_response.status_code == 200

    build_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "sales",
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v2",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v2",
                            "summary": "Built but not promoted.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.7M"},
                                {"label": "Conversion", "value": "5.0%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    built = build_response.get_json()["result"]["structuredContent"]["revision"]
    assert built["revision_number"] == 2

    Path(config["REGISTRY_DB_PATH"]).unlink()

    second_app = create_app(config)
    registry = second_app.extensions["registry"]
    revisions = registry.list_revisions("sales")
    rebuilt_revision = registry.get_revision_by_number("sales", 2)

    assert len(revisions) == 2
    assert rebuilt_revision is not None
    assert rebuilt_revision.git_tag == "dash-server/sales/r000002"
    assert rebuilt_revision.release_manifest_path == "releases/sales/r000002.yaml"
    assert len(rebuilt_revision.commit_sha) == 40


def test_create_app_rebuilds_canonical_events_from_git_after_registry_deletion(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    client = first_app.test_client()

    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v1",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.2M"},
                                {"label": "Conversion", "value": "4.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "sales",
                    "bundle": {
                        "manifest": {
                            "name": "sales",
                            "title": "Sales Dashboard v2",
                            "route": "/apps/sales",
                            "description": "Sales dashboard created through the MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Sales Dashboard v2",
                            "summary": "Preview and then promote.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.9M"},
                                {"label": "Conversion", "value": "5.2%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "app_start_preview",
                "arguments": {"name": "sales", "revision_number": 2},
            },
        },
    )
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "app_promote_revision",
                "arguments": {"name": "sales", "revision_number": 2},
            },
        },
    )
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "app_rollback",
                "arguments": {"name": "sales"},
            },
        },
    )

    Path(config["REGISTRY_DB_PATH"]).unlink()

    second_app = create_app(config)
    events = second_app.extensions["registry"].list_events("sales")
    event_types = [event.event_type for event in events]

    assert "app_created" in event_types
    assert "revision_built" in event_types
    assert "preview_started" in event_types
    assert "preview_cleared" in event_types
    assert "revision_promoted" in event_types
    assert "rolled_back" in event_types


def test_startup_reconcile_applies_external_git_desired_state_commit(tmp_path):
    config = {
        "TESTING": True,
        "REGISTRY_DB_PATH": str(tmp_path / "registry.sqlite3"),
        "ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        "WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "DIAGNOSTICS_ROOT": str(tmp_path / "diagnostics"),
        "GITOPS_REPO_PATH": str(tmp_path / "gitops-repo"),
    }
    first_app = create_app(config)
    client = first_app.test_client()

    create_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "app_create",
                "arguments": {
                    "bundle": {
                        "manifest": {
                            "name": "deals",
                            "title": "Deals Dashboard v1",
                            "route": "/apps/deals",
                            "description": "Deals dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Deals Dashboard v1",
                            "summary": "Initial live revision.",
                            "metrics": [
                                {"label": "Revenue", "value": "$900K"},
                                {"label": "Conversion", "value": "3.8%"},
                            ],
                        },
                    }
                },
            },
        },
    )
    created = create_response.get_json()["result"]["structuredContent"]

    build_response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "app_build",
                "arguments": {
                    "name": "deals",
                    "bundle": {
                        "manifest": {
                            "name": "deals",
                            "title": "Deals Dashboard v2",
                            "route": "/apps/deals",
                            "description": "Deals dashboard created through the Stage 4 MCP control plane.",
                            "template": "metric-cards",
                        },
                        "dashboard": {
                            "headline": "Deals Dashboard v2",
                            "summary": "Promoted by an external Git change.",
                            "metrics": [
                                {"label": "Revenue", "value": "$1.4M"},
                                {"label": "Conversion", "value": "4.2%"},
                            ],
                        },
                    },
                },
            },
        },
    )
    built = build_response.get_json()["result"]["structuredContent"]["revision"]

    repo_root = Path(first_app.extensions["git_repo_service"].repo_root)
    live_path = repo_root / "desired-state" / "live" / "deals.yaml"
    desired_live = live_path.read_text()
    desired_live = desired_live.replace("targetRevision: r000001", "targetRevision: r000002")
    desired_live = desired_live.replace(created["current_revision"]["commit_sha"], built["commit_sha"])
    desired_live = desired_live.replace(created["current_revision"]["git_tag"], built["git_tag"])
    desired_live = desired_live.replace(
        created["current_revision"]["release_manifest_path"],
        built["release_manifest_path"],
    )
    live_path.write_text(desired_live)
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "--", "desired-state/live/deals.yaml"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", "external: promote deals r000002"],
        check=True,
        capture_output=True,
        text=True,
    )

    second_app = create_app(config)
    second_client = second_app.test_client()
    live_layout = second_client.get("/apps/deals/_dash-layout")
    status = second_app.extensions["runtime_service"].get_app_status("deals")

    assert live_layout.status_code == 200
    assert "Deals Dashboard v2" in json.dumps(live_layout.get_json())
    assert status["current_revision"]["revision_number"] == 2
