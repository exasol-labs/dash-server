"""Wire contract shared by the page payload, the blueprint, and the MCP handler.

The browser half of the session channel lives in a static asset
(``dash_apps/assets/session_channel.js``) and therefore cannot import this module.
Everything the two halves must agree on is named here once so a drift guard can
assert the JS still speaks the same protocol (see
``tests/test_session_channel.py::test_js_payload_declares_every_contract_constant``).

Nothing in this module imports Flask or any dash-server service: it is pure
vocabulary, so both the control-plane blueprint and the tests can use it freely.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

#: URL prefix the control-plane blueprint is mounted under. App names match
#: ``^[a-z][a-z0-9-]*$`` (``dash_apps/factory.py``), so a leading double
#: underscore can never collide with an ``/apps/<name>`` or ``/preview/<name>``
#: mount prefix — the dispatcher falls through to the control plane for these.
BLUEPRINT_URL_PREFIX = "/__dash-server/session"

ROUTE_REGISTER = "/register"
ROUTE_POLL = "/poll"
ROUTE_RESULT = "/result"

# ---------------------------------------------------------------------------
# Result-envelope keys
# ---------------------------------------------------------------------------

KEY_SESSION_ID = "session_id"
KEY_COMMAND_ID = "command_id"
KEY_CODE = "code"
KEY_OK = "ok"
KEY_VALUE = "value"
KEY_OUT = "out"
KEY_CONSOLE = "console"
KEY_ERROR = "error"
KEY_TRUNCATED = "truncated"
KEY_DURATION_MS = "duration_ms"
KEY_EVAL_MODE = "eval_mode"
KEY_TIER_USED = "tier_used"

#: How the submitted code was compiled. Reported back so an agent can tell why a
#: trailing expression did or did not become the returned value.
EVAL_MODES = ("expression", "last_line", "statements")

# ---------------------------------------------------------------------------
# Serializer sentinels
# ---------------------------------------------------------------------------
# The JS serializer cannot represent every value as JSON, and silent coercion
# would make an agent draw confidently wrong conclusions (`undefined` becoming
# `null` is the classic). Non-representable values become a tagged object using
# these keys, and every truncation is explicit.

SENTINEL_TYPE = "$dsType"
SENTINEL_LENGTH = "$dsLength"
SENTINEL_ITEMS = "$dsItems"
SENTINEL_TRUNCATED = "$dsTruncated"
SENTINEL_OMITTED_ITEMS = "$dsOmittedItems"
SENTINEL_OMITTED_KEYS = "$dsOmittedKeys"
SENTINEL_OMITTED_CHARS = "$dsOmittedChars"

SENTINEL_KEYS = (
    SENTINEL_TYPE,
    SENTINEL_LENGTH,
    SENTINEL_ITEMS,
    SENTINEL_TRUNCATED,
    SENTINEL_OMITTED_ITEMS,
    SENTINEL_OMITTED_KEYS,
    SENTINEL_OMITTED_CHARS,
)

# ---------------------------------------------------------------------------
# Prop-access tiers
# ---------------------------------------------------------------------------
# `ctx.props()` reports which mechanism actually worked rather than assuming one.
# Tier 3 (React fiber traversal) is unsupported and version-fragile — the project
# pins `dash>=4.3,<5.0`, so renderer internals are a moving target. Degrading to
# a lower tier is reported, never hidden.

TIER_DASH_COMPONENT_API = "dash_component_api"
TIER_REACT_FIBER = "react_fiber"
TIER_DOM = "dom"
TIER_NONE = "none"

PROP_TIERS = (TIER_DASH_COMPONENT_API, TIER_REACT_FIBER, TIER_DOM, TIER_NONE)

# ---------------------------------------------------------------------------
# Error categories (registered in `dash_server.errors`)
# ---------------------------------------------------------------------------

CATEGORY_UNAVAILABLE = "session_channel_unavailable"
CATEGORY_SESSION_GONE = "session_channel_session_gone"
CATEGORY_BUSY = "session_channel_busy"
CATEGORY_TIMEOUT = "session_channel_timeout"
CATEGORY_PROTOCOL = "session_channel_protocol_error"

#: Diagnostics channel every dispatched command is appended to.
DIAGNOSTICS_CHANNEL = "session.commands"

_MOUNT_APPS_RE = re.compile(r"^/apps/([a-z][a-z0-9-]*)(?:/.*)?$")
_MOUNT_PREVIEW_RE = re.compile(r"^/preview/([a-z][a-z0-9-]*)/(?:r0*)?(\d+)(?:/.*)?$")


def app_name_from_mount_path(mount_path: str) -> str | None:
    """Return the app name a mount path belongs to, or ``None`` if unrecognized.

    The page reports only its ``mount_path`` (that is all the worker's
    ``apply_hosted_footer`` call site knows), so the control plane derives the app
    name rather than threading a second value through the runtime hooks.
    """

    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        return None
    normalized = "/" + mount_path.strip("/")
    live = _MOUNT_APPS_RE.match(normalized)
    if live is not None:
        return live.group(1)
    preview = _MOUNT_PREVIEW_RE.match(normalized)
    if preview is not None:
        return preview.group(1)
    return None


def mount_kind_from_mount_path(mount_path: str) -> str:
    """Return ``"live"``, ``"preview"``, or ``"unknown"`` for a mount path."""

    if not isinstance(mount_path, str):
        return "unknown"
    normalized = "/" + mount_path.strip("/")
    if _MOUNT_APPS_RE.match(normalized) is not None:
        return "live"
    if _MOUNT_PREVIEW_RE.match(normalized) is not None:
        return "preview"
    return "unknown"


def truncate_text(text: str, limit: int) -> tuple[str, int]:
    """Clip ``text`` to ``limit`` characters, returning ``(clipped, omitted)``.

    Server-side backstop for the JS caps: the page is expected to bound its own
    payloads, but the control plane does not trust it to.
    """

    if limit <= 0 or len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def is_sentinel(value: Any) -> bool:
    """Whether ``value`` is a serializer sentinel object rather than plain data."""

    return isinstance(value, dict) and SENTINEL_TYPE in value


__all__ = [
    "BLUEPRINT_URL_PREFIX",
    "CATEGORY_BUSY",
    "CATEGORY_PROTOCOL",
    "CATEGORY_SESSION_GONE",
    "CATEGORY_TIMEOUT",
    "CATEGORY_UNAVAILABLE",
    "DIAGNOSTICS_CHANNEL",
    "EVAL_MODES",
    "KEY_CODE",
    "KEY_COMMAND_ID",
    "KEY_CONSOLE",
    "KEY_DURATION_MS",
    "KEY_ERROR",
    "KEY_EVAL_MODE",
    "KEY_OK",
    "KEY_OUT",
    "KEY_SESSION_ID",
    "KEY_TIER_USED",
    "KEY_TRUNCATED",
    "KEY_VALUE",
    "PROP_TIERS",
    "ROUTE_POLL",
    "ROUTE_REGISTER",
    "ROUTE_RESULT",
    "SENTINEL_ITEMS",
    "SENTINEL_KEYS",
    "SENTINEL_LENGTH",
    "SENTINEL_OMITTED_CHARS",
    "SENTINEL_OMITTED_ITEMS",
    "SENTINEL_OMITTED_KEYS",
    "SENTINEL_TRUNCATED",
    "SENTINEL_TYPE",
    "TIER_DASH_COMPONENT_API",
    "TIER_DOM",
    "TIER_NONE",
    "TIER_REACT_FIBER",
    "app_name_from_mount_path",
    "is_sentinel",
    "mount_kind_from_mount_path",
    "truncate_text",
]
