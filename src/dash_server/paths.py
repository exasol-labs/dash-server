"""Shared path-containment primitives.

Every place the server turns an externally supplied relative path into a
filesystem location must agree on what "inside the root" means. Before this
module existed there were eight subtly different implementations; the weakest
(a purely lexical check on the agent-facing workspace write path) never
resolved symlinks at all.

`safe_relative_path` is the lexical half: normalize separators and reject
absolute paths and traversal parts. `safe_join` is the filesystem half: apply
the lexical check, resolve, and require real containment in the resolved root
so symlink escapes fail too. Both raise ``ValueError``; callers wrap that in
their own domain error.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def safe_relative_path(value: str) -> str:
    """Validate and normalize a workspace-relative POSIX path string."""

    normalized = str(value or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ValueError(f"Path {value!r} is not a normalized relative path.")
    return str(path)


def safe_join(root: Path, relative: str) -> Path:
    """Join ``relative`` onto ``root`` with lexical and resolved containment checks."""

    resolved_root = root.resolve()
    candidate = (resolved_root / safe_relative_path(relative)).resolve()
    if resolved_root not in candidate.parents and candidate != resolved_root:
        raise ValueError(f"Path {relative!r} escapes {root}.")
    return candidate


__all__ = ["safe_join", "safe_relative_path"]
