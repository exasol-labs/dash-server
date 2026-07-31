"""Filesystem-backed draft workspace management."""

from __future__ import annotations

import ast
import difflib
import json
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from dash import Dash
from flask import Flask, current_app
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
from dash_server_runtime.worker._app_loader import load_app_module
from dash_server.artifacts_io import (
    APP_ENTRYPOINT_FILENAME,
    APP_MANIFEST_FILENAME,
    REQUIREMENTS_FILENAME,
    is_artifact_source_part,
)
from dash_server.errors import JSONRPC_INVALID_PARAMS
from dash_server.exceptions import DashServerError
from dash_server.paths import safe_join
from dash_server.gitops import GitWorktreeService
from dash_server.imports import isolated_local_imports
from dash_server.registry.models import AppManifest
from dash_server.workspace.validation_pipeline import (
    ValidationContext,
    run_pipeline,
)


class WorkspaceService:
    """Manage editable draft workspaces for hosted apps."""

    _state_filename = ".draft-state.json"
    _manifest_filename = APP_MANIFEST_FILENAME
    _app_filename = APP_ENTRYPOINT_FILENAME
    _requirements_filename = REQUIREMENTS_FILENAME
    _patch_preview_context_lines = 3

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

    def delete_workspace(self, app_name: str) -> dict[str, Any]:
        """Remove all draft workspace state for an app."""

        location = self.workspace_location(app_name)
        worktree_result: dict[str, object] | None = None
        if self.git_worktree_service is not None:
            worktree_result = self.git_worktree_service.delete_worktree(app_name)

        legacy_path = self._legacy_workspace_dir(app_name)
        removed_legacy_workspace = False
        if legacy_path.exists():
            shutil.rmtree(legacy_path)
            removed_legacy_workspace = True
        return {
            **location,
            "removed_legacy_workspace": removed_legacy_workspace,
            "worktree": worktree_result,
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
            )
        if occurrences > 1 and not replace_all:
            raise DashServerError(
                category="patch_error",
                summary="Search text matched multiple locations; set replace_all to true to continue.",
                details={"app": app_name, "path": relative_path, "occurrences": occurrences},
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
            )
        target = self._resolve_path(app_name, relative_path)
        if not target.exists():
            raise DashServerError(
                category="workspace_file_not_found",
                summary=f"Workspace file {relative_path} was not found.",
                details={"app": app_name, "path": relative_path},
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
        parsed_trees, syntax_errors = self._parse_python_files(python_files)

        context = ValidationContext(
            service=self,
            app_name=app_name,
            mount_path=mount_path,
            force_clean=force_clean,
            files=files,
            manifest=manifest,
            requirements=requirements,
            python_files=python_files,
            parsed_trees=parsed_trees,
            syntax_errors=syntax_errors,
            consumption_exports_enabled=self._consumption_exports_enabled(),
        )
        reports, is_valid = run_pipeline(context)
        return {
            "app": app_name,
            "candidate_version": self.draft_summary(app_name)["candidate_version"],
            "manifest": manifest.to_dict(),
            **reports,
            "is_valid": is_valid,
        }

    def _consumption_exports_enabled(self) -> bool:
        """Read the live server-wide exports policy, if a Flask app context is active.

        PS26-BUG-014: `app_validate` runs inside the MCP request's Flask app context, so
        `current_app.extensions["consumption_service"].policy.exports_enabled` reflects the
        same flag `dash://runtime/status` reports. Falls back to the permissive default
        (no warning) outside an app context, e.g. direct unit tests of this service.
        """

        try:
            consumption_service = current_app.extensions.get("consumption_service")
        except RuntimeError:
            return True
        if consumption_service is None:
            return True
        return bool(consumption_service.policy.exports_enabled)

    def _parse_python_files(
        self,
        python_files: dict[str, str],
    ) -> tuple[dict[str, ast.AST], list[dict[str, Any]]]:
        """Parse each ``.py`` file, collecting ASTs and syntax errors.

        The returned ``parsed_trees`` preserves ``python_files`` iteration order
        for files that parsed cleanly; files with syntax errors are recorded in
        ``syntax_errors`` and omitted from the trees.
        """

        parsed_trees: dict[str, ast.AST] = {}
        syntax_errors: list[dict[str, Any]] = []
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
        return parsed_trees, syntax_errors

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
        with isolated_dash_callback_globals(), isolated_local_imports(app_path.parent):
            # Shared "load app.py as a fresh module" contract — the same loader the
            # out-of-process worker uses (dash_server_runtime.worker._serve), so the
            # in-process smoke check and the subprocess serve path import identically.
            try:
                module = load_app_module(app_path, app_name)
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
                    apply_hosted_footer(created, wrap=True)
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
            "dash_server_runtime.worker",
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

        report: dict[str, Any] = {
            "status": status,
            "count": len(callbacks),
            "callbacks": callbacks,
            "missing_layout_ids": sorted(missing_layout_ids),
            "suppress_callback_exceptions": suppress_callback_exceptions,
        }
        # PS27-BUG-005: a completely standard, valid Dash idiom - pattern-matching
        # (ALL/MATCH/ALLSMALLER) components rendered dynamically by another callback,
        # never present in the static initial layout - fails here with no hint that
        # `suppress_callback_exceptions=True` is the fix. Found independently by two
        # personas in the round-2 study on unrelated apps. Not auto-passing this (a
        # plain missing id can also be a genuine typo/bug the layout-presence check is
        # right to catch) - just naming the fix explicitly when the shape says it's
        # actually a wildcard pattern, not a typo.
        if missing_layout_ids and not suppress_callback_exceptions:
            wildcard_ids = sorted(
                missing_id for missing_id in missing_layout_ids if _looks_like_pattern_matching_id(missing_id)
            )
            if wildcard_ids:
                report["hint"] = (
                    "The following missing_layout_ids look like Dash pattern-matching ids "
                    f"(ALL/MATCH/ALLSMALLER): {wildcard_ids}. If these components are rendered "
                    "dynamically by another callback rather than present in the initial "
                    "layout - the standard Dash idiom for e.g. 'acknowledge one of N "
                    "dynamically-rendered rows' - set suppress_callback_exceptions=True in the "
                    "Dash(...) constructor."
                )
        return report

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
            # Malformed input, not a build-state conflict: override the
            # category's 409 default with invalid-params semantics.
            raise DashServerError(
                category="workspace_validation_error",
                summary="dash-app.json must contain valid JSON.",
                details={"app": app_name, "path": self._manifest_filename, "message": str(exc)},
                jsonrpc_code=JSONRPC_INVALID_PARAMS,
                http_status=400,
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
        workspace_dir = self._workspace_dir(app_name, create=True)
        assert workspace_dir is not None
        workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            return safe_join(workspace_dir, relative_path)
        except ValueError:
            raise DashServerError(
                category="tool_validation_error",
                summary="Workspace paths must stay within the app workspace.",
                details={"app": app_name, "path": relative_path},
            ) from None

    def _is_editable_file(self, path: Path, root: Path) -> bool:
        if path.name == self._state_filename:
            return False
        return is_artifact_source_part(path.relative_to(root))

    def _require_string(self, value: Any, field_name: str, *, allow_empty: bool = False) -> str:
        if isinstance(value, str) and (allow_empty or value):
            return value
        raise DashServerError(
            category="tool_validation_error",
            summary=f"{field_name} must be a string.",
            details={"field": field_name},
        )

    def _write_files(self, app_name: str, files: Any) -> list[str]:
        if not isinstance(files, list) or not files:
            raise DashServerError(
                category="tool_validation_error",
                summary="files must be a non-empty array.",
                details={"field": "files"},
            )
        touched: list[str] = []
        for file_entry in files:
            if not isinstance(file_entry, dict):
                raise DashServerError(
                    category="tool_validation_error",
                    summary="Each file entry must be an object.",
                    details={"field": "files"},
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
        if "Exasol dashboard service is not registered on the Flask server" in error_text:
            # PS26-BUG-010: the import-smoke-check sandbox builds a bare Flask server
            # with no `exasol_dashboard_service` extension, so a query run directly in
            # `create_dash_app()` (rather than inside an `@app.callback`) always raises
            # this - regardless of whether the profile/query are actually valid. Left
            # as a generic `import_error`, the message reads exactly like a broken or
            # unbound profile; name the real fix instead.
            return {
                "status": "failed",
                "category": "exasol_query_outside_callback",
                "error": (
                    f"{error_text} This is not a broken Exasol profile - "
                    "load_rows/load_row/query_rows/query_one only work inside an "
                    "@app.callback (validation and the real server both wire the Exasol "
                    "connection onto the server only once request handling starts). Move "
                    "this query into a callback, e.g. one triggered by "
                    "dcc.Interval(max_intervals=1) if it only needs to run once at load."
                ),
                "traceback": traceback_text,
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


_PATTERN_MATCHING_WILDCARDS = ({"ALL"}, {"MATCH"}, {"ALLSMALLER"})


def _looks_like_pattern_matching_id(candidate_id: str) -> bool:
    """Whether a `missing_layout_ids` entry is a serialized Dash wildcard-pattern id.

    Dash's own `callback_map` pre-serializes a pattern-matching component id (e.g.
    ``{"type": "ack-btn", "index": ALL}``) into a canonical JSON string with each
    wildcard sentinel rendered as a single-element list, e.g.
    ``'{"index":["ALL"],"type":"ack-btn"}'`` (confirmed directly against Dash's
    `callback_map` representation) - this is exactly the shape every entry in
    `missing_layout_ids` has, since non-string (dict-shaped) ids are already
    filtered out before this list is built. Used only to decide whether to attach an
    actionable hint - never to change pass/fail status.
    """

    try:
        parsed = json.loads(candidate_id)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    return any(isinstance(value, list) and set(value) in _PATTERN_MATCHING_WILDCARDS for value in parsed.values())
