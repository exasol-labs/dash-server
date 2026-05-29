"""Out-of-process worker for hosted Dash apps.

This package owns the worker entry point. It's installed into every per-app dependency
environment alongside ``dash_server_runtime``, so the env contract is:

    dash_server_runtime + user requirements

— no ``dash_server`` (the control plane) needed inside the env. Per-app envs that *do*
have ``dash_server`` installed (today's shared-deps default) automatically light up the
optional Exasol / diagnostics bootstrap; per-app envs that don't will still serve Dash.

The CLI surface is:

    python -m dash_server_runtime.worker --mode=validate ...
    python -m dash_server_runtime.worker --mode=serve ...

A thin compat shim lives at ``dash_server.runtime.worker`` so existing callers and the
in-tree ``python -m dash_server.runtime.worker`` invocation continue to work.
"""

from __future__ import annotations

import argparse
import json

from ._serve import serve
from ._validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dash-server out-of-process worker")
    parser.add_argument("--mode", required=True, choices=["validate", "serve"])
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--app-source", required=True, help="Path to app.py or its directory")
    parser.add_argument("--mount-path", default=None)
    parser.add_argument(
        "--manifest-json",
        required=True,
        help="Manifest as inline JSON, or '@path/to/manifest.json' to read from disk",
    )
    parser.add_argument("--revision-number", type=int, default=0)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument(
        "--listen-port-range",
        default=None,
        help="Optional inclusive worker port range as START-END. Ignored when --listen-port is non-zero.",
    )
    parser.add_argument("--gitops-repo-path", default=None)
    parser.add_argument("--exasol-secrets-root", default=None)
    parser.add_argument("--diagnostics-root", default=None)
    args = parser.parse_args(argv)

    if args.mode == "validate":
        result = validate(args)
        print(json.dumps(result), flush=True)
        return 0 if result.get("status") == "passed" else 1
    if args.mode == "serve":
        return serve(args)
    return 1  # pragma: no cover — argparse rejects unknown modes


__all__ = ["main"]
