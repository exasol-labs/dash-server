"""Connection validation and runtime access for Exasol profiles."""

from __future__ import annotations

import ssl
import threading
import time
from importlib import import_module
from typing import Any
from collections.abc import Callable

from dash_server.exceptions import DashServerError
from dash_server.exasol._pyexasol_types import ExaConnectionLike
from dash_server.exasol.models import ExasolProfile
from dash_server.exasol.secrets import ExasolSecretStore

# PS27-BUG-001 (round-2 persona study): a cached connection used to live for the
# lifetime of its worker thread, released only via incidental error-triggered
# invalidation - never proactively. Under real concurrent load this exhausted
# Exasol Personal's connection license for 25-30 minutes at a stretch, with no
# correlation between reduced query activity and faster recovery. This default
# bounds how long an unused cached connection can survive before the next
# `connect()` call on that thread evicts and replaces it.
_DEFAULT_IDLE_TIMEOUT_SECONDS = 300


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

        Threads the profile's `query_defaults.statement_timeout_seconds` into the
        new connection (PS26-BUG-001: this value used to be stored and displayed
        but never actually reached `pyexasol.connect(...)` for the runtime/callback
        path, only for one-shot uncached probes). Because pyexasol's `query_timeout`
        is a connect-time setting that applies to every statement executed on that
        session, setting it here at cache-miss time covers every subsequent cached
        query for this profile on this thread.

        PS27-BUG-001: a cached connection idle for longer than
        `query_defaults.connection_idle_timeout_seconds` (default 300s) is evicted
        and replaced here rather than reused, so a long-lived-but-unused connection
        doesn't hold an Exasol license slot indefinitely.
        """

        cache = self._thread_cache()
        cached = cache.get(profile.name)
        if cached is not None:
            connection, last_used = cached
            if time.monotonic() - last_used <= self.connection_idle_timeout_seconds(profile):
                cache[profile.name] = (connection, time.monotonic())
                return connection
            self.invalidate(profile.name)
        connection = self.connect_uncached(
            profile,
            query_timeout_seconds=self.statement_timeout_seconds(profile),
        )
        cache[profile.name] = (connection, time.monotonic())
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
        cached = cache.pop(profile_name, None)
        if cached is None:
            return
        connection, _last_used = cached
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

    @staticmethod
    def statement_timeout_seconds(profile: ExasolProfile) -> int | None:
        """Read `query_defaults.statement_timeout_seconds` off a profile, defensively.

        Public (not prefixed) because both the cached runtime path (`connect`,
        below) and one-shot probe callers (`sql_smoke.run_sql_smoke`) need the
        same coercion instead of each hand-rolling their own `.get()`/`int(...)`.

        Returns `None` (no timeout passed to pyexasol) when the profile has no
        `query_defaults`, no `statement_timeout_seconds` key, or a value that
        doesn't coerce to an int — callers already tolerate `None` here (it's the
        same default `connect_uncached`/`_connect_kwargs` use for probe callers
        that don't want a timeout at all).
        """

        query_defaults = profile.query_defaults or {}
        value = query_defaults.get("statement_timeout_seconds")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def connection_idle_timeout_seconds(profile: ExasolProfile) -> float:
        """How long a cached connection may sit unused before `connect()` evicts it.

        Reads `query_defaults.connection_idle_timeout_seconds` off the profile,
        defensively (same tolerance as `statement_timeout_seconds`: a missing or
        non-coercible value falls back to the server default rather than disabling
        eviction entirely - PS27-BUG-001 was about an *unbounded* lifetime, so an
        unset override should not mean "never evict").
        """

        query_defaults = profile.query_defaults or {}
        value = query_defaults.get("connection_idle_timeout_seconds")
        if value is None:
            return _DEFAULT_IDLE_TIMEOUT_SECONDS
        try:
            return float(value)
        except (TypeError, ValueError):
            return _DEFAULT_IDLE_TIMEOUT_SECONDS

    def _thread_cache(self) -> dict[str, tuple[ExaConnectionLike, float]]:
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
            )
        return kwargs

    def _default_connector_loader(self) -> Any:
        return import_module("pyexasol")
