"""Entry point for ``python -m dash_server_runtime.worker``."""

from __future__ import annotations

import sys

from . import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
