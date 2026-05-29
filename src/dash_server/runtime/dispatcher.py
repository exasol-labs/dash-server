"""Mutable path-prefix dispatcher for hosted app WSGI apps."""

from __future__ import annotations

import re
from threading import RLock
from typing import Any
from collections.abc import Callable, Iterable

# PEP 3333 calling convention. The return type is `Iterable[bytes]` (not
# `list[bytes]`) so generator-based and Flask-app WSGI returns both type-check
# without runtime conversion.
WSGIEnviron = dict[str, Any]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[WSGIEnviron, StartResponse], Iterable[bytes]]
AuthorizationHandler = Callable[
    [WSGIEnviron, str],
    tuple[str, list[tuple[str, str]], bytes] | None,
]

# Accept "/preview/<name>/r000007/..." as an alias for "/preview/<name>/7/...".
# The runtime mounts under the bare-number form, but several tool descriptions
# and revision identifiers use the r000NNN form, so users naturally type either.
_PREVIEW_REVISION_ALIAS_RE = re.compile(r"^(/preview/[a-z0-9-]+/)r0*(\d+)(/.*)?$")


UnmountObserver = Callable[[str], None]


class DynamicPrefixDispatcher:
    """Dispatch requests to mounted WSGI apps by path prefix."""

    def __init__(self, fallback_app: WSGIApp) -> None:
        self._fallback_app = fallback_app
        self._mounts: dict[str, WSGIApp] = {}
        self._authorization_handler: AuthorizationHandler | None = None
        self._lock = RLock()
        # Explicit observer chain so other layers (worker manager, future env GC) can
        # react to unmounts without monkey-patching `unmount`. See plan §3.5e.
        self._unmount_observers: list[UnmountObserver] = []

    def set_authorization_handler(self, handler: AuthorizationHandler | None) -> None:
        with self._lock:
            self._authorization_handler = handler

    def on_unmount(self, callback: UnmountObserver) -> None:
        """Register a callback invoked after a successful unmount.

        Callbacks run outside the dispatcher lock and outside the mount/unmount critical
        section so they can perform their own teardown without deadlocking. Failures are
        swallowed — teardown is best-effort and never blocks the dispatcher.
        """

        with self._lock:
            self._unmount_observers.append(callback)

    def mount(self, prefix: str, app: WSGIApp) -> None:
        normalized = self._normalize_prefix(prefix)
        with self._lock:
            self._mounts[normalized] = app

    def unmount(self, prefix: str) -> None:
        normalized = self._normalize_prefix(prefix)
        with self._lock:
            removed = self._mounts.pop(normalized, None) is not None
            observers = list(self._unmount_observers) if removed else []
        for callback in observers:
            try:
                callback(normalized)
            except Exception:
                # Observer failures are operational, not control-flow.
                pass

    def is_mounted(self, prefix: str) -> bool:
        normalized = self._normalize_prefix(prefix)
        with self._lock:
            return normalized in self._mounts

    def __call__(self, environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        path_info = environ.get("PATH_INFO") or "/"
        rewritten = _PREVIEW_REVISION_ALIAS_RE.match(path_info)
        if rewritten:
            path_info = f"{rewritten.group(1)}{rewritten.group(2)}{rewritten.group(3) or ''}"
            environ = environ.copy()
            environ["PATH_INFO"] = path_info
        with self._lock:
            mounts = sorted(self._mounts.items(), key=lambda item: len(item[0]), reverse=True)
            authorization_handler = self._authorization_handler

        for prefix, app in mounts:
            if path_info == prefix or path_info.startswith(f"{prefix}/"):
                if authorization_handler is not None:
                    denied = authorization_handler(environ, prefix)
                    if denied is not None:
                        status, headers, body = denied
                        start_response(status, headers)
                        return [body]
                adjusted_environ = environ.copy()
                adjusted_environ["SCRIPT_NAME"] = f"{environ.get('SCRIPT_NAME', '')}{prefix}"
                remainder = path_info[len(prefix) :] or "/"
                adjusted_environ["PATH_INFO"] = remainder
                return app(adjusted_environ, start_response)

        return self._fallback_app(environ, start_response)

    def _normalize_prefix(self, prefix: str) -> str:
        if not prefix.startswith("/"):
            raise ValueError("Mount prefixes must start with '/'.")
        normalized = prefix.rstrip("/")
        return normalized or "/"
