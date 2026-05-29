"""Subprocess validator: imports app.py, calls the factory, emits a single JSON result."""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path
from typing import Any

from ._app_loader import describe_callbacks, load_app_module


def validate(args: argparse.Namespace) -> dict[str, Any]:
    """Run the subprocess-side import smoke check; return a JSON-serializable dict."""

    try:
        manifest_data: dict[str, Any]
        if args.manifest_json.startswith("@"):
            manifest_data = json.loads(Path(args.manifest_json[1:]).read_text())
        else:
            manifest_data = json.loads(args.manifest_json)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"Could not parse manifest JSON: {exc!s}",
            "traceback": None,
        }

    app_source = Path(args.app_source).resolve()
    mount_path = args.mount_path or manifest_data.get("route") or f"/apps/{args.app_name}"

    try:
        module = load_app_module(app_source, args.app_name)
    except Exception as exc:
        return {
            "status": "failed",
            "phase": "import",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }

    factory = getattr(module, "create_dash_app", None)
    if not callable(factory):
        return {
            "status": "failed",
            "phase": "factory_lookup",
            "error": "app.py must define create_dash_app(server, url_base_pathname, metadata).",
            "traceback": None,
        }

    try:
        from dash import Dash
    except Exception as exc:
        return {
            "status": "failed",
            "phase": "dash_import",
            "error": f"Failed to import dash inside the validation env: {exc!s}",
            "traceback": traceback.format_exc(),
        }

    try:
        from flask import Flask
    except Exception as exc:
        return {
            "status": "failed",
            "phase": "flask_import",
            "error": f"Failed to import flask inside the validation env: {exc!s}",
            "traceback": traceback.format_exc(),
        }

    try:
        test_server = Flask(f"dash_server.validate.{args.app_name}")
        metadata = {**manifest_data, "route": mount_path}
        created = factory(
            server=test_server,
            url_base_pathname=f"{mount_path.rstrip('/')}/",
            metadata=metadata,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "phase": "factory_call",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }

    if not isinstance(created, Dash):
        return {
            "status": "failed",
            "phase": "factory_result",
            "error": "create_dash_app must return a dash.Dash instance.",
            "traceback": None,
        }

    try:
        from dash_server_runtime import apply_hosted_footer, finalize_dash_app_callbacks

        apply_hosted_footer(created)
        finalize_dash_app_callbacks(created)
    except Exception as exc:
        return {
            "status": "failed",
            "phase": "runtime_hooks",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }

    callback_report = describe_callbacks(created)
    return {
        "status": "passed",
        "phase": "complete",
        "error": None,
        "traceback": None,
        "callbacks": callback_report,
    }
