"""Development entrypoint for dash-server."""

from __future__ import annotations

import argparse
import os

from .app_factory import create_app


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _env_port(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    return _port(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the dash-server app.")
    parser.add_argument(
        "--host",
        default=os.environ.get("DASH_SERVER_HOST", "127.0.0.1"),
        help="Control-plane bind host. Env: DASH_SERVER_HOST. Default: 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=_env_port("DASH_SERVER_PORT", 5100),
        help="Control-plane HTTP port. Env: DASH_SERVER_PORT. Default: 5100.",
    )
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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    test_config = {
        "DASH_SERVER_HOST": args.host,
        "DASH_SERVER_PORT": args.port,
    }
    if args.instance_path:
        test_config["INSTANCE_PATH"] = args.instance_path
    app = create_app(test_config)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
