"""Shared helpers for both ``--mode=validate`` and ``--mode=serve``.

These functions have no ``dash_server`` dependency — they're safe to import inside a
per-app environment that only has ``dash_server_runtime`` installed.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any


def load_app_module(app_source: Path, app_name: str) -> Any:
    """Import the artifact's ``app.py`` as a fresh module.

    Also ensures the app.py's directory is on ``sys.path`` so sibling modules (e.g. the
    scaffold's ``dash_server_exasol.py``) can be imported via the standard import system.
    """

    if app_source.is_dir():
        app_path = app_source / "app.py"
    else:
        app_path = app_source
    module_name = f"dash_server_worker_{app_name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    parent = str(app_path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(module)
    return module


def describe_callbacks(dash_app: Any) -> dict[str, Any]:
    """Snapshot Dash's callback_map into the shape the validator/runtime emits."""

    callbacks: list[dict[str, Any]] = []
    callback_map = getattr(dash_app, "callback_map", {}) or {}
    layout_ids = collect_layout_ids(dash_app)
    for output_key, spec in callback_map.items():
        inputs = [
            {"id": _dep_id(dep), "property": _dep_property(dep)}
            for dep in spec.get("inputs", []) or []
        ]
        outputs = [
            {"id": _dep_id(dep), "property": _dep_property(dep)}
            for dep in spec.get("output", []) or []
        ]
        state = [
            {"id": _dep_id(dep), "property": _dep_property(dep)}
            for dep in spec.get("state", []) or []
        ]
        referenced_ids = {dep["id"] for dep in inputs + outputs + state}
        missing = sorted(referenced_ids - layout_ids)
        callbacks.append(
            {
                "output_key": str(output_key),
                "inputs": inputs,
                "outputs": outputs,
                "state": state,
                "missing_layout_ids": missing,
            }
        )
    return {
        "callbacks": callbacks,
        "count": len(callbacks),
        "missing_layout_ids": sorted(
            {missing_id for cb in callbacks for missing_id in cb["missing_layout_ids"]}
        ),
        "suppress_callback_exceptions": bool(
            getattr(getattr(dash_app, "config", None), "suppress_callback_exceptions", False)
        ),
        "status": "passed",
    }


def collect_layout_ids(dash_app: Any) -> set[str]:
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if node is None:
            return
        component_id = getattr(node, "id", None)
        if isinstance(component_id, str) and component_id:
            seen.add(component_id)
        children = getattr(node, "children", None)
        if children is None:
            return
        if isinstance(children, (list, tuple)):
            for child in children:
                walk(child)
        else:
            walk(children)

    walk(getattr(dash_app, "layout", None))
    return seen


def _dep_id(dep: Any) -> str:
    return str(getattr(dep, "component_id", getattr(dep, "id", "")))


def _dep_property(dep: Any) -> str:
    return str(getattr(dep, "component_property", getattr(dep, "property", "")))
