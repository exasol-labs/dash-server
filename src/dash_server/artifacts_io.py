"""Shared artifact-directory primitives.

An "artifact" is a materialized app directory (``app.py``, ``dash-app.json``,
``requirements.txt``, plus whatever the app ships). Several subsystems walk
such a directory to copy, hash, or list its source files, and each used to
re-implement the same "skip ``__pycache__`` and ``.pyc``" filter with subtle
differences. The canonical filenames and the walk live here so there is one
definition of "the source files of an artifact".
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

# Canonical filenames every artifact/workspace uses.
APP_MANIFEST_FILENAME = "dash-app.json"
APP_ENTRYPOINT_FILENAME = "app.py"
REQUIREMENTS_FILENAME = "requirements.txt"


def is_artifact_source_part(relative: Path) -> bool:
    """Whether a path relative to an artifact root is a real source file (not build cruft)."""

    # PS27-BUG-003: an app's own import-time code can create arbitrary binary files in
    # its workspace as a side effect (e.g. a diskcache.Cache() sqlite file, needed for
    # the background-callback pattern) - these used to fall through this filter and
    # then crash `read_all_files()` with an unhandled UnicodeDecodeError trying to read
    # them as UTF-8 text. No hosted-app source file is legitimately dot-prefixed, so
    # excluding every hidden file/directory (the conventional home for exactly this
    # class of generated cache/tool cruft) covers this - and any other hidden
    # tool-generated artifact - in one general rule rather than an enumerated list.
    if any(part.startswith(".") for part in relative.parts):
        return False
    return "__pycache__" not in relative.parts and relative.suffix != ".pyc"


def iter_artifact_files(root: Path) -> Iterator[Path]:
    """Yield the source files under ``root`` in sorted order, skipping build cruft."""

    for source in sorted(root.rglob("*")):
        if not source.is_file():
            continue
        if is_artifact_source_part(source.relative_to(root)):
            yield source


def read_artifact_files(root: Path) -> dict[str, str]:
    """Map each artifact source file's POSIX-relative path to its text contents."""

    return {
        source.relative_to(root).as_posix(): source.read_text()
        for source in iter_artifact_files(root)
    }


def list_artifact_files(root: Path) -> list[str]:
    """Return the POSIX-relative paths of an artifact's source files, sorted."""

    return [source.relative_to(root).as_posix() for source in iter_artifact_files(root)]


def load_manifest_from_dir(root: Path) -> dict[str, Any] | None:
    """Read and JSON-decode ``dash-app.json`` from ``root``; ``None`` when absent."""

    manifest_path = root / APP_MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


__all__ = [
    "APP_ENTRYPOINT_FILENAME",
    "APP_MANIFEST_FILENAME",
    "REQUIREMENTS_FILENAME",
    "is_artifact_source_part",
    "iter_artifact_files",
    "list_artifact_files",
    "load_manifest_from_dir",
    "read_artifact_files",
]
