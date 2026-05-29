"""Development entrypoint for dash-server."""

from __future__ import annotations

import argparse

from .app_factory import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the dash-server app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--instance-path",
        default=None,
        help=(
            "Override the instance directory (where the SQLite registry, GitOps repo, "
            "artifacts, workspaces, diagnostics, and secrets all live). "
            "Default: <project_root>/instance. "
            "Also settable via DASH_SERVER_INSTANCE_PATH env var."
        ),
    )
    args = parser.parse_args()

    test_config = {"INSTANCE_PATH": args.instance_path} if args.instance_path else None
    app = create_app(test_config)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
