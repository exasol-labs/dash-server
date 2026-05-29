"""Helpers for importing hosted app modules from isolated local directories."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from collections.abc import Iterator


def _module_name_from_path(relative_path: Path) -> str | None:
    if relative_path.suffix != ".py":
        return None
    parts = list(relative_path.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def local_module_names(root: Path) -> set[str]:
    """Return dotted module names for Python files rooted at the given directory."""

    names: set[str] = set()
    for candidate in sorted(root.rglob("*.py")):
        if not candidate.is_file():
            continue
        module_name = _module_name_from_path(candidate.relative_to(root))
        if module_name is not None:
            names.add(module_name)
    return names


@contextmanager
def isolated_local_imports(root: Path) -> Iterator[None]:
    """Temporarily import local modules from root without leaking module cache state."""

    resolved_root = root.resolve()
    module_names = local_module_names(resolved_root)
    original_modules = {
        name: sys.modules[name]
        for name in module_names
        if name in sys.modules
    }
    for name in module_names:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(resolved_root))
    try:
        yield
    finally:
        try:
            sys.path.remove(str(resolved_root))
        except ValueError:
            pass
        for name in module_names:
            sys.modules.pop(name, None)
        sys.modules.update(original_modules)
