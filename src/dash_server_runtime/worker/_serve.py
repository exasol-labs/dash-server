"""Serve mode: import the artifact, build Flask, listen on loopback TCP, serve Dash.

The worker emits a single ``{"event":"ready","port":N}`` line on stdout (flushed) so
``AppWorkerManager`` can read the bound port without polling. SIGTERM stops the server.
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path
from typing import Any

from ._app_loader import load_app_module
from ._runtime_services import (
    build_diagnostics_service_for_worker,
    build_exasol_service_for_worker,
)


def serve(args: argparse.Namespace) -> int:
    import signal
    import socket
    from wsgiref.simple_server import make_server, WSGIRequestHandler

    try:
        manifest_data: dict[str, Any]
        if args.manifest_json.startswith("@"):
            manifest_data = json.loads(Path(args.manifest_json[1:]).read_text())
        else:
            manifest_data = json.loads(args.manifest_json)
    except Exception as exc:
        print(json.dumps({"event": "failed", "phase": "manifest_parse", "error": str(exc)}), flush=True)
        return 1

    mount_path = args.mount_path or manifest_data.get("route") or f"/apps/{args.app_name}"
    app_source = Path(args.app_source).resolve()

    try:
        from flask import Flask
    except Exception as exc:
        print(json.dumps({"event": "failed", "phase": "flask_import", "error": str(exc)}), flush=True)
        return 1

    try:
        from dash import Dash
    except Exception as exc:
        print(json.dumps({"event": "failed", "phase": "dash_import", "error": str(exc)}), flush=True)
        return 1

    server = Flask(f"dash_server.worker.{args.app_name}")

    # Wire up Exasol / diagnostics from the GitOps profiles + secret env vars passed at spawn.
    # Both bootstrap helpers degrade gracefully when ``dash_server`` is absent from the env.
    exasol_service, exasol_error = build_exasol_service_for_worker(
        args.gitops_repo_path, args.exasol_secrets_root
    )
    if exasol_service is not None:
        server.extensions["exasol_dashboard_service"] = exasol_service
    elif exasol_error is not None:
        print(
            json.dumps({"event": "warning", "phase": "exasol_bootstrap", "error": exasol_error}),
            flush=True,
        )

    diagnostics_service, diagnostics_error = build_diagnostics_service_for_worker(
        args.diagnostics_root
    )
    if diagnostics_service is not None:
        server.extensions["diagnostics_service"] = diagnostics_service
    elif diagnostics_error is not None:
        print(
            json.dumps(
                {"event": "warning", "phase": "diagnostics_bootstrap", "error": diagnostics_error}
            ),
            flush=True,
        )

    try:
        module = load_app_module(app_source, args.app_name)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "failed",
                    "phase": "import",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            ),
            flush=True,
        )
        return 1

    factory = getattr(module, "create_dash_app", None)
    if not callable(factory):
        print(
            json.dumps(
                {
                    "event": "failed",
                    "phase": "factory_lookup",
                    "error": "app.py must define create_dash_app(server, url_base_pathname, metadata).",
                }
            ),
            flush=True,
        )
        return 1

    metadata = {**manifest_data, "route": mount_path}
    try:
        # Worker serves the app at the root of its own HTTP listener; the proxy translates
        # `/apps/<name>/...` into `/...` before forwarding.
        created = factory(
            server=server,
            url_base_pathname="/",
            metadata=metadata,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "failed",
                    "phase": "factory_call",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            ),
            flush=True,
        )
        return 1

    if not isinstance(created, Dash):
        print(
            json.dumps(
                {
                    "event": "failed",
                    "phase": "factory_result",
                    "error": "create_dash_app must return a dash.Dash instance.",
                }
            ),
            flush=True,
        )
        return 1

    try:
        from dash_server_runtime import apply_hosted_footer, finalize_dash_app_callbacks

        apply_hosted_footer(created, mount_path=mount_path, revision_number=args.revision_number)
        finalize_dash_app_callbacks(created)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "failed",
                    "phase": "runtime_hooks",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            ),
            flush=True,
        )
        return 1

    listen_host = args.listen_host or "127.0.0.1"
    listen_port = int(args.listen_port or 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((listen_host, listen_port))
    bound_host, bound_port = sock.getsockname()
    sock.close()

    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            return  # Worker stdout is reserved for structured event lines.

    httpd = make_server(bound_host, bound_port, server.wsgi_app, handler_class=_QuietHandler)

    def _terminate(_signum, _frame):
        httpd.shutdown()

    try:
        signal.signal(signal.SIGTERM, _terminate)
        signal.signal(signal.SIGINT, _terminate)
    except (ValueError, AttributeError):
        pass

    print(
        json.dumps(
            {
                "event": "ready",
                "port": bound_port,
                "host": bound_host,
                "pid": os.getpid(),
                "mount_path": mount_path,
                "revision_number": args.revision_number,
            }
        ),
        flush=True,
    )

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0
