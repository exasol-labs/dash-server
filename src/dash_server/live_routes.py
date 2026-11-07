"""The path prefix under which live dashboards are mounted.

Live dashboards are served at ``<prefix><name>`` (``/apps/<name>`` by default); previews
stay at ``/preview/<name>/<revision>``. The prefix is set once per server from
``DASH_SERVER_LIVE_PATH_PREFIX`` (config key ``LIVE_PATH_PREFIX``) by ``create_app``,
and isolated workers inherit it through the same environment variable.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
import os
import re
from typing import Any, TypeVar

_T = TypeVar("_T")

DEFAULT_LIVE_PATH_PREFIX = "/apps/"
LIVE_PATH_PREFIX_ENV_VAR = "DASH_SERVER_LIVE_PATH_PREFIX"

# One lowercase path segment, so the prefix can never shadow another route of the server.
_PREFIX_PATTERN = re.compile(r"^/[a-z][a-z0-9_-]*/$")
_RESERVED_PREFIXES = frozenset(
    {
        "/preview/",
        "/mcp/",
        "/auth/",
        "/manage/",
        "/static/",
        "/assets/",
        "/exports/",
        "/downloads/",
        "/invite/",
        "/share/",
    }
)
# `/apps/` as a path, not inside `dash://apps/...` resource URIs or `/manage/apps/...` routes.
_DEFAULT_PREFIX_IN_TEXT = re.compile(r"(?<![\w/])/apps/")


def normalize_live_path_prefix(value: str | None) -> str:
    """Return ``value`` as ``/<segment>/``, or raise ``ValueError`` for an unusable prefix."""

    if value is None or not value.strip():
        return DEFAULT_LIVE_PATH_PREFIX
    prefix = "/" + value.strip().strip("/") + "/"
    if not _PREFIX_PATTERN.match(prefix):
        raise ValueError(
            f"{LIVE_PATH_PREFIX_ENV_VAR} must be one lowercase path segment such as /apps/ or /dashboards/, got {value!r}."
        )
    if prefix in _RESERVED_PREFIXES:
        raise ValueError(f"{LIVE_PATH_PREFIX_ENV_VAR} {prefix} is a route of dash-server itself.")
    return prefix


_live_path_prefix = normalize_live_path_prefix(os.environ.get(LIVE_PATH_PREFIX_ENV_VAR))


def configure_live_path_prefix(value: str | None) -> str:
    """Set the prefix for this process and for the workers it starts; return it normalized."""

    global _live_path_prefix
    _live_path_prefix = normalize_live_path_prefix(value)
    os.environ[LIVE_PATH_PREFIX_ENV_VAR] = _live_path_prefix
    return _live_path_prefix


def live_path_prefix() -> str:
    """The configured prefix, with leading and trailing slash, e.g. ``/apps/``."""

    return _live_path_prefix


def default_live_route(app_name: str) -> str:
    """The live route of an app that names none: ``<prefix><app_name>``."""

    return f"{_live_path_prefix}{app_name}"


def is_live_route(route: Any) -> bool:
    """Whether ``route`` is a path under the live prefix."""

    return isinstance(route, str) and route.startswith(_live_path_prefix)


def localize_live_paths(value: Any) -> Any:
    """Rewrite the default ``/apps/`` in tool and help text to the configured prefix.

    Strings, lists, and dicts are rewritten recursively; ``dash://apps/...`` resource URIs and
    ``/manage/apps/...`` routes are left alone.
    """

    if _live_path_prefix == DEFAULT_LIVE_PATH_PREFIX:
        return value
    if isinstance(value, str):
        return _DEFAULT_PREFIX_IN_TEXT.sub(_live_path_prefix, value)
    if isinstance(value, list):
        return [localize_live_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: localize_live_paths(item) for key, item in value.items()}
    return value


def localized(function: Callable[[], _T]) -> Callable[[], _T]:
    """Decorate a static help payload builder so its ``/apps/`` paths follow the prefix."""

    @functools.wraps(function)
    def wrapper() -> _T:
        return localize_live_paths(function())

    return wrapper
