"""Browser session channel: run ephemeral JavaScript in a live dashboard tab.

Dash keeps interaction state (selections, `dcc.Store` contents, what is actually
visible) in the browser, not on the server, so no amount of server-side
introspection can answer "what is the user looking at right now". This package is
the other half of that: the hosted chrome injected into every dashboard polls the
control plane, evaluates a command in the page, and posts a bounded result back.

See ``plans/live-dashboard-introspection-plan.md``. Local mode only — the gate is
enforced at injection time (`apply_hosted_footer`), route time (the blueprint), and
tool time (the MCP handler).
"""

from __future__ import annotations

from .blueprint import create_session_channel_blueprint
from .service import SessionChannelService

__all__ = ["SessionChannelService", "create_session_channel_blueprint"]
