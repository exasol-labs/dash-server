"""Shared MCP resource-URI patterns.

The two app-scoped resource URIs that carry an authorization capability were
written as raw regexes in both the server's read ladder and the blueprint's
transport gate. They live here once so the two cannot drift.
"""

from __future__ import annotations

import re

# `dash://apps/<app>/outputs` and `dash://exports/<job>` are the only resources
# gated by a capability (dashboard.export); both the server dispatch table and
# the blueprint transport gate match against these compiled patterns.
APP_OUTPUTS_RESOURCE_RE = re.compile(r"dash://apps/([a-z0-9-]+)/outputs")
EXPORT_RESOURCE_RE = re.compile(r"dash://exports/([0-9a-f-]+)")


__all__ = ["APP_OUTPUTS_RESOURCE_RE", "EXPORT_RESOURCE_RE"]
