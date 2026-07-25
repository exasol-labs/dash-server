"""Local cross-module symbol validation for draft workspaces.

Builds the ``cross_module_symbols`` report used by
``WorkspaceService.validate_workspace``. The checks statically validate direct
local module imports and aliased local-module attribute access without
executing any workspace code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

_NOTES = "Validated direct local module imports and aliased local module attribute access."


def default_cross_module_symbol_report(
    *,
    syntax_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    if syntax_errors:
        return {
            "status": "skipped",
            "issues": [],
            "warnings": [],
            "notes": "Skipped local cross-module symbol checks because Python syntax validation failed.",
        }
    return {
        "status": "passed",
        "issues": [],
        "warnings": [],
        "notes": _NOTES,
    }


def cross_module_symbol_report(
    trees: dict[str, ast.AST],
) -> dict[str, Any]:
    module_to_path, path_to_module = _local_module_index(trees.keys())
    module_analysis = {
        path: _analyze_local_module(tree)
        for path, tree in trees.items()
    }
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_issues: set[tuple[str, int | None, str, str]] = set()
    seen_warnings: set[tuple[str, int | None, str]] = set()
    for relative_path, tree in trees.items():
        references, reference_warnings = _collect_cross_module_references(
            tree,
            relative_path=relative_path,
            current_module=path_to_module.get(relative_path),
            module_to_path=module_to_path,
        )
        for warning in reference_warnings:
            warning_key = (
                warning["path"],
                warning.get("line"),
                warning["message"],
            )
            if warning_key not in seen_warnings:
                seen_warnings.add(warning_key)
                warnings.append(warning)
        for reference in references:
            target_info = module_analysis.get(reference["target_path"])
            if target_info is None:
                continue
            symbol = reference["symbol"]
            if symbol in target_info["defined_symbols"]:
                continue
            if target_info["analysis_limited"]:
                warning = {
                    "path": reference["path"],
                    "line": reference["line"],
                    "message": (
                        f"Skipped strict check for {reference['target_module']}.{symbol} because "
                        f"{reference['target_path']} uses conditional or dynamic top-level bindings."
                    ),
                }
                warning_key = (
                    warning["path"],
                    warning.get("line"),
                    warning["message"],
                )
                if warning_key not in seen_warnings:
                    seen_warnings.add(warning_key)
                    warnings.append(warning)
                continue
            issue = {
                "path": reference["path"],
                "line": reference["line"],
                "message": (
                    f"{reference['target_module']}.{symbol} is referenced but not defined in "
                    f"{reference['target_path']}."
                ),
                "symbol": symbol,
                "reference": reference["reference"],
                "target_path": reference["target_path"],
            }
            issue_key = (
                issue["path"],
                issue["line"],
                issue["message"],
                issue["reference"],
            )
            if issue_key not in seen_issues:
                seen_issues.add(issue_key)
                issues.append(issue)
    status = "failed" if issues else "passed_with_warnings" if warnings else "passed"
    return {
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "notes": _NOTES,
    }


def _local_module_index(
    paths: Any,
) -> tuple[dict[str, str], dict[str, str]]:
    module_to_path: dict[str, str] = {}
    path_to_module: dict[str, str] = {}
    for relative_path in sorted(str(path) for path in paths):
        module_name = _module_name_from_path(relative_path)
        if module_name is None:
            continue
        module_to_path[module_name] = relative_path
        path_to_module[relative_path] = module_name
    return module_to_path, path_to_module


def _module_name_from_path(relative_path: str) -> str | None:
    path = Path(relative_path)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _analyze_local_module(tree: ast.AST) -> dict[str, Any]:
    defined_symbols: set[str] = set()
    analysis_limited = False
    control_flow_nodes: tuple[type[ast.AST], ...] = (
        ast.If,
        ast.Try,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
    )
    match_node = getattr(ast, "Match", None)
    if isinstance(match_node, type):
        control_flow_nodes = (*control_flow_nodes, match_node)
    for node in getattr(tree, "body", []):
        if _is_docstring_expr(node) or isinstance(node, ast.Pass):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_symbols.add(node.name)
            if node.name == "__getattr__":
                analysis_limited = True
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                defined_symbols.update(_assignment_target_names(target))
            continue
        if isinstance(node, ast.AnnAssign):
            defined_symbols.update(_assignment_target_names(node.target))
            continue
        if isinstance(node, ast.AugAssign):
            defined_symbols.update(_assignment_target_names(node.target))
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding_name = alias.asname or alias.name.split(".", 1)[0]
                if binding_name:
                    defined_symbols.add(binding_name)
            continue
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                analysis_limited = True
                continue
            for alias in node.names:
                binding_name = alias.asname or alias.name
                if binding_name:
                    defined_symbols.add(binding_name)
            continue
        if isinstance(node, control_flow_nodes):
            analysis_limited = True
    return {
        "defined_symbols": defined_symbols,
        "analysis_limited": analysis_limited,
    }


def _assignment_target_names(target: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
        return names
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            names.update(_assignment_target_names(item))
    return names


def _is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _collect_cross_module_references(
    tree: ast.AST,
    *,
    relative_path: str,
    current_module: str | None,
    module_to_path: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    references: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    import_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "." in alias.name and alias.asname is None and alias.name in module_to_path:
                    warnings.append(
                        {
                            "path": relative_path,
                            "line": getattr(node, "lineno", None),
                            "message": (
                                f"Skipped strict local symbol checks for dotted import {alias.name!r}; "
                                "add an explicit alias to enable module attribute validation."
                            ),
                        }
                    )
                    continue
                target_module = alias.name if alias.name in module_to_path else None
                if target_module is None:
                    continue
                binding_name = alias.asname or alias.name
                import_aliases[binding_name] = target_module
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        resolved_module = _resolve_local_import_module(
            module=node.module,
            level=node.level,
            current_module=current_module,
            current_path=relative_path,
            module_to_path=module_to_path,
        )
        if resolved_module is None:
            continue
        if any(alias.name == "*" for alias in node.names):
            warnings.append(
                {
                    "path": relative_path,
                    "line": getattr(node, "lineno", None),
                    "message": (
                        f"Skipped strict local symbol checks for wildcard import from {resolved_module}."
                    ),
                }
            )
            continue
        if node.module is None:
            warnings.append(
                {
                    "path": relative_path,
                    "line": getattr(node, "lineno", None),
                    "message": (
                        "Skipped strict local symbol checks for relative import without an explicit module name."
                    ),
                }
            )
            continue
        for alias in node.names:
            references.append(
                {
                    "path": relative_path,
                    "line": getattr(node, "lineno", None),
                    "reference": alias.asname or alias.name,
                    "symbol": alias.name,
                    "target_module": resolved_module,
                    "target_path": module_to_path[resolved_module],
                }
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        target_module = import_aliases.get(node.value.id)
        if target_module is None:
            continue
        references.append(
            {
                "path": relative_path,
                "line": getattr(node, "lineno", None),
                "reference": f"{node.value.id}.{node.attr}",
                "symbol": node.attr,
                "target_module": target_module,
                "target_path": module_to_path[target_module],
            }
        )
    return references, warnings


def _resolve_local_import_module(
    *,
    module: str | None,
    level: int,
    current_module: str | None,
    current_path: str,
    module_to_path: dict[str, str],
) -> str | None:
    if level == 0:
        return module if module in module_to_path else None
    if current_module is None:
        return None
    if current_path.endswith("__init__.py"):
        current_package = current_module
    else:
        current_package = current_module.rpartition(".")[0]
    if not current_package:
        return None
    package_parts = current_package.split(".")
    ascents = max(level - 1, 0)
    if ascents > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - ascents] if ascents else package_parts
    if module:
        base_parts = [*base_parts, *module.split(".")]
    candidate = ".".join(part for part in base_parts if part)
    return candidate if candidate in module_to_path else None
