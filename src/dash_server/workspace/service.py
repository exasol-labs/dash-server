"""Filesystem-backed draft workspace management."""

from __future__ import annotations

import ast
import difflib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from dash import Dash
from flask import Flask
from packaging.requirements import InvalidRequirement, Requirement

from dash_server.dash_apps.callback_isolation import (
    finalize_dash_app_callbacks,
    isolated_dash_callback_globals,
)
from dash_server.dash_apps.branding import apply_hosted_footer
from dash_server.dash_apps.runtime_checks import verify_dash_mount
from dash_server.dash_apps.factory import (
    canonicalize_files_bundle,
    is_files_bundle_shape,
    validate_bundle,
    validate_manifest_payload,
)
from dash_server.exceptions import DashServerError
from dash_server.gitops import GitWorktreeService
from dash_server.imports import isolated_local_imports
from dash_server.registry.models import AppManifest


class WorkspaceService:
    """Manage editable draft workspaces for hosted apps."""

    _state_filename = ".draft-state.json"
    _manifest_filename = "dash-app.json"
    _app_filename = "app.py"
    _requirements_filename = "requirements.txt"
    _patch_preview_context_lines = 3
    _exasol_env_pattern = re.compile(
        r"\b(?:EXA|EXASOL)_(?:DSN|USER|PASS|PASSWORD|PAT|ACCESS_TOKEN|REFRESH_TOKEN)\b"
    )

    def __init__(
        self,
        workspaces_root: str,
        *,
        dependency_installer: Any | None = None,
        git_worktree_service: GitWorktreeService | None = None,
    ) -> None:
        self.workspaces_root = Path(workspaces_root)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.dependency_installer = dependency_installer
        self.git_worktree_service = git_worktree_service

    def workspace_exists(self, app_name: str) -> bool:
        """Return whether a workspace exists for the app."""

        return self._workspace_dir(app_name, create=False) is not None

    def workspace_location(self, app_name: str) -> dict[str, Any]:
        """Return the active workspace storage metadata for the app."""

        workspace_dir = self._workspace_dir(app_name, create=False)
        if workspace_dir is None:
            return {
                "storage_backend": (
                    "git_worktree"
                    if self.git_worktree_service is not None and self.git_worktree_service.can_use_worktrees()
                    else "filesystem"
                ),
                "workspace_path": str(self._legacy_workspace_dir(app_name)),
            }
        return {
            "storage_backend": (
                "git_worktree"
                if self._is_git_workspace_path(app_name, workspace_dir)
                else "filesystem"
            ),
            "workspace_path": str(workspace_dir),
        }

    def ensure_workspace_backend(self, app_name: str) -> dict[str, Any]:
        """Ensure the workspace uses the configured backend, migrating legacy storage when needed."""

        self._workspace_dir(app_name, create=False, migrate_legacy=True)
        return self.workspace_location(app_name)

    def ensure_workspace_from_bundle(self, bundle: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
        manifest, dashboard = validate_bundle(bundle)
        existing_workspace = self._workspace_dir(manifest.name, create=False)
        existed = existing_workspace is not None and any(existing_workspace.iterdir())
        workspace_dir = self._workspace_dir(manifest.name, create=True)
        assert workspace_dir is not None
        workspace_dir.mkdir(parents=True, exist_ok=True)
        current_state = self._read_state(manifest.name)
        current_candidate = int(current_state.get("candidate_version", 1))
        files = self._workspace_files_from_bundle(manifest, dashboard)
        for relative_path, content in files.items():
            file_path = workspace_dir / relative_path
            if overwrite or not file_path.exists():
                file_path.write_text(content)
        if overwrite and existed:
            current_candidate += 1
        self._write_state(manifest.name, {"candidate_version": current_candidate})
        return self.draft_summary(manifest.name)

    def ensure_workspace_from_files_bundle(
        self, bundle: dict[str, Any], *, overwrite: bool = False
    ) -> tuple[AppManifest, dict[str, Any]]:
        if not is_files_bundle_shape(bundle):
            raise DashServerError(
                category="bundle_validation_error",
                summary="Expected a files-based bundle shape.",
                details={"field": "bundle"},
                jsonrpc_code=-32602,
            )

        canonical_bundle, files = canonicalize_files_bundle(bundle)
        manifest, _ = validate_bundle(canonical_bundle)
        self.ensure_workspace_from_bundle(canonical_bundle, overwrite=overwrite)
        self._write_files(manifest.name, files)
        return manifest, self.draft_summary(manifest.name)

    def list_files(self, app_name: str) -> list[str]:
        self._require_workspace(app_name)
        workspace_dir = self._workspace_dir(app_name, create=False)
        assert workspace_dir is not None
        files: list[str] = []
        for path in sorted(workspace_dir.rglob("*")):
            if path.is_file() and self._is_editable_file(path, workspace_dir):
                files.append(path.relative_to(workspace_dir).as_posix())
        return files

    def read_file(self, app_name: str, relative_path: str) -> str:
        file_path = self._resolve_path(app_name, relative_path)
        if not file_path.exists() or not file_path.is_file():
            raise DashServerError(
                category="workspace_file_not_found",
                summary=f"Workspace file {relative_path} was not found.",
                details={"app": app_name, "path": relative_path},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return file_path.read_text()

    def put_files(self, app_name: str, files: list[dict[str, Any]]) -> dict[str, Any]:
        touched = self._write_files(app_name, files)
        candidate = self._bump_candidate(app_name)
        return {"candidate_version": candidate, "touched_files": touched}

    def patch_file(
        self,
        app_name: str,
        relative_path: str,
        search: str,
        replace: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        content = self.read_file(app_name, relative_path)
        matches = self._patch_matches(content, search)
        occurrences = len(matches)
        if occurrences == 0:
            raise DashServerError(
                category="patch_error",
                summary="Search text was not found in the target file.",
                details={"app": app_name, "path": relative_path},
                jsonrpc_code=-32006,
                http_status=409,
            )
        if occurrences > 1 and not replace_all:
            raise DashServerError(
                category="patch_error",
                summary="Search text matched multiple locations; set replace_all to true to continue.",
                details={"app": app_name, "path": relative_path, "occurrences": occurrences},
                jsonrpc_code=-32006,
                http_status=409,
            )
        selected_matches = matches if replace_all else matches[:1]
        updated, replacement_ranges = self._apply_patch_replacements(
            content,
            replace,
            selected_matches,
        )
        self._resolve_path(app_name, relative_path).write_text(updated)
        candidate = self._bump_candidate(app_name)
        return {
            "candidate_version": candidate,
            "path": relative_path,
            "replacements": len(selected_matches),
            **self._patch_verification_payload(updated, replace, replacement_ranges),
        }

    def delete_file(self, app_name: str, relative_path: str) -> dict[str, Any]:
        if relative_path in {
            self._manifest_filename,
            self._app_filename,
            self._requirements_filename,
        }:
            raise DashServerError(
                category="workspace_constraint_error",
                summary=f"{relative_path} is required for hosted app workspaces and cannot be deleted.",
                details={"app": app_name, "path": relative_path},
                jsonrpc_code=-32006,
                http_status=409,
            )
        target = self._resolve_path(app_name, relative_path)
        if not target.exists():
            raise DashServerError(
                category="workspace_file_not_found",
                summary=f"Workspace file {relative_path} was not found.",
                details={"app": app_name, "path": relative_path},
                jsonrpc_code=-32004,
                http_status=404,
            )
        target.unlink()
        candidate = self._bump_candidate(app_name)
        return {"candidate_version": candidate, "deleted_path": relative_path}

    def diff_against_live(self, app_name: str, live_artifact_path: str) -> dict[str, Any]:
        self._require_workspace(app_name)
        draft_files = self.read_all_files(app_name)
        live_files = self._artifact_files(live_artifact_path)
        diff_lines: list[str] = []
        for relative_path in sorted(set(live_files) | set(draft_files)):
            live_text = live_files.get(relative_path, "").splitlines(keepends=True)
            draft_text = draft_files.get(relative_path, "").splitlines(keepends=True)
            diff_lines.extend(
                difflib.unified_diff(
                    live_text,
                    draft_text,
                    fromfile=f"live/{relative_path}",
                    tofile=f"draft/{relative_path}",
                )
            )
        return {
            "candidate_version": self.draft_summary(app_name)["candidate_version"],
            "diff": "".join(diff_lines),
        }

    def validate_workspace(
        self,
        app_name: str,
        mount_path: str | None = None,
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
        self._require_workspace(app_name)
        files = self.read_all_files(app_name)
        manifest = self._load_manifest(app_name)
        requirements = self._parse_requirements(files.get(self._requirements_filename, ""))
        python_files = {
            path: content for path, content in files.items() if path.endswith(".py")
        }
        parsed_trees: dict[str, ast.AST] = {}
        syntax_errors: list[dict[str, Any]] = []
        lint_warnings: list[dict[str, Any]] = []
        for relative_path, content in python_files.items():
            try:
                tree = ast.parse(content, filename=relative_path)
            except SyntaxError as exc:
                syntax_errors.append(
                    {
                        "path": relative_path,
                        "line": exc.lineno,
                        "message": exc.msg,
                    }
                )
                continue
            parsed_trees[relative_path] = tree
            lint_warnings.extend(self._lint_tree(tree, relative_path))
        callback_report = self._default_callback_report()
        cross_module_symbols = self._default_cross_module_symbol_report(
            syntax_errors=syntax_errors,
        )
        if not syntax_errors:
            cross_module_symbols = self._cross_module_symbol_report(parsed_trees)

        dependency_install = self._default_dependency_install_status(
            syntax_errors=syntax_errors,
            invalid_requirements=requirements["invalid"],
            requirements=requirements["entries"],
        )
        if cross_module_symbols["status"] == "failed":
            dependency_install = {
                "status": "skipped",
                "requirements": requirements["entries"],
                "notes": "Skipped dependency install because local cross-module symbol validation failed.",
            }
        elif not syntax_errors and not requirements["invalid"]:
            dependency_install = self._ensure_dependencies(
                app_name,
                requirements["entries"],
                force_clean=force_clean,
            )

        import_result = {
            "status": "skipped",
            "error": None,
            "traceback": None,
        }
        if not syntax_errors:
            if cross_module_symbols["status"] == "failed":
                import_result = {
                    "status": "skipped",
                    "category": "cross_module_symbols_failed",
                    "error": "Skipped import smoke check because local cross-module symbol validation failed.",
                    "traceback": None,
                }
            elif dependency_install["status"] == "failed":
                import_result = {
                    "status": "skipped",
                    "category": "environment_missing_dependency",
                    "error": "Dependency install failed before import smoke check.",
                    "traceback": None,
                }
            else:
                import_result = self._import_smoke_check(
                    app_name,
                    manifest,
                    declared_packages=requirements["packages"],
                    dependency_install=dependency_install,
                    mount_path=mount_path,
                )
                imported_callbacks = import_result.get("callbacks")
                if isinstance(imported_callbacks, dict):
                    callback_report = imported_callbacks
        credential_safety = self._credential_safety_report(
            files,
            manifest,
            python_files=python_files,
        )
        exasol_validation = self._exasol_validation_report(
            files,
            manifest,
            python_files=python_files,
        )
        is_valid = (
            not syntax_errors
            and cross_module_symbols["status"] != "failed"
            and not requirements["invalid"]
            and dependency_install["status"] != "failed"
            and import_result.get("status") == "passed"
            and callback_report["status"] != "failed"
            and credential_safety["status"] != "failed"
            and exasol_validation["status"] != "failed"
        )
        return {
            "app": app_name,
            "candidate_version": self.draft_summary(app_name)["candidate_version"],
            "manifest": manifest.to_dict(),
            "requirements": requirements,
            "lint": {
                "status": "passed" if not lint_warnings else "passed_with_warnings",
                "warnings": lint_warnings,
            },
            "syntax": {
                "status": "passed" if not syntax_errors else "failed",
                "errors": syntax_errors,
            },
            "cross_module_symbols": cross_module_symbols,
            "imports": import_result,
            "callbacks": callback_report,
            "credential_safety": credential_safety,
            "exasol": exasol_validation,
            "dependency_install": dependency_install,
            "is_valid": is_valid,
        }

    def draft_summary(self, app_name: str) -> dict[str, Any]:
        state = self._read_state(app_name)
        return {
            "candidate_version": int(state.get("candidate_version", 1)),
            "files": self.list_files(app_name),
            **self.workspace_location(app_name),
        }

    def read_all_files(self, app_name: str) -> dict[str, str]:
        files: dict[str, str] = {}
        self._require_workspace(app_name)
        workspace_dir = self._workspace_dir(app_name, create=False)
        assert workspace_dir is not None
        for relative_path in self.list_files(app_name):
            files[relative_path] = (workspace_dir / relative_path).read_text()
        return files

    def artifact_files(self, artifact_path: str) -> dict[str, str]:
        return self._artifact_files(artifact_path)

    def snapshot_workspace(self, app_name: str, artifact_dir: Path) -> None:
        self._require_workspace(app_name)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, content in self.read_all_files(app_name).items():
            target = artifact_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def replace_workspace(
        self,
        app_name: str,
        files: dict[str, str],
        *,
        candidate_version: int = 1,
    ) -> None:
        """Replace the full editable workspace contents for an app."""

        workspace_dir = self._workspace_dir(app_name, create=True)
        assert workspace_dir is not None
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, content in files.items():
            target = workspace_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self._write_state(app_name, {"candidate_version": candidate_version})

    def _artifact_files(self, artifact_path: str) -> dict[str, str]:
        path = Path(artifact_path)
        if path.is_dir():
            files: dict[str, str] = {}
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file() and self._is_editable_file(candidate, path):
                    files[candidate.relative_to(path).as_posix()] = candidate.read_text()
            return files
        if path.suffix == ".json":
            payload = json.loads(path.read_text())
            manifest = payload.get("manifest")
            dashboard = payload.get("bundle")
            bundle_files = self._workspace_files_from_bundle(
                validate_manifest_payload(manifest),
                dashboard,
            )
            return bundle_files
        return {}

    def _patch_matches(self, content: str, search: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        cursor = 0
        while True:
            start = content.find(search, cursor)
            if start < 0:
                break
            end = start + len(search)
            matches.append((start, end))
            cursor = end
        return matches

    def _apply_patch_replacements(
        self,
        content: str,
        replace: str,
        matches: list[tuple[int, int]],
    ) -> tuple[str, list[tuple[int, int]]]:
        parts: list[str] = []
        replacement_ranges: list[tuple[int, int]] = []
        cursor = 0
        output_length = 0
        for start, end in matches:
            unchanged = content[cursor:start]
            parts.append(unchanged)
            output_length += len(unchanged)
            replacement_start = output_length
            parts.append(replace)
            output_length += len(replace)
            replacement_ranges.append((replacement_start, output_length))
            cursor = end
        parts.append(content[cursor:])
        return "".join(parts), replacement_ranges

    def _patch_verification_payload(
        self,
        updated: str,
        replace: str,
        replacement_ranges: list[tuple[int, int]],
    ) -> dict[str, Any]:
        updated_lines = updated.splitlines()
        replacement_line_count = len(replace.splitlines()) if replace else 0
        hunks: list[dict[str, Any]] = []
        matched_line_numbers: list[int] = []
        for index, (start_offset, _end_offset) in enumerate(replacement_ranges, start=1):
            start_line = self._line_number_for_offset(updated, start_offset)
            matched_line_numbers.append(start_line)
            hunks.append(
                self._build_patch_preview_hunk(
                    updated_lines,
                    match_index=index,
                    start_line=start_line,
                    replacement_line_count=replacement_line_count,
                )
            )
        first_hunk = hunks[0] if hunks else {"before_context": [], "after_context": []}
        return {
            "matched_line_numbers": matched_line_numbers,
            "before_context": first_hunk["before_context"],
            "after_context": first_hunk["after_context"],
            "preview": hunks,
        }

    def _build_patch_preview_hunk(
        self,
        updated_lines: list[str],
        *,
        match_index: int,
        start_line: int,
        replacement_line_count: int,
    ) -> dict[str, Any]:
        affected_start = max(1, start_line)
        affected_end = start_line + replacement_line_count - 1 if replacement_line_count > 0 else start_line - 1
        window_start = max(1, start_line - self._patch_preview_context_lines)
        window_end = min(
            len(updated_lines),
            max(start_line, affected_end) + self._patch_preview_context_lines,
        )
        line_window = [
            {
                "line_number": line_number,
                "text": updated_lines[line_number - 1],
                "kind": (
                    "replacement"
                    if replacement_line_count > 0 and affected_start <= line_number <= affected_end
                    else "context"
                ),
            }
            for line_number in range(window_start, window_end + 1)
        ]
        before_end = min(start_line - 1, len(updated_lines))
        after_start = max(affected_end + 1, start_line)
        return {
            "match_index": match_index,
            "start_line": start_line,
            "end_line": max(start_line, affected_end),
            "before_context": [
                {"line_number": line_number, "text": updated_lines[line_number - 1]}
                for line_number in range(window_start, before_end + 1)
            ],
            "after_context": [
                {"line_number": line_number, "text": updated_lines[line_number - 1]}
                for line_number in range(after_start, window_end + 1)
            ],
            "lines": line_window,
        }

    def _line_number_for_offset(self, text: str, offset: int) -> int:
        bounded_offset = max(0, min(offset, len(text)))
        return text.count("\n", 0, bounded_offset) + 1

    def _default_cross_module_symbol_report(
        self,
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
            "notes": "Validated direct local module imports and aliased local module attribute access.",
        }

    def _cross_module_symbol_report(
        self,
        trees: dict[str, ast.AST],
    ) -> dict[str, Any]:
        module_to_path, path_to_module = self._local_module_index(trees.keys())
        module_analysis = {
            path: self._analyze_local_module(tree)
            for path, tree in trees.items()
        }
        issues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        seen_issues: set[tuple[str, int | None, str, str]] = set()
        seen_warnings: set[tuple[str, int | None, str]] = set()
        for relative_path, tree in trees.items():
            references, reference_warnings = self._collect_cross_module_references(
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
            "notes": "Validated direct local module imports and aliased local module attribute access.",
        }

    def _local_module_index(
        self,
        paths: Any,
    ) -> tuple[dict[str, str], dict[str, str]]:
        module_to_path: dict[str, str] = {}
        path_to_module: dict[str, str] = {}
        for relative_path in sorted(str(path) for path in paths):
            module_name = self._module_name_from_path(relative_path)
            if module_name is None:
                continue
            module_to_path[module_name] = relative_path
            path_to_module[relative_path] = module_name
        return module_to_path, path_to_module

    def _module_name_from_path(self, relative_path: str) -> str | None:
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

    def _analyze_local_module(self, tree: ast.AST) -> dict[str, Any]:
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
            if self._is_docstring_expr(node) or isinstance(node, ast.Pass):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined_symbols.add(node.name)
                if node.name == "__getattr__":
                    analysis_limited = True
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    defined_symbols.update(self._assignment_target_names(target))
                continue
            if isinstance(node, ast.AnnAssign):
                defined_symbols.update(self._assignment_target_names(node.target))
                continue
            if isinstance(node, ast.AugAssign):
                defined_symbols.update(self._assignment_target_names(node.target))
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

    def _assignment_target_names(self, target: ast.AST) -> set[str]:
        names: set[str] = set()
        if isinstance(target, ast.Name):
            names.add(target.id)
            return names
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                names.update(self._assignment_target_names(item))
        return names

    def _is_docstring_expr(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )

    def _collect_cross_module_references(
        self,
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

            resolved_module = self._resolve_local_import_module(
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
        self,
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

    def _import_smoke_check(
        self,
        app_name: str,
        manifest: AppManifest,
        *,
        declared_packages: list[str],
        dependency_install: dict[str, Any],
        mount_path: str | None = None,
    ) -> dict[str, Any]:
        app_path = self._resolve_path(app_name, self._app_filename)
        # Phase 2: when dependency install reported a non-server python_executable
        # (i.e. per_app dependency isolation is on), run the smoke check inside that
        # interpreter as a child process. The in-process path stays the default.
        subprocess_python = dependency_install.get("python_executable") if isinstance(dependency_install, dict) else None
        if (
            isinstance(subprocess_python, str)
            and subprocess_python
            and subprocess_python != sys.executable
        ):
            return self._import_smoke_check_subprocess(
                app_name=app_name,
                manifest=manifest,
                declared_packages=declared_packages,
                dependency_install=dependency_install,
                mount_path=mount_path,
                app_path=app_path,
                subprocess_python=subprocess_python,
            )
        module_name = f"dash_server_workspace_{app_name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, app_path)
        assert spec is not None and spec.loader is not None
        with isolated_dash_callback_globals(), isolated_local_imports(app_path.parent):
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                return self._import_failure_result(
                    exc,
                    declared_packages=declared_packages,
                    dependency_install=dependency_install,
                )

            factory = getattr(module, "create_dash_app", None)
            if not callable(factory):
                return {
                    "status": "failed",
                    "error": "app.py must define create_dash_app(server, url_base_pathname, metadata).",
                    "traceback": None,
                }

            try:
                test_server = Flask(f"dash_server.validate.{app_name}")
                created = factory(
                    server=test_server,
                    url_base_pathname=f"{(mount_path or manifest.route).rstrip('/')}/",
                    metadata={
                        **manifest.to_dict(),
                        "route": mount_path or manifest.route,
                    },
                )
                if isinstance(created, Dash):
                    apply_hosted_footer(created)
                    finalize_dash_app_callbacks(created)
            except Exception as exc:
                return self._import_failure_result(
                    exc,
                    declared_packages=declared_packages,
                    dependency_install=dependency_install,
                )
        if not isinstance(created, Dash):
            return {
                "status": "failed",
                "error": "create_dash_app must return a dash.Dash instance.",
                "traceback": None,
            }

        mount_check = verify_dash_mount(test_server)
        if mount_check["status"] != "passed":
            return {
                "status": "failed",
                "category": mount_check["category"],
                "error": mount_check["message"],
                "traceback": None,
                "details": {
                    "path": mount_check.get("path"),
                    "status_code": mount_check.get("status_code"),
                },
            }

        callback_report = self._describe_callbacks(created)
        return {
            "status": "passed",
            "error": None,
            "traceback": None,
            "callbacks": callback_report,
        }

    def _import_smoke_check_subprocess(
        self,
        *,
        app_name: str,
        manifest: AppManifest,
        declared_packages: list[str],
        dependency_install: dict[str, Any],
        mount_path: str | None,
        app_path: Path,
        subprocess_python: str,
    ) -> dict[str, Any]:
        """Run the import smoke check inside a child process using the per-app env's interpreter."""

        # The dash_server package must be importable inside the env so the worker module
        # can be loaded. We extend PYTHONPATH to point at the control-plane source. Once the
        # helper package is published independently this becomes unnecessary.
        env = dict(__import__("os").environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        # `src/` is the directory holding both `dash_server/` and `dash_server_runtime/`.
        src_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = (
            f"{src_root}{__import__('os').pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else src_root
        )

        cmd = [
            subprocess_python,
            "-m",
            "dash_server.runtime.worker",
            "--mode=validate",
            "--app-name",
            app_name,
            "--app-source",
            str(app_path),
            "--mount-path",
            mount_path or manifest.route,
            "--manifest-json",
            json.dumps(manifest.to_dict()),
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "error": "Subprocess import smoke check timed out.",
                "traceback": None,
                "subprocess": {
                    "command": cmd,
                    "exit_code": None,
                    "timeout": True,
                    "stdout_tail": (exc.stdout or "")[-4000:],
                    "stderr_tail": (exc.stderr or "")[-4000:],
                    "python_executable": subprocess_python,
                    "environment_id": dependency_install.get("environment_id"),
                },
            }

        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        parsed: dict[str, Any] | None = None
        # The worker writes a single JSON document on the last line of stdout.
        for line in reversed(stdout_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            break

        subprocess_meta = {
            "command": cmd,
            "exit_code": completed.returncode,
            "timeout": False,
            "stdout_tail": stdout_text[-4000:],
            "stderr_tail": stderr_text[-4000:],
            "python_executable": subprocess_python,
            "environment_id": dependency_install.get("environment_id"),
        }

        if parsed is None:
            return {
                "status": "failed",
                "error": "Subprocess import smoke check did not produce a parseable JSON result.",
                "traceback": None,
                "subprocess": subprocess_meta,
            }

        parsed.setdefault("error", None)
        parsed.setdefault("traceback", None)
        parsed["subprocess"] = subprocess_meta
        return parsed

    def _workspace_files_from_bundle(
        self, manifest: AppManifest, dashboard: dict[str, Any]
    ) -> dict[str, str]:
        return {
            self._manifest_filename: json.dumps(manifest.to_dict(), indent=2),
            self._app_filename: self._render_app_py(manifest, dashboard),
            self._requirements_filename: "dash>=4.0,<5.0\n",
        }

    def _render_app_py(self, manifest: AppManifest, dashboard: dict[str, Any]) -> str:
        metrics_literal = json.dumps(dashboard["metrics"], indent=4)
        headline_literal = json.dumps(dashboard["headline"])
        summary_literal = json.dumps(dashboard["summary"])
        return f'''from dash import Dash, Input, Output, dcc, html

HEADLINE = {headline_literal}
SUMMARY = {summary_literal}
METRICS = {metrics_literal}


def create_dash_app(server, url_base_pathname, metadata):
    prefix = url_base_pathname.rstrip("/") + "/"
    app = Dash(
        __name__,
        server=server,
        routes_pathname_prefix="/",
        requests_pathname_prefix=prefix,
        title=metadata.get("title", {json.dumps(manifest.title)}),
    )

    options = [{{"label": metric["label"], "value": metric["label"]}} for metric in METRICS]
    lookup = {{metric["label"]: metric["value"] for metric in METRICS}}

    app.layout = html.Div(
        [
            html.H1(HEADLINE),
            html.P(SUMMARY),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(metric["label"], className="metric-label"),
                            html.Strong(metric["value"], className="metric-value"),
                        ],
                        className="metric-card",
                    )
                    for metric in METRICS
                ],
                className="metric-grid",
            ),
            html.H2("Highlight"),
            dcc.Dropdown(
                id="metric-selector",
                options=options,
                value=options[0]["value"],
                clearable=False,
            ),
            html.Div(id="metric-detail"),
        ],
        style={{
            "fontFamily": "sans-serif",
            "margin": "2rem auto",
            "maxWidth": "960px",
        }},
    )

    @app.callback(Output("metric-detail", "children"), Input("metric-selector", "value"))
    def show_metric(selected_metric):
        return f"{{selected_metric}}: {{lookup[selected_metric]}}"

    return app
'''

    def _parse_requirements(self, content: str) -> dict[str, Any]:
        entries: list[str] = []
        invalid: list[str] = []
        packages: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                requirement = Requirement(line)
                entries.append(line)
                packages.append(self._normalize_requirement_name(requirement.name))
            except InvalidRequirement:
                invalid.append(line)
        return {"entries": entries, "invalid": invalid, "packages": packages}

    def _lint_tree(self, tree: ast.AST, relative_path: str) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        dict_bindings = self._literal_dict_key_bindings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                warnings.append(
                    {
                        "path": relative_path,
                        "line": node.lineno,
                        "message": "Avoid wildcard imports in hosted app workspaces.",
                    }
                )
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                warnings.append(
                    {
                        "path": relative_path,
                        "line": node.lineno,
                        "message": "Avoid bare except clauses in hosted app workspaces.",
                    }
                )
            warnings.extend(self._plotly_lint_warnings(node, relative_path, dict_bindings))
        return warnings

    def _default_callback_report(self) -> dict[str, Any]:
        return {
            "status": "skipped",
            "count": 0,
            "callbacks": [],
            "missing_layout_ids": [],
            "suppress_callback_exceptions": False,
        }

    def _describe_callbacks(self, dash_app: Dash) -> dict[str, Any]:
        layout_ids = self._extract_layout_ids(dash_app.layout)
        suppress_callback_exceptions = bool(
            getattr(dash_app.config, "get", lambda *_args, **_kwargs: False)(
                "suppress_callback_exceptions",
                False,
            )
        )
        callbacks: list[dict[str, Any]] = []
        missing_layout_ids: set[str] = set()

        for output_key, callback_data in sorted(dash_app.callback_map.items()):
            inputs = self._serialize_callback_dependencies(callback_data.get("inputs"))
            state = self._serialize_callback_dependencies(callback_data.get("state"))
            outputs = self._serialize_callback_outputs(
                callback_data.get("output"),
                fallback_key=output_key,
            )
            referenced_ids = {
                reference["id"]
                for reference in [*inputs, *state, *outputs]
                if isinstance(reference.get("id"), str)
            }
            callback_missing_ids = sorted(
                reference_id
                for reference_id in referenced_ids
                if reference_id not in layout_ids
            )
            missing_layout_ids.update(callback_missing_ids)
            callbacks.append(
                {
                    "output_key": output_key,
                    "outputs": outputs,
                    "inputs": inputs,
                    "state": state,
                    "missing_layout_ids": callback_missing_ids,
                }
            )

        if missing_layout_ids:
            status = "passed_with_warnings" if suppress_callback_exceptions else "failed"
        else:
            status = "passed"

        return {
            "status": status,
            "count": len(callbacks),
            "callbacks": callbacks,
            "missing_layout_ids": sorted(missing_layout_ids),
            "suppress_callback_exceptions": suppress_callback_exceptions,
        }

    def _serialize_callback_dependencies(self, dependencies: Any) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        if not isinstance(dependencies, list):
            return serialized
        for dependency in dependencies:
            if isinstance(dependency, dict):
                component_id = dependency.get("id")
                component_property = dependency.get("property")
            else:
                component_id = getattr(dependency, "component_id", None)
                component_property = getattr(dependency, "component_property", None)
            serialized.append(
                {
                    "id": _to_jsonable(component_id),
                    "property": _to_jsonable(component_property),
                }
            )
        return serialized

    def _serialize_callback_outputs(self, output: Any, *, fallback_key: str) -> list[dict[str, Any]]:
        if isinstance(output, (list, tuple)):
            return [
                {
                    "id": _to_jsonable(getattr(item, "component_id", None)),
                    "property": _to_jsonable(getattr(item, "component_property", None)),
                }
                for item in output
            ]
        component_id = getattr(output, "component_id", None)
        component_property = getattr(output, "component_property", None)
        if component_id is not None or component_property is not None:
            return [
                {"id": _to_jsonable(component_id), "property": _to_jsonable(component_property)}
            ]
        if "." in fallback_key:
            output_id, output_property = fallback_key.split(".", 1)
            return [{"id": output_id, "property": output_property}]
        return [{"id": None, "property": fallback_key}]

    def _extract_layout_ids(self, node: Any) -> set[str]:
        layout_ids: set[str] = set()
        stack: list[Any] = [node]
        while stack:
            current = stack.pop()
            if current is None:
                continue
            if isinstance(current, (list, tuple)):
                stack.extend(current)
                continue
            component_id = getattr(current, "id", None)
            if isinstance(component_id, str):
                layout_ids.add(component_id)
            children = getattr(current, "children", None)
            if children is not None:
                stack.append(children)
        return layout_ids

    def _literal_dict_key_bindings(self, tree: ast.AST) -> dict[str, set[str]]:
        bindings: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    keys = self._literal_dict_keys(node.value, bindings)
                    if keys:
                        bindings[target.id] = keys
        return bindings

    def _literal_dict_keys(
        self,
        node: ast.AST,
        bindings: dict[str, set[str]],
    ) -> set[str]:
        if isinstance(node, ast.Dict):
            keys: set[str] = set()
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            return keys
        if isinstance(node, ast.Name):
            return set(bindings.get(node.id, set()))
        return set()

    def _plotly_lint_warnings(
        self,
        node: ast.AST,
        relative_path: str,
        dict_bindings: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        if not isinstance(node, ast.Call):
            return []

        warnings: list[dict[str, Any]] = []
        function_name = self._call_function_name(node.func)
        explicit_keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
        expanded_keys: set[str] = set()
        for keyword in node.keywords:
            if keyword.arg is None:
                expanded_keys.update(self._literal_dict_keys(keyword.value, dict_bindings))

        if function_name.endswith("update_layout"):
            for duplicate in sorted(explicit_keywords & expanded_keys):
                warnings.append(
                    {
                        "path": relative_path,
                        "line": getattr(node, "lineno", None),
                        "message": (
                            f"Plotly update_layout may set {duplicate!r} twice via **kwargs and an explicit keyword."
                        ),
                    }
                )

        for keyword in node.keywords:
            if not keyword.arg:
                continue
            color_value = self._literal_string(keyword.value)
            if (
                keyword.arg.lower().endswith("color")
                and isinstance(color_value, str)
                and re.fullmatch(r"#[0-9a-fA-F]{8}", color_value)
            ):
                warnings.append(
                    {
                        "path": relative_path,
                        "line": getattr(keyword.value, "lineno", getattr(node, "lineno", None)),
                        "message": (
                            f"Plotly {keyword.arg} uses 8-digit hex {color_value!r}; prefer rgba(...) because many Plotly properties reject #RRGGBBAA."
                        ),
                    }
                )

        return warnings

    def _credential_safety_report(
        self,
        files: dict[str, str],
        manifest: AppManifest,
        *,
        python_files: dict[str, str],
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        raw_data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else None
        if isinstance(raw_data_sources, dict):
            primary = raw_data_sources.get("primary")
            if isinstance(primary, dict) and primary.get("kind") == "exasol":
                forbidden_keys = sorted(
                    key
                    for key in primary
                    if key
                    in {
                        "dsn",
                        "user",
                        "password",
                        "access_token",
                        "refresh_token",
                        "saas_pat",
                        "secret",
                        "secret_ref",
                    }
                )
                if forbidden_keys:
                    findings.append(
                        {
                            "path": self._manifest_filename,
                            "message": (
                                "Exasol data_sources must reference a server-side profile and must not embed "
                                f"credential keys: {', '.join(forbidden_keys)}."
                            ),
                        }
                    )

        for relative_path, content in python_files.items():
            if "pyexasol.connect" in content:
                findings.append(
                    {
                        "path": relative_path,
                        "message": (
                            "Hosted apps must not call pyexasol.connect(...) directly. "
                            "Use a server-side Exasol profile and runtime helper instead."
                        ),
                    }
                )
            if self._exasol_env_pattern.search(content):
                findings.append(
                    {
                        "path": relative_path,
                        "message": (
                            "Hosted apps must not read EXA_/EXASOL_ credential environment variables directly. "
                            "Bind an Exasol profile and let the server resolve credentials."
                        ),
                    }
                )
            if re.search(r"\b(?:password|access_token|refresh_token|saas_pat)\s*=", content):
                findings.append(
                    {
                        "path": relative_path,
                        "message": (
                            "Hosted app source appears to define database credential parameters directly. "
                            "Move Exasol credentials into the server-side profile configuration."
                        ),
                    }
                )

        status = "failed" if findings else "passed"
        return {"status": status, "findings": findings}

    def _exasol_validation_report(
        self,
        files: dict[str, str],
        manifest: AppManifest,
        *,
        python_files: dict[str, str],
    ) -> dict[str, Any]:
        if not self._is_exasol_workspace(manifest):
            return {"status": "not_applicable", "issues": []}

        issues: list[dict[str, Any]] = []
        exasol_query_calls = {
            "execute_profile_query",
            "query_rows",
            "query_one",
            "query_scalar",
            "load_rows",
            "load_row",
            "load_scalar",
        }

        for relative_path, content in python_files.items():
            if "import pyexasol" in content or "from pyexasol" in content:
                issues.append(
                    {
                        "level": "warning",
                        "path": relative_path,
                        "message": "Exasol-backed hosted apps should rely on the server helper path instead of importing pyexasol directly.",
                    }
                )
            try:
                tree = ast.parse(content, filename=relative_path)
            except SyntaxError:
                continue
            for node in tree.body:
                call = self._top_level_call(node)
                if call is None:
                    continue
                if self._call_matches_names(call.func, exasol_query_calls):
                    issues.append(
                        {
                            "level": "error",
                            "path": relative_path,
                            "line": getattr(call, "lineno", None),
                            "message": "Do not execute Exasol queries at import time. Run them inside callbacks or explicit request handlers.",
                        }
                    )

        # Common Exasol reserved words that bite scaffold authors when used as bare AS aliases.
        # Exasol's full reserved list is much larger; these are the ones we've actually
        # seen trip up persona-1 / persona-2 / persona-3 SQL.
        reserved_alias_words = {
            "DAY", "MONTH", "YEAR", "HOUR", "MINUTE", "SECOND",
            "ORDER", "GROUP", "LEVEL", "USER", "TIMESTAMP", "DATE",
            "TYPE", "VALUE", "ROW", "RANK", "COUNT",
        }
        reserved_alias_re = re.compile(
            r"\bAS\s+(" + "|".join(sorted(reserved_alias_words)) + r")\b",
            re.IGNORECASE,
        )

        for relative_path, content in files.items():
            if not relative_path.startswith("queries/") or not relative_path.endswith(".sql"):
                continue
            normalized = content.upper()
            for match in reserved_alias_re.finditer(content):
                issues.append(
                    {
                        "level": "warning",
                        "path": relative_path,
                        "line": content[: match.start()].count("\n") + 1,
                        "message": (
                            f"AS {match.group(1).upper()} uses an Exasol reserved word as a bare alias. "
                            f'Quote it: AS "{match.group(1).upper()}".'
                        ),
                    }
                )
            if "SELECT *" in normalized:
                issues.append(
                    {
                        "level": "warning",
                        "path": relative_path,
                        "message": "Avoid SELECT * in Exasol query files. Select only the columns the dashboard needs.",
                    }
                )
            stripped = content.strip()
            if ";" in stripped[:-1]:
                issues.append(
                    {
                        "level": "warning",
                        "path": relative_path,
                        "message": "Prefer one statement per Exasol SQL file.",
                    }
                )
            from_clause = " " + re.sub(r"\s+", " ", normalized).strip() + " "
            # Skip the "no LIMIT or aggregation" warning for single-row scalar queries
            # that come only from DUAL (e.g. the scaffold's placeholder
            # `SELECT 1240 AS ACTIVE_CUSTOMERS FROM DUAL`).
            only_from_dual = (
                " FROM DUAL " in from_clause
                and " JOIN " not in from_clause
                and " UNION " not in from_clause
            )
            if (
                " FROM " in from_clause
                and " LIMIT " not in from_clause
                and " GROUP BY " not in from_clause
                and not any(token in normalized for token in ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX("))
                and not only_from_dual
            ):
                issues.append(
                    {
                        "level": "warning",
                        "path": relative_path,
                        "message": "This query does not declare LIMIT or obvious aggregation. Ensure Exasol is doing bounded or aggregated work before returning rows.",
                    }
                )

        # Dead SQL detection: queries/*.sql files that no .py file references.
        sql_files = {p for p in files if p.startswith("queries/") and p.endswith(".sql")}
        if sql_files:
            referenced: set[str] = set()
            for relative_path, content in files.items():
                if relative_path.endswith(".py"):
                    for sql_path in sql_files:
                        if sql_path in content:
                            referenced.add(sql_path)
            for sql_path in sorted(sql_files - referenced):
                issues.append(
                    {
                        "level": "info",
                        "path": sql_path,
                        "message": (
                            f"{sql_path} is not referenced by any .py file in the workspace. "
                            "Delete unused SQL files with app_delete_file to keep the workspace tidy."
                        ),
                    }
                )

        if any(issue["level"] == "error" for issue in issues):
            status = "failed"
        elif any(issue["level"] == "warning" for issue in issues):
            status = "passed_with_warnings"
        else:
            status = "passed"
        return {"status": status, "issues": issues}

    def _is_exasol_workspace(self, manifest: AppManifest) -> bool:
        data_sources = manifest.data_sources if isinstance(manifest.data_sources, dict) else {}
        primary = data_sources.get("primary")
        return bool(
            manifest.template == "exasol-analytics"
            or (isinstance(primary, dict) and primary.get("kind") == "exasol")
        )

    def _top_level_call(self, node: ast.stmt) -> ast.Call | None:
        value = None
        if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            value = node.value
        if isinstance(value, ast.Call):
            return value
        return None

    def _call_matches_names(self, func: ast.AST, allowed_names: set[str]) -> bool:
        if isinstance(func, ast.Name):
            return func.id in allowed_names
        if isinstance(func, ast.Attribute):
            return func.attr in allowed_names
        return False

    def _call_function_name(self, func: ast.AST) -> str:
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return ""

    def _literal_string(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _load_manifest(self, app_name: str) -> AppManifest:
        manifest_text = self.read_file(app_name, self._manifest_filename)
        try:
            manifest_payload = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise DashServerError(
                category="workspace_validation_error",
                summary="dash-app.json must contain valid JSON.",
                details={"app": app_name, "path": self._manifest_filename, "message": str(exc)},
                jsonrpc_code=-32602,
            ) from exc
        return validate_manifest_payload(manifest_payload)

    def _bump_candidate(self, app_name: str) -> int:
        state = self._read_state(app_name)
        current = int(state.get("candidate_version", 1))
        next_value = current + 1
        self._write_state(app_name, {"candidate_version": next_value})
        return next_value

    def _read_state(self, app_name: str) -> dict[str, Any]:
        workspace_dir = self._workspace_dir(app_name, create=False)
        if workspace_dir is None:
            return {"candidate_version": 1}
        state_path = workspace_dir / self._state_filename
        if not state_path.exists():
            return {"candidate_version": 1}
        return json.loads(state_path.read_text())

    def _write_state(self, app_name: str, state: dict[str, Any]) -> None:
        workspace_dir = self._workspace_dir(app_name, create=True)
        assert workspace_dir is not None
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / self._state_filename).write_text(json.dumps(state, indent=2))

    def _require_workspace(self, app_name: str) -> None:
        workspace_dir = self._workspace_dir(app_name, create=False)
        if workspace_dir is None or not workspace_dir.exists():
            raise DashServerError(
                category="workspace_not_found",
                summary=f"Workspace for app {app_name} was not found.",
                details={"app": app_name},
                jsonrpc_code=-32004,
                http_status=404,
            )

    def _workspace_dir(
        self,
        app_name: str,
        *,
        create: bool,
        migrate_legacy: bool = False,
    ) -> Path | None:
        legacy_dir = self._legacy_workspace_dir(app_name)

        if self.git_worktree_service is None:
            if create:
                legacy_dir.mkdir(parents=True, exist_ok=True)
            return legacy_dir if create or legacy_dir.exists() else None

        if self.git_worktree_service.can_use_worktrees():
            worktree_dir = self.git_worktree_service.workspace_dir(app_name)
            if worktree_dir.exists():
                return worktree_dir
            if legacy_dir.exists() and (create or migrate_legacy):
                return self.git_worktree_service.migrate_legacy_workspace(app_name, legacy_dir)
            if create:
                return self.git_worktree_service.ensure_workspace_dir(app_name)
            return None

        if create:
            legacy_dir.mkdir(parents=True, exist_ok=True)
        return legacy_dir if create or legacy_dir.exists() else None

    def _legacy_workspace_dir(self, app_name: str) -> Path:
        return self.workspaces_root / app_name

    def _is_git_workspace_path(self, app_name: str, path: Path) -> bool:
        if self.git_worktree_service is None:
            return False
        try:
            return path.resolve() == self.git_worktree_service.workspace_dir(app_name).resolve()
        except FileNotFoundError:
            return False

    def _resolve_path(self, app_name: str, relative_path: str) -> Path:
        if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise DashServerError(
                category="tool_validation_error",
                summary="Workspace paths must stay within the app workspace.",
                details={"app": app_name, "path": relative_path},
                jsonrpc_code=-32602,
            )
        workspace_dir = self._workspace_dir(app_name, create=True)
        assert workspace_dir is not None
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir / relative_path

    def _is_editable_file(self, path: Path, root: Path) -> bool:
        relative_parts = path.relative_to(root).parts
        if path.name == self._state_filename:
            return False
        if "__pycache__" in relative_parts:
            return False
        return path.suffix != ".pyc"

    def _require_string(self, value: Any, field_name: str, *, allow_empty: bool = False) -> str:
        if isinstance(value, str) and (allow_empty or value):
            return value
        raise DashServerError(
            category="tool_validation_error",
            summary=f"{field_name} must be a string.",
            details={"field": field_name},
            jsonrpc_code=-32602,
        )

    def _write_files(self, app_name: str, files: Any) -> list[str]:
        if not isinstance(files, list) or not files:
            raise DashServerError(
                category="tool_validation_error",
                summary="files must be a non-empty array.",
                details={"field": "files"},
                jsonrpc_code=-32602,
            )
        touched: list[str] = []
        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise DashServerError(
                    category="tool_validation_error",
                    summary="Each file entry must be an object.",
                    details={"field": "files"},
                    jsonrpc_code=-32602,
                )
            relative_path = self._require_string(file_entry.get("path"), "files.path")
            content = self._require_string(file_entry.get("content"), "files.content", allow_empty=True)
            target = self._resolve_path(app_name, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            touched.append(relative_path)
        return touched

    def _ensure_dependencies(
        self,
        app_name: str,
        requirements: list[str],
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
        if self.dependency_installer is not None:
            return self.dependency_installer(
                app_name,
                requirements,
                force_clean=force_clean,
            )
        return {
            "status": "ready",
            "requirements": requirements,
            "notes": "No dependency installer was configured for workspace validation.",
        }

    def _default_dependency_install_status(
        self,
        *,
        syntax_errors: list[dict[str, Any]],
        invalid_requirements: list[str],
        requirements: list[str],
    ) -> dict[str, Any]:
        if syntax_errors:
            return {
                "status": "skipped",
                "requirements": requirements,
                "notes": "Skipped dependency install because Python syntax validation failed.",
            }
        if invalid_requirements:
            return {
                "status": "blocked",
                "requirements": requirements,
                "notes": "Skipped dependency install because requirements.txt contains invalid entries.",
            }
        return {
            "status": "ready",
            "requirements": requirements,
            "notes": "Requirements are ready for lightweight installation before import validation.",
        }

    def _import_failure_result(
        self,
        exc: Exception,
        *,
        declared_packages: list[str],
        dependency_install: dict[str, Any],
    ) -> dict[str, Any]:
        error_text = f"{type(exc).__name__}: {exc}"
        traceback_text = traceback.format_exc()
        missing_dependency = self._extract_missing_dependency(error_text)
        declared = (
            missing_dependency is not None
            and self._normalize_requirement_name(missing_dependency) in declared_packages
        )
        if missing_dependency is not None and (
            declared or dependency_install.get("status") == "failed"
        ):
            return {
                "status": "failed",
                "category": "environment_missing_dependency",
                "error": error_text,
                "traceback": traceback_text,
                "missing_dependency": missing_dependency,
                "declared_in_requirements": declared,
            }
        return {
            "status": "failed",
            "category": "import_error",
            "error": error_text,
            "traceback": traceback_text,
        }

    def _extract_missing_dependency(self, error_text: str) -> str | None:
        module_match = re.search(r"No module named ['\"]([^'\"]+)['\"]", error_text)
        if module_match:
            return module_match.group(1).split(".")[0]
        requires_match = re.search(r"requires ([A-Za-z0-9_.-]+) to be installed", error_text)
        if requires_match:
            return requires_match.group(1)
        return None

    def _normalize_requirement_name(self, name: str) -> str:
        return name.strip().lower().replace("_", "-")


def _to_jsonable(value: Any) -> Any:
    """Coerce captured callback IDs/properties to JSON-safe shapes.

    Pattern-matching callback IDs are dicts containing Dash sentinel objects
    (`dash.ALL`, `dash.MATCH`, `dash.ALLSMALLER`). Those are instances of
    `dash._wildcards.Wildcard` and not JSON-serializable. We render them as
    their `repr` (the user-facing form, e.g. ``"<ALL>"``) so the validation
    report and the MCP-tool response can still be serialized losslessly enough
    for an agent to spot the pattern in question.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return repr(value)
