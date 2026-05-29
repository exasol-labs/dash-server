"""Stable runtime helpers for hosted Dash apps.

This package is what gets installed into every per-app dependency environment created by
``DependencyEnvironmentService``. It is intentionally small: a few re-exports of the helpers
hosted apps actually call from inside their factories and callbacks. The control-plane
``dash_server`` package depends on the same underlying implementations, so in-process and
out-of-process serving share one source of truth.

Stability contract: anything imported from ``dash_server_runtime`` is part of the worker /
hosted-app contract and changes only with a major release. The fully-qualified
``dash_server.exasol.runtime`` import path continues to work for backwards compatibility,
but new generated scaffolds should prefer ``from dash_server_runtime import …``.

The version is recorded in each environment's identity key so a server upgrade that bumps
this package triggers a deterministic env rebuild rather than a silent skew.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Re-export the hosted-runtime entry points. Each one is a thin alias for the canonical
# implementation in `dash_server.*` so editing happens in exactly one place.
from dash_server.dash_apps.branding import apply_hosted_footer
from dash_server.dash_apps.callback_isolation import finalize_dash_app_callbacks
from dash_server.exasol.runtime import (
    execute_profile_query,
    query_one,
    query_rows,
    query_scalar,
)

__all__ = [
    "__version__",
    "apply_hosted_footer",
    "execute_profile_query",
    "finalize_dash_app_callbacks",
    "query_one",
    "query_rows",
    "query_scalar",
]
