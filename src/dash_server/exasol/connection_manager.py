"""Connection validation and runtime access for Exasol profiles."""

from __future__ import annotations

import ssl
import threading
from importlib import import_module
from typing import Any
from collections.abc import Callable

from dash_server.exceptions import DashServerError
from dash_server.exasol._pyexasol_types import ExaConnectionLike
from dash_server.exasol.models import ExasolProfile
from dash_server.exasol.secrets import ExasolSecretStore


def _classify_connection_error(error_text: str) -> dict[str, str] | None:
    """Return a structured hint for known cryptic pyexasol/Exasol errors."""

    if not error_text:
        return None
    lower = error_text.lower()
    if "only tls connections are allowed" in lower or "08004" in lower:
        return {
            "kind": "tls_required",
            "hint": (
                "Exasol rejected the connection because TLS was not negotiated. "
                "This is a server-side dash-server configuration issue, not a "
                "credential problem. Make sure the profile is built with "
                "encryption=True (the default in dash-server >= 0.6)."
            ),
        }
    if "certificate verify failed" in lower or "self-signed certificate" in lower:
        return {
            "kind": "tls_cert_verify_failed",
            "hint": (
                "TLS certificate verification failed. If the Exasol instance "
                "uses a self-signed certificate (Nano, Exasol Community Edition, "
                "or a corporate dev cluster) set tls_verify=false on the profile."
            ),
        }
    return None


class ExasolConnectionManager:
    """Create Exasol connections from stored profile metadata.

    Maintains a per-thread cache of open pyexasol sessions keyed by profile name.
    Opening a TLS session against Exasol costs ~400-500ms locally and most of every
    callback-time query was that handshake. With the cache, every subsequent query
    on the same request thread reuses the live session and pays only the actual
    statement cost (~30-50ms for a small dataset).

    Threading: pyexasol's `ExaConnection` is not thread-safe, so the cache is a
    `threading.local`. Flask's default WSGI dev server is multi-threaded
    (`threaded=True`); each worker thread gets its own connection per profile.
    """

    def __init__(
        self,
        secret_store: ExasolSecretStore,
        *,
        connector_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.secret_store = secret_store
        self.connector_loader = connector_loader or self._default_connector_loader
        self._local = threading.local()

    def validate_profile(self, profile: ExasolProfile) -> dict[str, Any]:
        secret_resolution = {
            "status": "failed",
            "provider": profile.secret_ref.provider,
            "key": profile.secret_ref.key,
        }
        try:
            secret_value = self.secret_store.resolve(profile.secret_ref)
            secret_resolution["status"] = "resolved"
        except DashServerError as exc:
            return {
                "profile": profile.to_dict(),
                "secret_resolution": {
                    **secret_resolution,
                    "error": exc.summary,
                },
                "driver": {"status": "unknown"},
                "connection_test": {"status": "skipped"},
                "is_valid": False,
            }

        try:
            connector = self.connector_loader()
        except Exception as exc:
            return {
                "profile": profile.to_dict(),
                "secret_resolution": secret_resolution,
                "driver": {
                    "status": "missing",
                    "module": "pyexasol",
                    "error": str(exc),
                },
                "connection_test": {"status": "skipped"},
                "is_valid": False,
            }

        connect_kwargs = self._connect_kwargs(profile, secret_value)
        connection_test = {"status": "failed"}
        try:
            connection = connector.connect(**connect_kwargs)
            close_fn = getattr(connection, "close", None)
            if callable(close_fn):
                close_fn()
            connection_test = {"status": "succeeded"}
        except Exception as exc:
            error_text = str(exc)
            connection_test = {
                "status": "failed",
                "error": error_text,
            }
            classification = _classify_connection_error(error_text)
            if classification is not None:
                connection_test["error_class"] = classification["kind"]
                connection_test["hint"] = classification["hint"]

        return {
            "profile": profile.to_dict(),
            "secret_resolution": secret_resolution,
            "driver": {"status": "available", "module": "pyexasol"},
            "connection_test": connection_test,
            "is_valid": connection_test["status"] == "succeeded",
        }

    def connect(self, profile: ExasolProfile) -> ExaConnectionLike:
        """Return a per-thread cached connection, opening a new one on first use.

        Callers should NOT call `.close()` on the returned connection — the manager
        owns the lifecycle. To force a reconnect (e.g. after a network blip), call
        `invalidate(profile.name)` and the next `connect()` will rebuild.
        """

        cache = self._thread_cache()
        cached = cache.get(profile.name)
        if cached is not None:
            return cached
        connection = self.connect_uncached(profile)
        cache[profile.name] = connection
        return connection

    def connect_uncached(
        self,
        profile: ExasolProfile,
        *,
        query_timeout_seconds: int | None = None,
    ) -> ExaConnectionLike:
        """Open a caller-owned connection without storing it in the thread cache.

        Use this for one-shot probes such as SQL smoke preflight where the caller
        intentionally closes the connection. Runtime callbacks should keep using
        `connect()` so they benefit from per-thread session reuse.
        """

        secret_value = self.secret_store.resolve(profile.secret_ref)
        connector = self.connector_loader()
        return connector.connect(
            **self._connect_kwargs(
                profile,
                secret_value,
                query_timeout_seconds=query_timeout_seconds,
            )
        )

    def invalidate(self, profile_name: str) -> None:
        """Discard the cached connection for ``profile_name`` on this thread.

        Used by `execute_profile_query` when a connection-level error suggests the
        session is dead. The next `connect()` rebuilds; the caller retries.
        """

        cache = self._thread_cache()
        connection = cache.pop(profile_name, None)
        if connection is None:
            return
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close_all_for_thread(self) -> None:
        """Close every cached connection on this thread. Safe to call repeatedly."""

        cache = self._thread_cache()
        for name in list(cache.keys()):
            self.invalidate(name)

    def _thread_cache(self) -> dict[str, ExaConnectionLike]:
        cache = getattr(self._local, "connections", None)
        if cache is None:
            cache = {}
            self._local.connections = cache
        return cache

    def _connect_kwargs(
        self,
        profile: ExasolProfile,
        secret_value: str,
        *,
        query_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        # local_direct Exasol always uses TLS; tls_verify controls only certificate
        # validation. Mapping tls_verify directly onto pyexasol's encryption flag
        # (the pre-0.6 behavior) made self-signed deployments unreachable.
        kwargs: dict[str, Any] = {
            "dsn": profile.dsn,
            "user": profile.user,
            "encryption": True,
            "websocket_sslopt": {
                "cert_reqs": ssl.CERT_REQUIRED if profile.tls_verify else ssl.CERT_NONE,
            },
        }
        if query_timeout_seconds is not None:
            kwargs["query_timeout"] = query_timeout_seconds
        if profile.credential_mode in {"password", "saas_pat"}:
            kwargs["password"] = secret_value
        elif profile.credential_mode == "access_token":
            kwargs["access_token"] = secret_value
        elif profile.credential_mode == "refresh_token":
            kwargs["refresh_token"] = secret_value
        else:
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary=f"Unsupported Exasol credential mode {profile.credential_mode}.",
                details={"credential_mode": profile.credential_mode},
                jsonrpc_code=-32602,
            )
        return kwargs

    def _default_connector_loader(self) -> Any:
        return import_module("pyexasol")
