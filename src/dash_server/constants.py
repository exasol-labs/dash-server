"""Shared closed value sets.

Deployment mode, auth provider, isolation mode, cookie SameSite, ACL grant
scope, and principal-type all have a fixed, small set of legal string values.
Those sets used to be written as inline ``{...}`` literals at each validator and
re-typed again at consumers. They are defined once here so a validator and the
code that produces the value can never disagree.
"""

from __future__ import annotations

# Deployment / auth (validated in app_factory).
DEPLOYMENT_MODES = frozenset({"local", "hosted"})
AUTH_PROVIDERS = frozenset({"disabled", "oidc", "trusted_proxy"})
SAMESITE_VALUES = frozenset({"lax", "strict", "none"})

# Runtime isolation (validated in app_factory).
DEPENDENCY_ISOLATION_MODES = frozenset({"shared", "per_app"})
RUNTIME_MODES = frozenset({"in_process", "isolated"})

# Sharing / ACL (authorization service and registry).
GRANT_SCOPES = frozenset({"live", "preview", "manage", "all"})
PRINCIPAL_TYPES = frozenset({"user", "group", "organization", "domain", "public", "link"})


__all__ = [
    "AUTH_PROVIDERS",
    "DEPENDENCY_ISOLATION_MODES",
    "DEPLOYMENT_MODES",
    "GRANT_SCOPES",
    "PRINCIPAL_TYPES",
    "RUNTIME_MODES",
    "SAMESITE_VALUES",
]
