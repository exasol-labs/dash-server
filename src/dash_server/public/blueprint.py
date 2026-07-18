"""Public routes for dashboard discovery."""

from __future__ import annotations

from datetime import datetime
import hashlib

from flask import Blueprint, current_app, jsonify, redirect, render_template

from dash_server.auth import Principal, current_auth_context


def create_public_blueprint() -> Blueprint:
    """Create the public landing page blueprint."""

    blueprint = Blueprint("public", __name__)

    @blueprint.get("/")
    def dashboard_catalog():
        runtime_service = current_app.extensions["runtime_service"]
        auth_context = current_auth_context()
        catalog = runtime_service.list_dashboard_catalog(
            auth_context=auth_context,
            authorization_service=current_app.extensions["authorization_service"],
        )
        consumption_policy = current_app.extensions["consumption_service"].policy
        if (
            not auth_context.principal.is_authenticated
            and not consumption_policy.public_exports_enabled
        ):
            for app_entry in catalog["apps"]:
                app_entry["consumption"]["visible"] = False
        return render_template("dashboard_catalog.html", catalog=catalog)

    @blueprint.get("/share/links/<token>")
    def redeem_share_link(token: str):
        registry = current_app.extensions["registry"]
        runtime_service = current_app.extensions["runtime_service"]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        link = registry.get_share_link_by_hash(token_hash)
        if link is None:
            return _share_link_error("share_link_not_found", "This sharing link is not valid.", 404)
        if link["revoked_at"] is not None:
            return _share_link_error("share_link_revoked", "This sharing link has been revoked.", 410)
        if int(link["use_count"]) >= int(link["max_uses"]):
            return _share_link_error("share_link_used", "This one-time sharing link has already been used.", 410)
        if _is_expired(str(link["expires_at"])):
            return _share_link_error("share_link_expired", "This sharing link has expired.", 410)

        app = registry.get_app(link["app_name"])
        if app is None:
            return _share_link_error("share_link_app_missing", "The shared dashboard no longer exists.", 404)

        redeemed_link = registry.mark_share_link_redeemed(int(link["id"]))
        if redeemed_link is None:
            latest_link = registry.get_share_link(int(link["id"]))
            if latest_link is not None and latest_link["revoked_at"] is not None:
                return _share_link_error("share_link_revoked", "This sharing link has been revoked.", 410)
            return _share_link_error("share_link_used", "This one-time sharing link has already been used.", 410)
        principal = Principal.link_access(
            link_id=int(link["id"]),
            app_name=link["app_name"],
            role=link["role"],
            scope=link["scope"],
            email=link["recipient_email"],
        )
        existing_grants = [
            grant for grant in registry.list_acl_entries(link["app_name"])
            if grant["principal_type"] == "link" and grant["principal_id"] == principal.principal_id
        ]
        if not existing_grants:
            registry.grant_app_access(
                link["app_name"],
                principal_type="link",
                principal_id=principal.principal_id,
                role=link["role"],
                scope=link["scope"],
                created_by_principal_id=link["created_by_principal_id"],
                expires_at=link["expires_at"],
            )
        current_app.extensions["identity_service"].store_session_principal(principal)

        if link["scope"] == "preview" and app.preview_revision_number is not None:
            return redirect(runtime_service.preview_path(app.name, app.preview_revision_number))
        return redirect(app.route)

    @blueprint.get("/share/invitations/<token>")
    def accept_invitation(token: str):
        registry = current_app.extensions["registry"]
        runtime_service = current_app.extensions["runtime_service"]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invitation = registry.get_invitation_by_hash(token_hash)
        if invitation is None:
            return _share_link_error("invitation_not_found", "This invitation is not valid.", 404)
        if invitation["revoked_at"] is not None or invitation["status"] == "revoked":
            return _share_link_error("invitation_revoked", "This invitation has been revoked.", 410)
        if invitation["status"] == "accepted" or invitation["accepted_at"] is not None:
            return _share_link_error("invitation_used", "This invitation has already been accepted.", 410)
        if _is_expired(str(invitation["expires_at"])):
            return _share_link_error("invitation_expired", "This invitation has expired.", 410)

        app = registry.get_app(invitation["app_name"])
        if app is None:
            return _share_link_error("invitation_app_missing", "The invited dashboard no longer exists.", 404)

        principal = Principal.authenticated_user(
            issuer="dash-server:external",
            subject=invitation["email_normalized"],
            email=invitation["recipient_email"],
            display_name=invitation["recipient_email"],
            roles=(),
            email_verified=True,
        )
        accepted = registry.mark_invitation_accepted(
            int(invitation["id"]),
            accepted_principal_id=principal.principal_id,
        )
        if accepted is None:
            latest_invitation = registry.get_invitation(int(invitation["id"]))
            if latest_invitation is not None and latest_invitation["revoked_at"] is not None:
                return _share_link_error("invitation_revoked", "This invitation has been revoked.", 410)
            return _share_link_error("invitation_used", "This invitation has already been accepted.", 410)

        registry.upsert_principal_user(principal, user_type="external")
        grant = registry.grant_app_access(
            invitation["app_name"],
            principal_type="user",
            principal_id=principal.principal_id,
            role=invitation["role"],
            scope=invitation["scope"],
            created_by_principal_id=invitation["created_by_principal_id"],
            expires_at=invitation["expires_at"],
        )
        registry.attach_invitation_grant(int(invitation["id"]), int(grant["id"]))
        registry.append_event(
            invitation["app_name"],
            "external_invitation_accepted",
            data={
                "invitation_id": invitation["id"],
                "grant_id": grant["id"],
                "principal_id": principal.principal_id,
            },
        )
        current_app.extensions["identity_service"].store_session_principal(principal)

        if invitation["scope"] == "preview" and app.preview_revision_number is not None:
            return redirect(runtime_service.preview_path(app.name, app.preview_revision_number))
        return redirect(app.route)

    return blueprint


def _is_expired(expires_at: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at) <= datetime.utcnow()
    except ValueError:
        return False


def _share_link_error(category: str, message: str, status_code: int):
    return jsonify({"error": {"category": category, "message": message}}), status_code
