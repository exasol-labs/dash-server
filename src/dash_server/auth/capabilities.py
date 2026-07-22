"""Capability-string constants and the role→capability matrix.

The capability strings authorization turns on used to be bare literals repeated
across the authorization service, the MCP transport gate, and the blueprint
capability map. They are named here once, and the role→capability matrix that
was previously private to ``AuthorizationService`` lives here so the ``/mcp``
gate can be *derived* from it (`roles_with_capability`) instead of hand-keeping
a parallel role set.
"""

from __future__ import annotations

# Dashboard capabilities.
DASHBOARD_DISCOVER = "dashboard.discover"
DASHBOARD_VIEW_LIVE = "dashboard.view_live"
DASHBOARD_VIEW_PREVIEW = "dashboard.view_preview"
DASHBOARD_VIEW_METADATA = "dashboard.view_metadata"
DASHBOARD_EXPORT = "dashboard.export"
DASHBOARD_MANAGE_CONSUMPTION = "dashboard.manage_consumption"
DASHBOARD_EDIT_DRAFT = "dashboard.edit_draft"
DASHBOARD_BUILD_PREVIEW = "dashboard.build_preview"
DASHBOARD_PROMOTE = "dashboard.promote"
DASHBOARD_MANAGE_SHARING = "dashboard.manage_sharing"
DASHBOARD_DELETE = "dashboard.delete"

# Cross-cutting capabilities.
DIAGNOSTICS_VIEW = "diagnostics.view"
MCP_USE_CONTROL_PLANE = "mcp.use_control_plane"
TENANT_ADMIN = "tenant.admin"


# Role → capabilities. ``mcp.use_control_plane`` is carried by admin, owner, and
# editor: that is the set the ``/mcp`` transport gate has always admitted, now
# declared in one place rather than as a literal role set in the blueprint.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset(
        {
            DASHBOARD_DISCOVER,
            DASHBOARD_VIEW_LIVE,
            DASHBOARD_VIEW_METADATA,
            DASHBOARD_EXPORT,
        }
    ),
    "preview_viewer": frozenset(
        {
            DASHBOARD_DISCOVER,
            DASHBOARD_VIEW_PREVIEW,
            DASHBOARD_VIEW_METADATA,
        }
    ),
    "editor": frozenset(
        {
            DASHBOARD_DISCOVER,
            DASHBOARD_VIEW_LIVE,
            DASHBOARD_VIEW_PREVIEW,
            DASHBOARD_VIEW_METADATA,
            DASHBOARD_EXPORT,
            DASHBOARD_EDIT_DRAFT,
            DASHBOARD_BUILD_PREVIEW,
            DIAGNOSTICS_VIEW,
            MCP_USE_CONTROL_PLANE,
        }
    ),
    "owner": frozenset(
        {
            DASHBOARD_DISCOVER,
            DASHBOARD_VIEW_LIVE,
            DASHBOARD_VIEW_PREVIEW,
            DASHBOARD_VIEW_METADATA,
            DASHBOARD_EXPORT,
            DASHBOARD_MANAGE_CONSUMPTION,
            DASHBOARD_EDIT_DRAFT,
            DASHBOARD_BUILD_PREVIEW,
            DASHBOARD_PROMOTE,
            DASHBOARD_MANAGE_SHARING,
            DASHBOARD_DELETE,
            DIAGNOSTICS_VIEW,
            MCP_USE_CONTROL_PLANE,
        }
    ),
    "admin": frozenset(
        {
            DASHBOARD_DISCOVER,
            DASHBOARD_VIEW_LIVE,
            DASHBOARD_VIEW_PREVIEW,
            DASHBOARD_VIEW_METADATA,
            DASHBOARD_EXPORT,
            DASHBOARD_MANAGE_CONSUMPTION,
            DASHBOARD_EDIT_DRAFT,
            DASHBOARD_BUILD_PREVIEW,
            DASHBOARD_PROMOTE,
            DASHBOARD_MANAGE_SHARING,
            DASHBOARD_DELETE,
            DIAGNOSTICS_VIEW,
            MCP_USE_CONTROL_PLANE,
            TENANT_ADMIN,
        }
    ),
}


def roles_with_capability(capability: str) -> frozenset[str]:
    """Return the roles whose matrix entry carries ``capability``."""

    return frozenset(
        role for role, capabilities in ROLE_CAPABILITIES.items() if capability in capabilities
    )
