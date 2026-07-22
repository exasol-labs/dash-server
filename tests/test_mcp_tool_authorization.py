"""Authorization matrix for handler-enforced app-scoped MCP tools (Wave 2, P2.3).

Every app-scoped tool that carries ``enforce_in_handler`` is gated in the
handler path by ``authorize_app(app, spec.app_capability)`` — the same call the
server makes. This drives that call across the global roles and asserts it
matches the capability matrix, generated from the ToolSpec table so a new
enforced tool is covered by construction. In particular it pins the gap that
Wave 2 closed: a global ``editor`` (which lacks ``dashboard.manage_sharing``) is
denied the sharing tools even though it clears the coarse ``/mcp`` gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dash_server.auth.capabilities import ROLE_CAPABILITIES
from dash_server.auth.models import AuthContext, Principal
from dash_server.mcp.tool_specs import TOOL_SPECS

_ENFORCED = [spec for spec in TOOL_SPECS if spec.enforce_in_handler]
_ROLES = ["viewer", "editor", "owner", "admin"]


def _global_role_context(role: str) -> AuthContext:
    principal = Principal.authenticated_user(
        issuer="trusted_proxy",
        subject=f"{role}-1",
        email=f"{role}@example.test",
        roles=(role,),
    )
    return AuthContext(mode="hosted", auth_enabled=True, provider="trusted_proxy", principal=principal)


def _expected_allowed(role: str, capability: str) -> bool:
    # Global viewer/preview_viewer roles never confer global capabilities
    # (they authorize only via grants/public policy); every other role confers
    # exactly its matrix capabilities.
    if role in {"viewer", "preview_viewer"}:
        return False
    return capability in ROLE_CAPABILITIES[role]


def test_there_is_an_enforced_sharing_tool():
    sharing = [s for s in _ENFORCED if s.app_capability == "dashboard.manage_sharing"]
    assert sharing, "expected sharing tools to be enforced in the handler path"


@pytest.mark.slow
@pytest.mark.parametrize("role", _ROLES)
def test_enforced_tool_capability_matches_role_matrix(make_hosted_app, role: str, tmp_path: Path):
    app = make_hosted_app()
    registry = app.extensions["registry"]
    authorization = app.extensions["authorization_service"]
    hosted_app = registry.get_app("demo")
    assert hosted_app is not None and hosted_app.enabled

    context = _global_role_context(role)
    for spec in _ENFORCED:
        decision = authorization.authorize_app(context, hosted_app, spec.app_capability)
        assert decision.allowed is _expected_allowed(role, spec.app_capability), (
            f"role={role} tool={spec.name} capability={spec.app_capability} "
            f"expected allowed={_expected_allowed(role, spec.app_capability)}"
        )


@pytest.mark.slow
def test_global_editor_is_denied_manage_sharing(make_hosted_app):
    """The exact gap Wave 2 closed: editor clears the /mcp gate but lacks manage_sharing."""
    app = make_hosted_app()
    registry = app.extensions["registry"]
    authorization = app.extensions["authorization_service"]
    hosted_app = registry.get_app("demo")

    editor = _global_role_context("editor")
    owner = _global_role_context("owner")
    assert authorization.authorize_app(editor, hosted_app, "dashboard.manage_sharing").allowed is False
    assert authorization.authorize_app(owner, hosted_app, "dashboard.manage_sharing").allowed is True
