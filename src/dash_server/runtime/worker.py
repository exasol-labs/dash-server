"""Compat shim for the worker entry point.

Phase 3.5a relocated the worker into ``dash_server_runtime.worker`` so per-app envs
only need the small helper package — not the full ``dash_server`` control plane —
installed in their site-packages.

This shim preserves two things:

1. Existing callers that do ``from dash_server.runtime.worker import main`` continue
   to work without code changes.
2. ``python -m dash_server.runtime.worker --mode=...`` still resolves to the same
   ``main`` and behaves identically.

New code should use ``dash_server_runtime.worker`` directly. The
``AppWorkerManager`` already spawns the new path; this shim only exists for
in-tree tests and any third-party scripts that picked up the original name.
"""

from __future__ import annotations

import sys

from dash_server_runtime.worker import main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


__all__ = ["main"]
