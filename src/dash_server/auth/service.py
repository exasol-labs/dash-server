"""Request-scoped identity resolution for local and hosted deployments."""

from __future__ import annotations

from dataclasses import replace
import ipaddress
import json
import secrets
from typing import Any
from urllib.parse import urlencode

from flask import current_app, g, request, session

from dash_server.config import coerce_bool
from dash_server.exceptions import DashServerError

from .models import AuthContext, Principal


class IdentityService:
    """Resolve principals from local mode, sessions, or trusted proxy headers."""

    _session_principal_key = "dash_server_principal"
    _oidc_state_key = "dash_server_oidc_state"
    _oidc_nonce_key = "dash_server_oidc_nonce"
    _oidc_next_key = "dash_server_oidc_next"

    def __init__(self, config: dict[str, Any]) -> None:
        self.mode = str(config.get("DASH_SERVER_MODE", "local"))
        self.auth_enabled = coerce_bool(config.get("DASH_SERVER_AUTH_ENABLED"))
        self.provider = str(config.get("DASH_SERVER_AUTH_PROVIDER", "disabled"))
        self.oidc_issuer = config.get("DASH_SERVER_OIDC_ISSUER")
        self.oidc_client_id = config.get("DASH_SERVER_OIDC_CLIENT_ID")
        self.oidc_redirect_uri = config.get("DASH_SERVER_OIDC_REDIRECT_URI")
        self.oidc_authorization_endpoint = config.get("DASH_SERVER_OIDC_AUTHORIZATION_ENDPOINT")
        self.oidc_scopes = str(config.get("DASH_SERVER_OIDC_SCOPES", "openid email profile"))
        self.oidc_groups_claim = str(config.get("DASH_SERVER_OIDC_GROUPS_CLAIM", "groups"))
        self.oidc_org_claim = config.get("DASH_SERVER_OIDC_ORG_CLAIM")
        self.oidc_accept_test_tokens = coerce_bool(config.get("DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS"))
        self.trusted_proxy_headers_enabled = bool(
            config.get("DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED", False)
        )
        self.trusted_proxy_allowed_cidrs = tuple(
            str(item).strip()
            for item in (config.get("DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS") or ())
            if str(item).strip()
        )
        self.trusted_proxy_user_header = str(
            config.get("DASH_SERVER_TRUSTED_PROXY_USER_HEADER", "X-Forwarded-User")
        )
        self.trusted_proxy_email_header = str(
            config.get("DASH_SERVER_TRUSTED_PROXY_EMAIL_HEADER", "X-Forwarded-Email")
        )
        self.trusted_proxy_groups_header = str(
            config.get("DASH_SERVER_TRUSTED_PROXY_GROUPS_HEADER", "X-Forwarded-Groups")
        )
        raw_bootstrap_admins = config.get("DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS") or ()
        if isinstance(raw_bootstrap_admins, str):
            raw_bootstrap_admins = raw_bootstrap_admins.split(",")
        self.bootstrap_admin_principal_ids = frozenset(
            str(item).strip()
            for item in raw_bootstrap_admins
            if str(item).strip()
        )

    def context_for_request(self) -> AuthContext:
        if self.mode == "local":
            return AuthContext.for_mode("local", auth_enabled=self.auth_enabled, provider=self.provider)
        if not self.auth_enabled:
            return AuthContext.for_mode(self.mode, auth_enabled=False, provider=self.provider)
        if self.provider == "trusted_proxy":
            trusted_principal = self._with_bootstrap_roles(self._trusted_proxy_principal())
            if trusted_principal.is_authenticated:
                return AuthContext(
                    mode=self.mode,
                    auth_enabled=True,
                    provider=self.provider,
                    principal=trusted_principal,
                )
            return AuthContext(
                mode=self.mode,
                auth_enabled=True,
                provider=self.provider,
                principal=self._session_principal(),
            )
        if self.provider == "oidc":
            return AuthContext(
                mode=self.mode,
                auth_enabled=True,
                provider=self.provider,
                principal=self._with_bootstrap_roles(self._session_principal()),
            )
        return AuthContext.for_mode(self.mode, auth_enabled=self.auth_enabled, provider=self.provider)

    def oidc_authorization_url(self, *, next_url: str = "/") -> str:
        self._require_provider("oidc")
        if not isinstance(self.oidc_client_id, str) or not self.oidc_client_id:
            raise self._auth_error("oidc_config_error", "OIDC client id is not configured.")
        if not isinstance(self.oidc_redirect_uri, str) or not self.oidc_redirect_uri:
            raise self._auth_error("oidc_config_error", "OIDC redirect uri is not configured.")
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        session[self._oidc_state_key] = state
        session[self._oidc_nonce_key] = nonce
        session[self._oidc_next_key] = next_url if next_url.startswith("/") else "/"
        params = {
            "client_id": self.oidc_client_id,
            "redirect_uri": self.oidc_redirect_uri,
            "response_type": "code",
            "scope": self.oidc_scopes,
            "state": state,
            "nonce": nonce,
        }
        return f"{self._authorization_endpoint()}?{urlencode(params)}"

    def complete_oidc_callback(self, args: dict[str, Any]) -> tuple[Principal, str]:
        self._require_provider("oidc")
        error = args.get("error")
        if isinstance(error, str) and error:
            raise self._auth_error("oidc_callback_error", f"OIDC provider returned error: {error}.")

        expected_state = session.pop(self._oidc_state_key, None)
        expected_nonce = session.pop(self._oidc_nonce_key, None)
        next_url = session.pop(self._oidc_next_key, "/")
        supplied_state = args.get("state")
        if not expected_state or supplied_state != expected_state:
            raise self._auth_error("oidc_state_error", "OIDC callback state did not match the login session.")

        payload = self._testing_oidc_payload(args)
        if payload is None:
            raise self._auth_error(
                "oidc_exchange_not_implemented",
                "OIDC token exchange is not implemented yet; use trusted_proxy or testing OIDC payloads in Phase 1.",
                http_status=501,
            )
        if not expected_nonce or payload.get("nonce") != expected_nonce:
            raise self._auth_error("oidc_nonce_error", "OIDC callback nonce did not match the login session.")

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise self._auth_error("oidc_claim_error", "OIDC test payload must include sub.")
        issuer = payload.get("iss") if isinstance(payload.get("iss"), str) else self.oidc_issuer
        if not isinstance(issuer, str) or not issuer:
            raise self._auth_error("oidc_claim_error", "OIDC issuer is not available.")
        groups = payload.get(self.oidc_groups_claim, ())
        if not isinstance(groups, list):
            groups = []
        tenant_id = None
        if isinstance(self.oidc_org_claim, str):
            tenant_claim = payload.get(self.oidc_org_claim)
            tenant_id = tenant_claim if isinstance(tenant_claim, str) else None
        principal = Principal.authenticated_user(
            issuer=issuer,
            subject=subject,
            email=payload.get("email") if isinstance(payload.get("email"), str) else None,
            display_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
            groups=tuple(str(item) for item in groups if isinstance(item, str)),
            email_verified=bool(payload.get("email_verified", False)),
            tenant_id=tenant_id,
        )
        principal = self._with_bootstrap_roles(principal)
        session[self._session_principal_key] = principal.to_dict()
        return principal, next_url if isinstance(next_url, str) and next_url.startswith("/") else "/"

    def logout(self) -> None:
        session.pop(self._session_principal_key, None)
        session.pop(self._oidc_state_key, None)
        session.pop(self._oidc_nonce_key, None)
        session.pop(self._oidc_next_key, None)

    def store_session_principal(self, principal: Principal) -> None:
        session[self._session_principal_key] = principal.to_dict()

    def _session_principal(self) -> Principal:
        payload = session.get(self._session_principal_key)
        if isinstance(payload, dict):
            return Principal.from_dict(payload)
        return Principal.anonymous()

    def _trusted_proxy_principal(self) -> Principal:
        if not self.trusted_proxy_headers_enabled or not self._request_from_trusted_proxy():
            return Principal.anonymous()
        subject = request.headers.get(self.trusted_proxy_user_header)
        if not subject:
            return Principal.anonymous()
        email = request.headers.get(self.trusted_proxy_email_header)
        groups_header = request.headers.get(self.trusted_proxy_groups_header, "")
        groups = tuple(
            item.strip()
            for item in groups_header.split(",")
            if item.strip()
        )
        return Principal.authenticated_user(
            issuer="trusted_proxy",
            subject=subject,
            email=email,
            display_name=email or subject,
            groups=groups,
            email_verified=bool(email),
        )

    def _with_bootstrap_roles(self, principal: Principal) -> Principal:
        if not principal.is_authenticated or principal.principal_id not in self.bootstrap_admin_principal_ids:
            return principal
        roles = tuple(dict.fromkeys((*principal.roles, "admin", "owner", "editor", "viewer")))
        return replace(principal, roles=roles)

    def _request_from_trusted_proxy(self) -> bool:
        if not self.trusted_proxy_allowed_cidrs:
            return False
        remote_addr = request.remote_addr
        if not remote_addr:
            return False
        try:
            remote_ip = ipaddress.ip_address(remote_addr)
        except ValueError:
            return False
        for cidr in self.trusted_proxy_allowed_cidrs:
            try:
                if remote_ip in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def _authorization_endpoint(self) -> str:
        if isinstance(self.oidc_authorization_endpoint, str) and self.oidc_authorization_endpoint:
            return self.oidc_authorization_endpoint
        if isinstance(self.oidc_issuer, str) and self.oidc_issuer:
            return f"{self.oidc_issuer.rstrip('/')}/authorize"
        raise self._auth_error("oidc_config_error", "OIDC issuer is not configured.")

    def _testing_oidc_payload(self, args: dict[str, Any]) -> dict[str, Any] | None:
        raw_payload = args.get("id_token_payload")
        if raw_payload is None:
            return None
        if not self.oidc_accept_test_tokens:
            raise self._auth_error(
                "oidc_test_token_error",
                "OIDC testing payloads are disabled for this server.",
            )
        if not isinstance(raw_payload, str):
            raise self._auth_error("oidc_test_token_error", "OIDC testing payload must be JSON text.")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise self._auth_error("oidc_test_token_error", "OIDC testing payload must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise self._auth_error("oidc_test_token_error", "OIDC testing payload must be a JSON object.")
        return payload

    def _require_provider(self, provider: str) -> None:
        if self.provider != provider:
            raise self._auth_error(
                "auth_provider_error",
                f"Auth provider {self.provider} does not support this operation.",
                http_status=404,
            )

    def _auth_error(self, category: str, summary: str, *, http_status: int | None = None) -> DashServerError:
        return DashServerError(
            category=category,
            summary=summary,
            details={"provider": self.provider, "mode": self.mode},
            http_status=http_status,
        )


def current_auth_context() -> AuthContext:
    context = getattr(g, "auth_context", None)
    if isinstance(context, AuthContext):
        return context
    service = current_app.extensions["identity_service"]
    context = service.context_for_request()
    g.auth_context = context
    return context
