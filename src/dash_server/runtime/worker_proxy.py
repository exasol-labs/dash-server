"""WSGI proxy that forwards requests to a worker's loopback HTTP listener.

Mounted via the existing ``DynamicPrefixDispatcher.mount(prefix, wsgi_app)`` path
so the dispatcher itself doesn't need to know workers exist.

Two implementation choices worth highlighting:

* The proxy uses stdlib ``http.client`` instead of ``requests`` to keep the
  dependency footprint small. One short-lived connection per request is fine
  for v1; HTTP/1.1 keep-alive pools live behind a per-worker cache key in a
  follow-up.
* On worker absence (manager returns ``None`` from ``ensure_running``) the
  proxy returns a structured 502 body with a single ``X-Dash-Server-Worker-Error``
  header so callers can tell worker errors apart from app-side errors.
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from collections.abc import Iterable

from .dispatcher import StartResponse, WSGIEnviron
from .worker_manager import AppWorkerManager, WorkerRecord

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class WorkerProxyWSGIApp:
    """WSGI app that forwards every request to a per-mount worker process."""

    def __init__(
        self,
        manager: AppWorkerManager,
        *,
        mount_path: str,
        app_name: str,
    ) -> None:
        self.manager = manager
        self.mount_path = mount_path
        self.app_name = app_name

    def __call__(self, environ: WSGIEnviron, start_response: StartResponse) -> Iterable[bytes]:
        # ensure_running re-spawns from the persisted spec when the worker is stopped_idle
        # or crashed; it only returns None when no spec exists or re-spawn timed out.
        record = self.manager.ensure_running(self.mount_path)
        if record is None:
            return self._error(
                start_response,
                status="503 Service Unavailable",
                payload={
                    "error": "worker_not_running",
                    "app": self.app_name,
                    "mount_path": self.mount_path,
                    "message": (
                        "No worker process is available for this mount and no persisted "
                        "spec could be re-spawned. The control plane will attempt to "
                        "restart on the next deploy or app_start call."
                    ),
                },
            )

        try:
            return self._forward(environ, start_response, record)
        except (OSError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError) as exc:
            return self._error(
                start_response,
                status="502 Bad Gateway",
                payload={
                    "error": "worker_connection_failed",
                    "app": self.app_name,
                    "mount_path": self.mount_path,
                    "message": f"Could not reach worker at {record.host}:{record.port}: {exc!s}",
                    "pid": record.pid,
                },
            )
        except Exception as exc:
            return self._error(
                start_response,
                status="502 Bad Gateway",
                payload={
                    "error": "worker_proxy_internal_error",
                    "app": self.app_name,
                    "mount_path": self.mount_path,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )

    # ------------------------------------------------------------------ proxy

    def _forward(
        self,
        environ: WSGIEnviron,
        start_response: StartResponse,
        record: WorkerRecord,
    ) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path_info = environ.get("PATH_INFO") or "/"
        query_string = environ.get("QUERY_STRING") or ""
        # Dispatcher already adjusted PATH_INFO to be relative to the mount prefix.
        target_path = path_info if path_info.startswith("/") else f"/{path_info}"
        if query_string:
            target_path = f"{target_path}?{query_string}"

        headers = self._collect_request_headers(environ)
        body = self._read_request_body(environ)

        connection = http.client.HTTPConnection(record.host, record.port, timeout=60)
        try:
            connection.request(method, target_path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        finally:
            connection.close()

        status = f"{response.status} {response.reason or ''}".strip()
        response_headers: list[tuple[str, str]] = []
        for header, value in response.getheaders():
            if header.lower() in _HOP_BY_HOP_HEADERS:
                continue
            response_headers.append((header, value))
        # Always overwrite Content-Length so chunked responses round-trip cleanly.
        if not any(h.lower() == "content-length" for h, _ in response_headers):
            response_headers.append(("Content-Length", str(len(response_body))))

        self.manager.touch_last_request(self.mount_path)
        # Surface the worker's HTTP status to the manager so the `worker_http` probe
        # can read it without a separate roundtrip.
        self.manager.set_last_response_status(self.mount_path, response.status)
        start_response(status, response_headers)
        return [response_body]

    # ------------------------------------------------------------------ helpers

    def _collect_request_headers(self, environ: WSGIEnviron) -> dict[str, str]:
        headers: dict[str, str] = {}
        if environ.get("CONTENT_TYPE"):
            headers["Content-Type"] = environ["CONTENT_TYPE"]
        if environ.get("CONTENT_LENGTH"):
            headers["Content-Length"] = environ["CONTENT_LENGTH"]
        for key, value in environ.items():
            if not key.startswith("HTTP_"):
                continue
            header_name = key[5:].replace("_", "-").title()
            if header_name.lower() in _HOP_BY_HOP_HEADERS:
                continue
            if header_name.lower() == "host":
                # Replace with the worker's host:port so the worker sees a stable Host header.
                continue
            headers[header_name] = value
        return headers

    def _read_request_body(self, environ: WSGIEnviron) -> bytes:
        content_length_raw = environ.get("CONTENT_LENGTH") or "0"
        try:
            length = int(content_length_raw)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        stream = environ.get("wsgi.input")
        if stream is None:
            return b""
        data = stream.read(length)
        return data if isinstance(data, bytes) else b""

    def _error(
        self,
        start_response: StartResponse,
        *,
        status: str,
        payload: dict[str, Any],
    ) -> Iterable[bytes]:
        body = json.dumps(payload).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("X-Dash-Server-Worker-Error", payload.get("error", "worker_error")),
            ],
        )
        return [body]
