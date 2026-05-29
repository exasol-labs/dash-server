"""Service layer for draft workspaces and revisioned deployment workflows."""

from __future__ import annotations

import hashlib
import difflib
import importlib.util
import json
import os
import sqlite3
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from dash import Dash
from flask import Flask, got_request_exception, request
from werkzeug.test import Client as WSGIClient
from werkzeug.wrappers import Response as WSGIResponse

from dash_server.dash_apps.demo import build_demo_bundle
from dash_server.dash_apps.callback_isolation import (
    finalize_dash_app_callbacks,
    isolated_dash_callback_globals,
)
from dash_server.dash_apps.branding import apply_hosted_footer
from dash_server.dash_apps.runtime_checks import verify_dash_mount
from dash_server.dash_apps.factory import (
    is_files_bundle_shape,
    validate_bundle,
    validate_manifest_payload,
)
from dash_server.dependencies import DependencyInstaller
from dash_server.diagnostics import DiagnosticsService
from dash_server.exceptions import DashServerError
from dash_server.gitops import GitRepoService, GitWorktreeService
from dash_server.imports import isolated_local_imports
from dash_server.registry.models import AppManifest, AppRevision, HostedApp
from dash_server.registry.sqlite_registry import SQLiteAppRegistry
from dash_server.workspace.service import WorkspaceService

from .dispatcher import DynamicPrefixDispatcher


class AppRuntimeService:
    """Manage hosted apps, draft workspaces, and route mounts in-process."""

    def __init__(
        self,
        registry: SQLiteAppRegistry,
        dispatcher: DynamicPrefixDispatcher,
        artifacts_root: str,
        workspaces_root: str,
        diagnostics_service: DiagnosticsService,
        dependency_installer: DependencyInstaller,
        git_repo_service: GitRepoService,
        git_worktree_service: GitWorktreeService | None = None,
        runtime_extensions: dict[str, Any] | None = None,
        worker_manager: Any | None = None,
        runtime_mode: str = "in_process",
        dependency_environment_service: Any | None = None,
    ) -> None:
        self.registry = registry
        self.dispatcher = dispatcher
        self.artifacts_root = Path(artifacts_root)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.dependency_installer = dependency_installer
        self.git_repo_service = git_repo_service
        self.workspace_service = WorkspaceService(
            workspaces_root,
            dependency_installer=self.dependency_installer.ensure_requirements,
            git_worktree_service=git_worktree_service,
        )
        self.diagnostics_service = diagnostics_service
        self.runtime_extensions = runtime_extensions or {}
        self.worker_manager = worker_manager
        self.runtime_mode = runtime_mode
        self.dependency_environment_service = dependency_environment_service
        if self.worker_manager is not None:
            # Stop the worker whenever the dispatcher unmounts its proxy. The dispatcher's
            # observer hook lets us layer this in without subclassing or monkey-patching.
            self.dispatcher.on_unmount(self._stop_worker_for_mount)

    def _stop_worker_for_mount(self, mount_path: str) -> None:
        """Dispatcher unmount-observer: tear down the worker behind a mount path."""

        manager = self.worker_manager
        if manager is None:
            return
        try:
            manager.stop(mount_path)
        except Exception:
            # Best-effort: worker teardown failures never affect unmount semantics.
            pass

    def ensure_demo_app(self) -> None:
        bundle = build_demo_bundle()
        self.workspace_service.ensure_workspace_from_bundle(bundle, overwrite=False)
        existing_app = self.registry.get_app("demo")
        if existing_app is None:
            app, revision = self._create_app_from_workspace("demo", status="running")
            self.registry.append_event(
                app.name,
                "app_seeded",
                revision_id=revision.id,
                data={"revision_number": revision.revision_number, "status": app.status},
            )
            self.diagnostics_service.record_build_result(
                app.name,
                status="succeeded",
                summary="Seeded built-in demo app.",
                revision_number=revision.revision_number,
                artifact_path=revision.artifact_path,
            )
            self.diagnostics_service.append_log(
                app.name,
                "build",
                "Seeded built-in demo app.",
                revision_number=revision.revision_number,
            )
            return

        current_revision = self.registry.get_current_revision("demo")
        if current_revision is None:
            app, revision = self._create_app_from_workspace("demo", status=existing_app.status)
            self.registry.append_event(
                app.name,
                "app_seeded",
                revision_id=revision.id,
                data={"revision_number": revision.revision_number, "status": app.status},
            )
            self.diagnostics_service.record_build_result(
                app.name,
                status="succeeded",
                summary="Reseeded built-in demo app.",
                revision_number=revision.revision_number,
                artifact_path=revision.artifact_path,
            )

    def bootstrap(self) -> None:
        # In_process mounts are essentially free (microseconds) so the loop stays serial.
        # Isolated mounts wait 1.5–3 s each for their worker's ready event; serialising 10
        # apps means 15–30 s of cold-start latency. Parallelise the per-app work for
        # isolated mode while preserving deterministic route-duplication detection.
        #
        # Phase 4b: before the mount loop, adopt any workers that survived a control-plane
        # restart. Adoption produces fork-shape records the dispatcher can mount the proxy
        # in front of without re-spawning; reaped entries fall through to the normal
        # mount loop, which will re-spawn them.
        adopted_mounts: set[str] = set()
        if self.runtime_mode == "isolated" and self.worker_manager is not None:
            try:
                adoption_results = self.worker_manager.adopt_persisted_workers()
            except Exception:
                adoption_results = {}
            for mount_path, action in adoption_results.items():
                if action == "adopted":
                    adopted_mounts.add(mount_path)
                    # Re-mount the proxy onto the dispatcher so traffic flows to the
                    # adopted worker. We import locally to avoid a circular import at
                    # module load time.
                    from .worker_proxy import WorkerProxyWSGIApp

                    record = self.worker_manager.get_record(mount_path)
                    if record is not None:
                        proxy = WorkerProxyWSGIApp(
                            self.worker_manager,
                            mount_path=mount_path,
                            app_name=record.app_name,
                        )
                        self.dispatcher.mount(mount_path, proxy)

        apps = list(self.registry.list_apps())
        seen_live_routes: set[str] = set()
        # Phase 1: resolve which apps will mount live, in order. This decides the route
        # collision policy serially so behavior matches the legacy loop exactly.
        live_candidates: list[Any] = []
        for app in apps:
            if (
                app.status == "running"
                and app.enabled
                and app.current_revision_number is not None
            ):
                if app.route in seen_live_routes:
                    self.diagnostics_service.record_error(
                        app.name,
                        source="runtime",
                        category="exposure_routing_error",
                        summary=f"Skipped mounting app {app.name} because route {app.route} is duplicated.",
                        details={"app": app.name, "route": app.route},
                    )
                    continue
                seen_live_routes.add(app.route)
                # Phase 4b: skip apps whose worker we just adopted — already mounted.
                if app.route in adopted_mounts:
                    continue
                live_candidates.append(app)

        # Phase 2: do the actual mount work. Parallelise only when isolated mode is on.
        if self.runtime_mode == "isolated" and len(live_candidates) > 1:
            self._bootstrap_mount_parallel(live_candidates, apps)
        else:
            for app in live_candidates:
                self._bootstrap_mount_live_revision(app.name)
            for app in apps:
                if app.preview_revision_number is not None:
                    self._bootstrap_mount_preview_revision(app.name, app.preview_revision_number)

        # Phase 3: workspace seeding is local I/O and stays serial.
        for app in apps:
            if not self.workspace_service.workspace_exists(app.name):
                current_revision = self.registry.get_current_revision(app.name)
                if current_revision is not None:
                    self._seed_workspace_from_revision(app.name, current_revision)

    def _bootstrap_mount_parallel(
        self, live_candidates: list[Any], all_apps: list[Any]
    ) -> None:
        """Spawn live + preview mounts concurrently when running isolated workers."""

        from concurrent.futures import ThreadPoolExecutor

        # Bound concurrency so we don't fork-bomb a small box. Empirical sweet spot is
        # min(cpu_count, app_count, 8) — most servers have well under 8 active apps.
        try:
            cpu_hint = max(1, (os.cpu_count() or 4))
        except Exception:
            cpu_hint = 4
        worker_count = min(len(live_candidates) + 1, cpu_hint, 8)
        worker_count = max(1, worker_count)

        tasks: list[Any] = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for app in live_candidates:
                tasks.append(pool.submit(self._bootstrap_mount_live_revision, app.name))
            for app in all_apps:
                if app.preview_revision_number is not None:
                    tasks.append(
                        pool.submit(
                            self._bootstrap_mount_preview_revision,
                            app.name,
                            app.preview_revision_number,
                        )
                    )
            # Surface the first exception (if any) by joining each task. Failures are
            # already routed into diagnostics by the underlying _bootstrap_* helpers.
            for task in tasks:
                try:
                    task.result()
                except Exception:
                    continue

    def git_desired_state(self) -> dict[str, Any]:
        """Return the parsed Git-backed desired live and preview state."""

        return self.git_repo_service.desired_state()

    def rebuild_cache_from_git(self) -> dict[str, Any]:
        """Reconstruct app and revision cache rows from the authoritative GitOps repository."""

        if not self.git_repo_service.has_commits():
            return {"apps": [], "status": "skipped"}

        desired = self.git_desired_state()
        rebuilt_apps: list[dict[str, Any]] = []
        for app_name in self.git_repo_service.tracked_apps():
            manifest_payload = self.git_repo_service.read_app_manifest(app_name)
            if not isinstance(manifest_payload, dict):
                continue
            try:
                source_manifest = validate_manifest_payload(manifest_payload)
            except DashServerError as exc:
                self._record_dash_server_error(app_name, source="runtime", exc=exc)
                continue

            release_payloads = self.git_repo_service.read_release_manifests(app_name)
            revisions: list[AppRevision] = []
            for release_payload in release_payloads:
                revision = self._upsert_revision_from_release(app_name, source_manifest, release_payload)
                if revision is not None:
                    revisions.append(revision)
            revisions.sort(key=lambda revision: revision.revision_number)
            if not revisions:
                continue

            live_desired = desired["live"].get(app_name)
            preview_desired = desired["preview"].get(app_name)
            current_revision = self._resolve_desired_revision_from_list(revisions, live_desired) or revisions[-1]
            preview_revision = self._resolve_desired_revision_from_list(revisions, preview_desired)
            rollback_revision = self._previous_revision(revisions, current_revision)

            existing_app = self.registry.get_app(app_name)
            status = existing_app.status if existing_app is not None else "running"
            exposure = self._exposure_from_desired_state(
                live_desired,
                fallback_manifest=source_manifest,
                fallback_app=existing_app,
            )

            self.registry.upsert_app_cache(
                name=app_name,
                title=current_revision.manifest.get("title", source_manifest.title),
                route=exposure["route"],
                status=status,
                visibility=exposure["visibility"],
                auth_policy=exposure["auth_policy"],
                enabled=exposure["enabled"],
                permissions=exposure["permissions"],
                current_revision_id=current_revision.id,
                preview_revision_id=preview_revision.id if preview_revision is not None else None,
                rollback_revision_id=rollback_revision.id if rollback_revision is not None else None,
            )

            for revision in revisions:
                lifecycle_state = "archived"
                if revision.id == current_revision.id:
                    lifecycle_state = "live"
                elif preview_revision is not None and revision.id == preview_revision.id:
                    lifecycle_state = "warming"
                self.registry.update_revision_state(
                    revision.id,
                    lifecycle_state,
                    rollout_metadata=revision.rollout_metadata,
                )

            if not self.registry.list_events(app_name):
                revision_map = {revision.revision_number: revision.id for revision in revisions}
                for event_payload in self.git_repo_service.read_history_events(app_name):
                    event_type = event_payload.get("event_type")
                    event_data = event_payload.get("data", {})
                    revision_number = event_payload.get("revision_number")
                    timestamp = event_payload.get("timestamp")
                    if not isinstance(event_type, str) or not isinstance(event_data, dict):
                        continue
                    revision_id = (
                        revision_map.get(revision_number)
                        if isinstance(revision_number, int)
                        else None
                    )
                    self.registry.ensure_event(
                        app_name,
                        event_type,
                        revision_id=revision_id,
                        data=event_data,
                        created_at=timestamp if isinstance(timestamp, str) else None,
                    )
            rebuilt_apps.append(
                {
                    "app": app_name,
                    "revisions": [revision.revision_number for revision in revisions],
                    "current_revision": current_revision.revision_number,
                    "preview_revision": preview_revision.revision_number if preview_revision is not None else None,
                }
            )

        return {"apps": rebuilt_apps, "status": "rebuilt"}

    def git_drift_report(self) -> dict[str, Any]:
        """Return a comparison between desired Git state and the observed runtime state."""

        desired = self.git_desired_state()
        drift: list[dict[str, Any]] = []
        app_names = sorted(
            {
                *[app.name for app in self.registry.list_apps()],
                *desired["live"].keys(),
                *desired["preview"].keys(),
            }
        )
        for app_name in app_names:
            app = self.registry.get_app(app_name)
            live_desired = desired["live"].get(app_name)
            preview_desired = desired["preview"].get(app_name)
            live_status = "missing"
            preview_status = "missing"
            if app is not None and live_desired is not None:
                current_revision = self.registry.get_current_revision(app_name)
                live_target = self._resolve_desired_revision(app_name, live_desired)
                live_status = (
                    "in_sync"
                    if current_revision is not None
                    and live_target is not None
                    and current_revision.id == live_target.id
                    and app.route == live_desired["spec"].get("route")
                    and app.visibility == live_desired["spec"].get("visibility")
                    and app.auth_policy == live_desired["spec"].get("authPolicy")
                    and app.enabled == bool(live_desired["spec"].get("enabled"))
                    else "drifted"
                )
                preview_revision = self.registry.get_preview_revision(app_name)
                preview_target = self._resolve_desired_revision(app_name, preview_desired) if preview_desired is not None else None
                if preview_desired is None:
                    preview_status = "cleared" if preview_revision is None else "drifted"
                else:
                    preview_status = (
                        "in_sync"
                        if preview_revision is not None
                        and preview_target is not None
                        and preview_revision.id == preview_target.id
                        else "drifted"
                    )
            observed_current = self.registry.get_current_revision(app_name) if app is not None else None
            observed_preview = self.registry.get_preview_revision(app_name) if app is not None else None
            drift.append(
                {
                    "app": app_name,
                    "live": {
                        "status": live_status,
                        "desired": live_desired,
                        "observed_revision": (
                            observed_current.revision_number if observed_current is not None else None
                        ),
                    },
                    "preview": {
                        "status": preview_status,
                        "desired": preview_desired,
                        "observed_revision": (
                            observed_preview.revision_number if observed_preview is not None else None
                        ),
                    },
                }
            )
        return {
            "repo": self.git_repo_service.status()["repo"],
            "desired_state": desired,
            "drift": drift,
        }

    def reconcile_git_desired_state(self) -> dict[str, Any]:
        """Apply Git-tracked desired state to the observed runtime and SQLite cache."""

        desired = self.git_desired_state()
        results: list[dict[str, Any]] = []
        desired_live_routes = self._desired_live_routes(desired["live"])
        for app_name in sorted(
            {
                *[app.name for app in self.registry.list_apps()],
                *desired["live"].keys(),
                *desired["preview"].keys(),
            }
        ):
            app = self.registry.get_app(app_name)
            if app is None:
                results.append(
                    {
                        "app": app_name,
                        "status": "skipped",
                        "reason": "app_not_registered",
                    }
                )
                continue
            try:
                result = self._reconcile_app_desired_state(
                    app_name,
                    desired["live"].get(app_name),
                    desired["preview"].get(app_name),
                    desired_live_routes,
                )
            except DashServerError as exc:
                self._record_dash_server_error(app_name, source="runtime", exc=exc)
                result = {
                    "app": app_name,
                    "status": "failed",
                    "reason": exc.category,
                    "details": exc.details,
                }
            results.append(result)
        return {
            "repo": self.git_repo_service.status()["repo"],
            "desired_state": desired,
            "results": results,
        }

    def _bootstrap_mount_live_revision(self, name: str) -> bool:
        try:
            self._mount_live_revision(name)
            return True
        except DashServerError as exc:
            app = self.registry.get_app(name)
            route = app.route if app is not None else None
            self.diagnostics_service.append_log(
                name,
                "runtime",
                "Skipped live mount during bootstrap because the persisted revision failed to load.",
                level="error",
                data={"route": route, "category": exc.category},
            )
            return False

    def _bootstrap_mount_preview_revision(self, name: str, revision_number: int) -> bool:
        try:
            self._mount_preview_revision(name, revision_number)
            return True
        except DashServerError as exc:
            self.diagnostics_service.append_log(
                name,
                "runtime",
                "Skipped preview mount during bootstrap because the persisted revision failed to load.",
                level="error",
                revision_number=revision_number,
                data={"preview_path": self.preview_path(name, revision_number), "category": exc.category},
            )
            return False

    def create_app(self, bundle: Any, start_immediately: bool = True) -> dict[str, Any]:
        if is_files_bundle_shape(bundle):
            manifest, _ = self.workspace_service.ensure_workspace_from_files_bundle(
                bundle,
                overwrite=True,
            )
            self.diagnostics_service.append_log(
                manifest.name,
                "build",
                "Created draft workspace from a files-based app_create bundle.",
                data={"bundle_keys": sorted(bundle.keys()) if isinstance(bundle, dict) else []},
            )
        else:
            manifest, _ = validate_bundle(bundle)
            self.workspace_service.ensure_workspace_from_bundle(bundle, overwrite=True)
        if self.registry.get_app(manifest.name) is not None:
            raise DashServerError(
                category="app_conflict",
                summary=f"App {manifest.name} already exists.",
                details={"app": manifest.name},
                jsonrpc_code=-32001,
                http_status=409,
            )
        self._ensure_route_available(manifest.route)
        try:
            app, revision = self._create_app_from_workspace(
                manifest.name,
                status="running" if start_immediately else "stopped",
            )
        except sqlite3.IntegrityError as exc:
            raise DashServerError(
                category="app_conflict",
                summary=f"App {manifest.name} already exists.",
                details={"app": manifest.name},
                jsonrpc_code=-32001,
                http_status=409,
            ) from exc

        self._write_live_desired_state_for_revision(
            app.name,
            revision,
            commit_message=f"app/{app.name}: create live desired state r{revision.revision_number:06d}",
        )
        self._reconcile_or_raise(app.name)

        self._append_canonical_event(
            app.name,
            "app_created",
            revision_id=revision.id,
            data={"revision_number": revision.revision_number, "status": app.status},
            commit_message=f"app/{app.name}: audit app create r{revision.revision_number:06d}",
        )
        self.diagnostics_service.record_build_result(
            app.name,
            status="succeeded",
            summary="Created initial revision from draft workspace.",
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
        )
        self.diagnostics_service.append_log(
            app.name,
            "build",
            "Created initial revision from draft workspace.",
            revision_number=revision.revision_number,
        )
        return self._serialize_status(app.name)

    def build_revision(
        self,
        name: str,
        bundle: Any | None = None,
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
        app = self._require_app(name)
        if bundle is not None:
            manifest, _ = validate_bundle(bundle)
            self._validate_workspace_identity(app, manifest)
            self.workspace_service.ensure_workspace_from_bundle(bundle, overwrite=True)

        validation = self._safe_workspace_validation(name, force_clean=force_clean)
        if not validation["is_valid"] or validation["requirements"]["invalid"]:
            category = self._classify_validation_category(validation)
            error_record = self.diagnostics_service.record_error(
                name,
                source="build",
                category=category,
                summary="Workspace validation failed; fix the draft before building.",
                details={"validation": validation},
                traceback_text=validation["imports"].get("traceback"),
            )
            self.diagnostics_service.record_build_result(
                name,
                status="failed",
                summary="Workspace validation failed during build.",
                validation=validation,
                error=error_record,
            )
            raise DashServerError(
                category="workspace_validation_error",
                summary="Workspace validation failed; fix the draft before building.",
                details={"app": name, "validation": validation},
                jsonrpc_code=-32007,
                http_status=409,
            )

        manifest = validate_manifest_payload(json.loads(self.workspace_service.read_file(name, "dash-app.json")))
        self._validate_workspace_identity(app, manifest)

        next_revision_number = self.registry.next_revision_number(name)
        artifact_path, source_hash, dependency_lock_hash = self._write_workspace_artifact(
            name,
            next_revision_number,
        )
        git_revision = self._materialize_git_revision(
            name,
            next_revision_number,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            artifact_path=artifact_path,
        )
        revision = self.registry.create_revision(
            name,
            manifest,
            {
                "source_files": self.workspace_service.list_files(name),
                "draft_candidate_version": self.workspace_service.draft_summary(name)["candidate_version"],
            },
            artifact_path=artifact_path,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            commit_sha=git_revision["commit_sha"],
            git_tag=git_revision["git_tag"],
            git_branch=git_revision["git_branch"],
            release_manifest_path=git_revision["release_manifest_path"],
        )
        # Phase 4a: when validation produced a per-app env, persist its identity on the
        # revision row so _mount_revision_isolated doesn't have to recompute it later and
        # Phase 4d GC can join revisions against envs by id.
        dep_install = validation.get("dependency_install") if isinstance(validation, dict) else None
        if isinstance(dep_install, dict):
            env_id = dep_install.get("environment_id")
            env_python = dep_install.get("python_executable")
            if isinstance(env_id, str) and env_id:
                self.registry.update_revision_environment(
                    revision.id,
                    dependency_environment_id=env_id,
                    env_python_executable=env_python or "",
                )
        self.git_repo_service.publish_release_to_main(
            app_name=name,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            source_hash=revision.source_hash,
            dependency_lock_hash=revision.dependency_lock_hash,
            release_manifest_path=revision.release_manifest_path,
        )
        self._append_canonical_event(
            name,
            "revision_built",
            revision_id=revision.id,
            data={"revision_number": revision.revision_number, "artifact_path": revision.artifact_path},
            commit_message=f"app/{name}: audit revision build r{revision.revision_number:06d}",
        )
        preflight = self.preflight_revision(name, revision.revision_number)
        build_error = None
        build_status = "succeeded"
        build_summary = f"Built revision {revision.revision_number}."
        log_level = "info"
        if preflight["preflight"]["status"] != "passed":
            build_error = self.diagnostics_service.record_error(
                name,
                source="build",
                category=self._preflight_failure_category(preflight["preflight"]),
                summary=f"Artifact preflight failed for revision {revision.revision_number}.",
                details={
                    "app": name,
                    "revision_number": revision.revision_number,
                    "preflight": preflight["preflight"],
                },
                traceback_text=self._preflight_traceback_text(preflight["preflight"]),
                revision_number=revision.revision_number,
            )
            build_status = "failed"
            build_summary = (
                f"Built revision {revision.revision_number}, but artifact preflight failed."
            )
            log_level = "error"
            self._append_canonical_event(
                name,
                "revision_preflight_failed",
                revision_id=revision.id,
                data={
                    "revision_number": revision.revision_number,
                    "preflight_status": preflight["preflight"]["status"],
                },
                commit_message=f"app/{name}: audit preflight failure r{revision.revision_number:06d}",
            )
        self.diagnostics_service.record_build_result(
            name,
            status=build_status,
            summary=build_summary,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            validation=validation,
            preflight=preflight["preflight"],
            error=build_error,
        )
        self.diagnostics_service.append_log(
            name,
            "build",
            build_summary,
            revision_number=revision.revision_number,
            level=log_level,
            data={"artifact_path": revision.artifact_path},
        )
        return {
            **self._serialize_revision_details(app, revision),
            "validation": validation,
            "preflight": preflight["preflight"],
            "force_clean": force_clean,
        }

    def put_files(self, name: str, files: list[dict[str, Any]]) -> dict[str, Any]:
        self._require_app(name)
        result = self.workspace_service.put_files(name, files)
        self.registry.append_event(name, "workspace_updated", data=result)
        self.diagnostics_service.append_log(
            name,
            "build",
            "Updated draft files.",
            data=result,
        )
        return self._serialize_workspace(name, result)

    def patch_file(
        self,
        name: str,
        path: str,
        search: str,
        replace: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        self._require_app(name)
        result = self.workspace_service.patch_file(
            name, path, search, replace, replace_all=replace_all
        )
        self.registry.append_event(name, "workspace_patched", data=result)
        self.diagnostics_service.append_log(
            name,
            "build",
            f"Patched draft file {path}.",
            data=result,
        )
        return self._serialize_workspace(name, result)

    def delete_file(self, name: str, path: str) -> dict[str, Any]:
        self._require_app(name)
        result = self.workspace_service.delete_file(name, path)
        self.registry.append_event(name, "workspace_deleted", data=result)
        self.diagnostics_service.append_log(
            name,
            "build",
            f"Deleted draft file {path}.",
            data=result,
        )
        return self._serialize_workspace(name, result)

    def validate_workspace(self, name: str, *, force_clean: bool = False) -> dict[str, Any]:
        self._require_app(name)
        validation = self._safe_workspace_validation(name, force_clean=force_clean)
        self.diagnostics_service.append_log(
            name,
            "build",
            "Validated draft workspace.",
            data={"is_valid": validation["is_valid"], "force_clean": force_clean},
        )
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "draft": self.workspace_service.draft_summary(name),
            "validation": validation,
        }

    def get_app_overview(self, name: str) -> dict[str, Any]:
        return self._serialize_status(name)

    def list_dashboard_catalog(
        self,
        *,
        auth_context: Any | None = None,
        authorization_service: Any | None = None,
    ) -> dict[str, Any]:
        apps = self.registry.list_apps()
        entries = []
        for app in apps:
            discover_decision = None
            preview_decision = None
            if auth_context is not None and authorization_service is not None:
                discover_decision = authorization_service.authorize_app(
                    auth_context,
                    app,
                    "dashboard.discover",
                )
                if not discover_decision.allowed:
                    continue
                preview_decision = authorization_service.authorize_app(
                    auth_context,
                    app,
                    "dashboard.view_preview",
                    target="preview",
                )
            entries.append(
                self._serialize_dashboard_catalog_entry(
                    app,
                    discover_decision=discover_decision,
                    preview_decision=preview_decision,
                )
            )
        entries.sort(key=lambda entry: (entry["status"]["priority"], entry["title"].lower(), entry["name"]))
        return {
            "apps": entries,
            "summary": {
                "total_apps": len(entries),
                "registered_apps": len(apps),
                "published_apps": sum(1 for entry in entries if entry["live"]["published"]),
                "preview_apps": sum(1 for entry in entries if entry["preview"]["visible"]),
                "public_apps": sum(1 for entry in entries if entry["access"]["reason"] == "public_catalog"),
            },
            "viewer": auth_context.to_dict() if auth_context is not None else None,
        }

    def list_workspace_files(self, name: str) -> dict[str, Any]:
        self._require_app(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "draft": self.workspace_service.draft_summary(name),
        }

    def read_workspace_file(self, name: str, path: str) -> dict[str, Any]:
        self._require_app(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "path": path,
            "content": self.workspace_service.read_file(name, path),
            "draft": self.workspace_service.draft_summary(name),
        }

    def diff_workspace(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        current_revision = self._require_current_revision(name)
        return {
            "app": self._serialize_app_row(app),
            **self.workspace_service.diff_against_live(name, current_revision.artifact_path),
        }

    def diff_workspace_against_live_revision(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_current_revision(name)
        return self._workspace_revision_comparison(
            app=app,
            name=name,
            revision=revision,
            target="live",
            revision_key="live",
        )

    def diff_workspace_against_artifact(
        self,
        name: str,
        *,
        revision_number: int | None = None,
    ) -> dict[str, Any]:
        app = self._require_app(name)
        revision = (
            self._require_revision(name, revision_number)
            if revision_number is not None
            else self._latest_revision(name)
        )
        return self._workspace_revision_comparison(
            app=app,
            name=name,
            revision=revision,
            target="latest_build" if revision_number is None else "revision",
            revision_key="artifact",
        )

    def get_latest_artifact_files(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._latest_revision(name)
        artifact_files = self.workspace_service.artifact_files(revision.artifact_path)
        return {
            "app": self._serialize_app_row(app),
            "target": "latest_build",
            "artifact": {
                "revision": self._revision_metadata(revision),
                "source_hash": revision.source_hash or self._files_source_hash(artifact_files),
                "file_count": len(artifact_files),
                "files": sorted(artifact_files.keys()),
            },
        }

    def get_routes(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        preview_revision = self.registry.get_preview_revision(name)
        return {
            "app": self._serialize_app_row(app),
            "routes": {
                "live": {
                    "mount_path": app.route,
                    "enabled": app.enabled,
                    "mounted": self.dispatcher.is_mounted(app.route),
                },
                "preview": (
                    {
                        "mount_path": self.preview_path(name, preview_revision.revision_number),
                        "mounted": self.dispatcher.is_mounted(
                            self.preview_path(name, preview_revision.revision_number)
                        ),
                        "revision_number": preview_revision.revision_number,
                    }
                    if preview_revision is not None
                    else None
                ),
            },
        }

    def get_permissions(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        return {
            "app": self._serialize_app_row(app),
            "permissions": app.permissions,
        }

    def update_route(self, name: str, mount_path: str) -> dict[str, Any]:
        app = self._require_app(name)
        normalized = self._normalize_live_route(mount_path)
        if normalized != app.route:
            self._ensure_route_available(normalized, excluding_app=name)
        revision = self._require_current_revision(name)
        previous_route = app.route
        self._write_live_desired_state_for_revision(
            name,
            revision,
            route=normalized,
            commit_message=f"app/{name}: update live route to {normalized}",
        )
        self._reconcile_or_raise(name)
        self.registry.append_event(
            name,
            "route_updated",
            data={"previous_route": previous_route, "route": normalized},
        )
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Updated live route from {previous_route} to {normalized}.",
            data={"previous_route": previous_route, "route": normalized},
        )
        return self._serialize_status(name)

    def update_visibility(self, name: str, visibility: str) -> dict[str, Any]:
        normalized = self._normalize_visibility(visibility)
        revision = self._require_current_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            revision,
            visibility=normalized,
            commit_message=f"app/{name}: update visibility to {normalized}",
        )
        self._reconcile_or_raise(name)
        self.registry.append_event(name, "visibility_updated", data={"visibility": normalized})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Updated visibility to {normalized}.",
            data={"visibility": normalized},
        )
        return self._serialize_status(name)

    def update_auth_policy(self, name: str, auth_policy: str) -> dict[str, Any]:
        normalized = self._normalize_auth_policy(auth_policy)
        revision = self._require_current_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            revision,
            auth_policy=normalized,
            commit_message=f"app/{name}: update auth policy to {normalized}",
        )
        self._reconcile_or_raise(name)
        self.registry.append_event(name, "auth_policy_updated", data={"auth_policy": normalized})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Updated auth policy to {normalized}.",
            data={"auth_policy": normalized},
        )
        return self._serialize_status(name)

    def update_permissions(self, name: str, permissions: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_permissions(permissions)
        revision = self._require_current_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            revision,
            permissions=normalized,
            commit_message=f"app/{name}: update permissions",
        )
        self._reconcile_or_raise(name)
        self.registry.append_event(name, "permissions_updated", data={"permissions": normalized})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            "Updated app permissions.",
            data={"permissions": normalized},
        )
        return self._serialize_status(name)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_current_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            revision,
            enabled=enabled,
            commit_message=f"app/{name}: {'enable' if enabled else 'disable'} live publication",
        )
        self._reconcile_or_raise(name)
        event_type = "publication_enabled" if enabled else "publication_disabled"
        self.registry.append_event(name, event_type, data={"enabled": enabled, "route": app.route})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            ("Enabled" if enabled else "Disabled") + f" live publication at {app.route}.",
            data={"enabled": enabled, "route": app.route},
        )
        return self._serialize_status(name)

    def run_healthcheck(
        self,
        name: str,
        *,
        target: str = "live",
        record: bool = True,
    ) -> dict[str, Any]:
        app = self._require_app(name)
        if target not in {"live", "preview"}:
            raise DashServerError(
                category="tool_validation_error",
                summary="Healthcheck target must be live or preview.",
                details={"field": "target", "value": target},
                jsonrpc_code=-32602,
            )

        if target == "preview":
            revision = self.registry.get_preview_revision(name)
            if revision is None:
                raise DashServerError(
                    category="preview_unavailable",
                    summary=f"App {name} does not have an active preview revision.",
                    details={"app": name},
                    jsonrpc_code=-32005,
                    http_status=409,
                )
            mount_path = self.preview_path(name, revision.revision_number)
            mounted = self.dispatcher.is_mounted(mount_path)
            probes = [
                {
                    "name": "publication",
                    "status": "passed",
                    "details": {
                        "enabled": True,
                        "visibility": app.visibility,
                        "auth_policy": app.auth_policy,
                        "mount_path": mount_path,
                        "target": "preview",
                    },
                },
                {
                    "name": "process_alive",
                    "status": "passed" if app.status == "running" and mounted else "failed",
                    "details": {
                        "app_status": app.status,
                        "mounted": mounted,
                    },
                },
            ]

            if app.status != "running" or not mounted:
                homepage_probe = self._skipped_probe("http_ready", "Preview revision is not mounted.")
                layout_probe = self._skipped_probe("dash_layout", "Preview revision is not mounted.")
                dependency_probe = self._skipped_probe("dash_dependencies", "Preview revision is not mounted.")
                asset_probe = self._skipped_probe("static_assets", "Preview revision is not mounted.")
                overall_status = "stopped"
            else:
                homepage_probe = self._http_probe(
                    mount_path,
                    probe_name="http_ready",
                    follow_redirects=True,
                )
                layout_probe = self._http_probe(
                    f"{mount_path}/_dash-layout",
                    probe_name="dash_layout",
                    accepted_statuses={200},
                )
                dependency_probe = self._http_probe(
                    f"{mount_path}/_dash-dependencies",
                    probe_name="dash_dependencies",
                    accepted_statuses={200},
                )
                asset_probe = self._asset_probe(mount_path)
                overall_status = (
                    "healthy"
                    if all(probe["status"] == "passed" for probe in [homepage_probe, layout_probe, dependency_probe, asset_probe])
                    else "unhealthy"
                )
        else:
            revision = self._require_current_revision(name)
            mount_path = app.route
            mounted = self.dispatcher.is_mounted(mount_path)

            probes = []
            probes.append(
                {
                    "name": "publication",
                    "status": "passed" if app.enabled else "skipped",
                    "details": {
                        "enabled": app.enabled,
                        "visibility": app.visibility,
                        "auth_policy": app.auth_policy,
                        "mount_path": mount_path,
                        "target": "live",
                    },
                }
            )
            probes.append(
                {
                    "name": "process_alive",
                    "status": "passed" if app.status == "running" else "skipped" if not app.enabled else "failed",
                    "details": {
                        "app_status": app.status,
                        "mounted": mounted,
                    },
                }
            )

            if not app.enabled:
                homepage_probe = self._skipped_probe("http_ready", "Live publication is disabled.")
                layout_probe = self._skipped_probe("dash_layout", "Live publication is disabled.")
                dependency_probe = self._skipped_probe("dash_dependencies", "Live publication is disabled.")
                asset_probe = self._skipped_probe("static_assets", "Live publication is disabled.")
                overall_status = "not_published"
            elif app.status != "running":
                homepage_probe = self._skipped_probe("http_ready", "App runtime is stopped.")
                layout_probe = self._skipped_probe("dash_layout", "App runtime is stopped.")
                dependency_probe = self._skipped_probe("dash_dependencies", "App runtime is stopped.")
                asset_probe = self._skipped_probe("static_assets", "App runtime is stopped.")
                overall_status = "stopped"
            else:
                homepage_probe = self._http_probe(
                    mount_path,
                    probe_name="http_ready",
                    follow_redirects=True,
                )
                layout_probe = self._http_probe(
                    f"{mount_path}/_dash-layout",
                    probe_name="dash_layout",
                    accepted_statuses={200},
                )
                dependency_probe = self._http_probe(
                    f"{mount_path}/_dash-dependencies",
                    probe_name="dash_dependencies",
                    accepted_statuses={200},
                )
                asset_probe = self._asset_probe(mount_path)
                overall_status = (
                    "healthy"
                    if all(probe["status"] == "passed" for probe in [homepage_probe, layout_probe, dependency_probe, asset_probe])
                    else "unhealthy"
                )
        homepage_probe["name"] = "http_ready"
        probes.append(homepage_probe)
        layout_probe["name"] = "dash_layout"
        probes.append(layout_probe)
        dependency_probe["name"] = "dash_dependencies"
        probes.append(dependency_probe)
        asset_probe["name"] = "static_assets"
        probes.append(asset_probe)

        data_layer_probe = self._data_layer_probe(name, revision_number=revision.revision_number)
        probes.append(data_layer_probe)
        if data_layer_probe["status"] == "failed" and overall_status == "healthy":
            overall_status = "degraded"

        # BUG-002 fix: actively smoke-test each `queries/**/*.sql` against the bound
        # profile. Catches broken-but-never-clicked dashboards that the inspect-only
        # `data_layer` probe misses.
        sql_smoke_probe = self._sql_smoke_probe(name, revision)
        probes.append(sql_smoke_probe)
        if sql_smoke_probe["status"] == "failed" and overall_status == "healthy":
            overall_status = "degraded"

        # Phase 3: when running in isolated mode, surface the worker process state alongside
        # the existing HTTP probes. In in_process mode the probe records "not_applicable" so
        # the probe list stays stable across runtime modes.
        worker_probe = self._worker_alive_probe(
            mount_path=mount_path,
            revision_number=revision.revision_number,
        )
        probes.append(worker_probe)
        if worker_probe["status"] == "failed" and overall_status == "healthy":
            overall_status = "degraded"

        # Phase 3.5: worker_http reads the proxy's most recent forwarded-response status code.
        worker_http_probe = self._worker_http_probe(
            mount_path=mount_path,
            revision_number=revision.revision_number,
        )
        probes.append(worker_http_probe)
        if worker_http_probe["status"] == "failed" and overall_status == "healthy":
            overall_status = "degraded"

        payload: dict[str, Any] = {
            "app": self._serialize_app_row(app),
            "revision": self._revision_metadata(revision),
            "target": target,
            "health": {
                "status": overall_status,
                "probes": probes,
            },
        }
        if record:
            result = self.diagnostics_service.record_health_result(name, payload["health"])
            self.diagnostics_service.append_log(
                name,
                "health",
                f"Ran {target} health check with status {overall_status}.",
                revision_number=revision.revision_number,
                data={"target": target, "health": result},
            )
        return payload

    def preflight_revision(self, name: str, revision_number: int) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_revision(name, revision_number)
        mount_path = self._preflight_mount_path(name, revision_number)
        existing_errors = self.diagnostics_service.list_errors(name, limit=0)["errors"]
        existing_error_ids = {
            record["id"]
            for record in existing_errors
            if isinstance(record, dict) and isinstance(record.get("id"), str)
        }
        probes = [
            {
                "name": "artifact_mount",
                "status": "failed",
                "details": {"mount_path": mount_path},
            }
        ]
        error_payload: dict[str, Any] | None = None
        overall_status = "failed"

        try:
            self._mount_revision(revision, mount_path)
            probes[0] = {
                "name": "artifact_mount",
                "status": "passed",
                "details": {"mount_path": mount_path},
            }
            homepage_probe = self._http_probe(
                mount_path,
                probe_name="http_ready",
                follow_redirects=True,
            )
            layout_probe = self._http_probe(
                f"{mount_path}/_dash-layout",
                probe_name="dash_layout",
                accepted_statuses={200},
            )
            dependency_probe = self._http_probe(
                f"{mount_path}/_dash-dependencies",
                probe_name="dash_dependencies",
                accepted_statuses={200},
            )
            asset_probe = self._asset_probe(mount_path)
            probes.extend([homepage_probe, layout_probe, dependency_probe, asset_probe])
            # BUG-003 fix: actively smoke-test the bound profile's queries so an agent
            # can't ship broken SQL to live just because the layout happens to render.
            # `_sql_smoke_probe` returns `not_applicable` when there's no bound profile
            # or no queries/*.sql files, so this doesn't affect non-Exasol apps.
            sql_smoke_probe = self._sql_smoke_probe(name, revision)
            probes.append(sql_smoke_probe)
            overall_status = (
                "passed"
                if all(
                    probe["status"] in ("passed", "not_applicable") for probe in probes
                )
                else "failed"
            )
            if sql_smoke_probe["status"] == "failed":
                # Surface a top-level error_payload so app_validate / app_deploy_draft
                # callers see a clear summary without having to walk the probe list.
                details = sql_smoke_probe.get("details", {})
                failed_file = details.get("first_failed_file") or details.get("profile")
                error_payload = {
                    "category": "artifact_preflight_failed",
                    "summary": (
                        f"SQL smoke-test failed for {failed_file}: "
                        f"{details.get('latest_error') or 'unknown error'}"
                    ),
                    "details": details,
                }
        except DashServerError as exc:
            error_payload = {
                "category": exc.category,
                "summary": exc.summary,
                "details": exc.details,
            }
            probes.extend(
                [
                    self._skipped_probe("http_ready", "Artifact preflight did not finish mounting."),
                    self._skipped_probe("dash_layout", "Artifact preflight did not finish mounting."),
                    self._skipped_probe("dash_dependencies", "Artifact preflight did not finish mounting."),
                    self._skipped_probe("static_assets", "Artifact preflight did not finish mounting."),
                    self._skipped_probe("sql_smoke", "Artifact preflight did not finish mounting."),
                ]
            )
        finally:
            self.dispatcher.unmount(mount_path)

        captured_errors = [
            record
            for record in self.diagnostics_service.list_errors(name, limit=0)["errors"]
            if isinstance(record, dict)
            and record.get("id") not in existing_error_ids
            and record.get("revision_number") == revision.revision_number
        ]
        payload = {
            "app": self._serialize_app_row(app),
            "revision": self._revision_metadata(revision),
            "target": "sandbox",
            "preflight": {
                "status": overall_status,
                "mount_path": mount_path,
                "probes": probes,
                "error": error_payload,
                "captured_errors": captured_errors,
            },
        }
        self.diagnostics_service.append_log(
            name,
            "build",
            f"Ran artifact preflight for revision {revision.revision_number} with status {overall_status}.",
            level="info" if overall_status == "passed" else "error",
            revision_number=revision.revision_number,
            data={"preflight": payload["preflight"]},
        )
        return payload

    def tail_logs(self, name: str, *, channel: str = "latest", limit: int = 20) -> dict[str, Any]:
        self._require_app(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "logs": self.diagnostics_service.tail_logs(name, channel=channel, limit=limit),
        }

    def get_errors(self, name: str, *, limit: int = 20) -> dict[str, Any]:
        self._require_app(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            **self.diagnostics_service.list_errors(name, limit=limit),
        }

    def get_callback_failures(self, name: str, *, limit: int = 20) -> dict[str, Any]:
        self._require_app(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            **self.diagnostics_service.list_callback_failures(name, limit=limit),
        }

    def get_dependency_report(self, name: str) -> dict[str, Any]:
        validation = self._safe_workspace_validation(name)
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "dependency_report": {
                "declared_requirements": validation["requirements"]["entries"],
                "invalid_requirements": validation["requirements"]["invalid"],
                "install_plan": validation["dependency_install"],
            },
        }

    def collect_diagnostics(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        validation = self._safe_workspace_validation(name)
        current_revision = self._require_current_revision(name)
        latest_built_revision = self._latest_revision(name)
        latest_error = self.diagnostics_service.latest_error(
            name,
            revision_number=current_revision.revision_number,
        )
        latest_runtime_error = self.diagnostics_service.latest_error(name, source="runtime")
        # BUG-004 fix: surface data-layer errors here so the canonical diagnostics tool
        # doesn't disagree with `dash://apps/{name}/errors`. Until BUG-005 lands the
        # revision-stamping, data-layer records have `revision_number: null` and would
        # be filtered out of the main `latest_error` query above — we read this stream
        # explicitly so they're never invisible.
        latest_data_layer_error = self.diagnostics_service.latest_error(name, source="data_layer")
        latest_build_result = self.diagnostics_service.get_latest_build_result(name)
        latest_build_error = None
        if isinstance(latest_build_result, dict) and latest_build_result.get("status") == "failed":
            build_revision_number = latest_build_result.get("revision_number")
            latest_build_error = self.diagnostics_service.latest_error(
                name,
                source="build",
                revision_number=build_revision_number if isinstance(build_revision_number, int) else None,
            )
        health = self.run_healthcheck(name, record=False)["health"]
        rollback_revision = self.registry.get_rollback_revision(name)
        logs = {
            "latest": self.diagnostics_service.tail_logs(name, channel="latest", limit=20)["entries"],
            "build": self.diagnostics_service.tail_logs(name, channel="build", limit=20)["entries"],
            "runtime": self.diagnostics_service.tail_logs(name, channel="runtime", limit=20)["entries"],
        }
        draft_vs_latest_build = self.diff_workspace_against_artifact(name)
        draft_vs_live = self.diff_workspace_against_live_revision(name)

        # `latest_data_layer_error` is included in the recovery cascade so a dashboard
        # that's only failing at the SQL surface still gets actionable advice (the
        # `exasol_query_error` recovery category lives in DiagnosticsService).
        error_for_recovery = latest_error or latest_build_error or latest_data_layer_error
        error_for_comparison = error_for_recovery or latest_runtime_error
        if error_for_recovery is not None:
            category = error_for_recovery["category"]
            parsed_traceback = error_for_recovery.get("parsed_traceback")
        else:
            category = "none"
            parsed_traceback = None
        artifact_comparison = self._artifact_comparison_report(
            name,
            traceback=(
                error_for_comparison.get("parsed_traceback")
                if isinstance(error_for_comparison, dict)
                else None
            ),
            error_record=error_for_comparison if isinstance(error_for_comparison, dict) else None,
        )
        recovery_category = (
            "artifact_mismatch"
            if artifact_comparison["hints"]
            and error_for_recovery is not None
            and artifact_comparison["source_context"] != "current_draft"
            else category
        )

        return {
            "app": self._serialize_app_row(app),
            "lifecycle": self.get_app_status(name),
            "latest_build_result": latest_build_result,
            "latest_built_revision": self._revision_metadata(latest_built_revision),
            "logs": logs,
            "parsed_traceback": parsed_traceback,
            "callback_failure_summary": self.diagnostics_service.list_callback_failures(name, limit=10),
            "health": health,
            "manifest_validation_report": validation,
            "dependency_report": {
                "declared_requirements": validation["requirements"]["entries"],
                "invalid_requirements": validation["requirements"]["invalid"],
                "install_plan": validation["dependency_install"],
            },
            "last_known_good_revision": self._revision_metadata(
                rollback_revision or current_revision
            ),
            "latest_error": latest_error,
            "latest_runtime_error": latest_runtime_error,
            "latest_build_error": latest_build_error,
            "latest_data_layer_error": latest_data_layer_error,
            "draft_vs_latest_build": draft_vs_latest_build,
            "draft_vs_live": draft_vs_live,
            "artifact_comparison": artifact_comparison,
            "suggested_recovery_steps": self.diagnostics_service.suggested_recovery_steps(recovery_category),
        }

    def inspect_traceback(self, name: str, traceback_text: str | None = None) -> dict[str, Any]:
        self._require_app(name)
        latest_error: dict[str, Any] | None = None
        if traceback_text is None:
            current_revision = self.registry.get_current_revision(name)
            if current_revision is not None:
                latest_error = self.diagnostics_service.latest_error(
                    name,
                    revision_number=current_revision.revision_number,
                )
            if latest_error is None:
                latest_build = self.diagnostics_service.get_latest_build_result(name)
                if isinstance(latest_build, dict) and latest_build.get("status") == "failed":
                    build_revision_number = latest_build.get("revision_number")
                    latest_error = self.diagnostics_service.latest_error(
                        name,
                        source="build",
                        revision_number=build_revision_number if isinstance(build_revision_number, int) else None,
                    )
            if latest_error is None:
                raise DashServerError(
                    category="diagnostics_not_found",
                    summary=f"App {name} does not have a relevant traceback for the current revision or latest failed build.",
                    details={"app": name},
                    jsonrpc_code=-32004,
                    http_status=404,
                )
            if latest_error.get("traceback_text"):
                traceback_text = latest_error["traceback_text"]
            elif latest_error.get("parsed_traceback") is not None:
                inspected = latest_error["parsed_traceback"]
                comparison = self._artifact_comparison_report(
                    name,
                    traceback=inspected,
                    error_record=latest_error,
                )
                recovery_category = (
                    "artifact_mismatch"
                    if comparison["hints"]
                    else inspected["category"]
                )
                return {
                    "app": self._serialize_app_row(self._require_app(name)),
                    "traceback": inspected,
                    "artifact_comparison": comparison,
                    "artifact_guidance": comparison["hints"],
                    "suggested_recovery_steps": self.diagnostics_service.suggested_recovery_steps(
                        recovery_category
                    ),
                }
            else:
                raise DashServerError(
                    category="diagnostics_not_found",
                    summary=f"App {name} does not have a traceback payload to inspect.",
                    details={"app": name},
                    jsonrpc_code=-32004,
                    http_status=404,
                )

        inspected = self.diagnostics_service.inspect_traceback(traceback_text)["traceback"]
        comparison = self._artifact_comparison_report(
            name,
            traceback=inspected,
            error_record=latest_error,
        )
        recovery_category = "artifact_mismatch" if comparison["hints"] else inspected["category"]
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "traceback": inspected,
            "artifact_comparison": comparison,
            "artifact_guidance": comparison["hints"],
            "suggested_recovery_steps": self.diagnostics_service.suggested_recovery_steps(
                recovery_category
            ),
        }

    def start_preview(self, name: str, revision_number: int) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_revision(name, revision_number)
        preview_path = self.preview_path(name, revision.revision_number)
        self._write_preview_desired_state_for_revision(
            name,
            revision,
            commit_message=f"app/{name}: start preview r{revision.revision_number:06d}",
        )
        self._reconcile_or_raise(name)
        self._append_canonical_event(
            name,
            "preview_started",
            revision_id=revision.id,
            data={"revision_number": revision.revision_number, "preview_path": preview_path},
            commit_message=f"app/{name}: audit preview start r{revision.revision_number:06d}",
        )
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Started preview for revision {revision.revision_number}.",
            revision_number=revision.revision_number,
            data={"preview_path": preview_path},
        )
        return self._serialize_status(app.name)

    def promote_revision(self, name: str, revision_number: int) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_revision(name, revision_number)
        previous_current = self.registry.get_current_revision(name)
        previous_preview = self.registry.get_preview_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            revision,
            clear_preview=True,
            commit_message=f"app/{name}: promote r{revision.revision_number:06d} to live",
        )
        self._reconcile_or_raise(name)
        if previous_preview is not None:
            self._append_canonical_event(
                name,
                "preview_cleared",
                revision_id=previous_preview.id,
                data={"revision_number": previous_preview.revision_number, "reason": "promote"},
                commit_message=f"app/{name}: audit preview clear after promote",
            )
        self._append_canonical_event(
            name,
            "revision_promoted",
            revision_id=revision.id,
            data={
                "revision_number": revision.revision_number,
                "previous_revision_number": (
                    previous_current.revision_number if previous_current is not None else None
                ),
            },
            commit_message=f"app/{name}: audit promotion r{revision.revision_number:06d}",
        )
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Promoted revision {revision.revision_number} to live.",
            revision_number=revision.revision_number,
            data={"route": app.route},
        )
        return self._serialize_status(app.name)

    def rollback(self, name: str) -> dict[str, Any]:
        self._require_app(name)  # raises if the app doesn't exist
        rollback_revision = self.registry.get_rollback_revision(name)
        if rollback_revision is None:
            raise DashServerError(
                category="rollback_unavailable",
                summary=f"App {name} does not have a rollback target.",
                details={"app": name},
                jsonrpc_code=-32005,
                http_status=409,
            )

        current_revision = self.registry.get_current_revision(name)
        previous_preview = self.registry.get_preview_revision(name)
        self._write_live_desired_state_for_revision(
            name,
            rollback_revision,
            clear_preview=True,
            commit_message=f"app/{name}: rollback live to r{rollback_revision.revision_number:06d}",
        )
        self._reconcile_or_raise(name)
        if previous_preview is not None:
            self._append_canonical_event(
                name,
                "preview_cleared",
                revision_id=previous_preview.id,
                data={"revision_number": previous_preview.revision_number, "reason": "rollback"},
                commit_message=f"app/{name}: audit preview clear after rollback",
            )
        self._append_canonical_event(
            name,
            "rolled_back",
            revision_id=rollback_revision.id,
            data={
                "revision_number": rollback_revision.revision_number,
                "previous_revision_number": (
                    current_revision.revision_number if current_revision is not None else None
                ),
            },
            commit_message=f"app/{name}: audit rollback r{rollback_revision.revision_number:06d}",
        )
        self.diagnostics_service.append_log(
            name,
            "runtime",
            f"Rolled back to revision {rollback_revision.revision_number}.",
            revision_number=rollback_revision.revision_number,
        )
        return self._serialize_status(name)

    def start_app(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        if app.status != "running":
            self.registry.set_status(name, "running")
        if app.enabled:
            self._mount_live_revision(name)
        self.registry.append_event(name, "app_started", data={"published": app.enabled})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            "Started app runtime." if app.enabled else "Started app runtime without live publication.",
            data={"published": app.enabled},
        )
        return self._serialize_status(name)

    def stop_app(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        if app.status != "stopped":
            self.registry.set_status(name, "stopped")
        self.dispatcher.unmount(app.route)
        self.registry.append_event(name, "app_stopped", data={})
        self.diagnostics_service.append_log(name, "runtime", "Stopped app runtime.")
        return self._serialize_status(name)

    def restart_app(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        self.dispatcher.unmount(app.route)
        self.registry.set_status(name, "running")
        if app.enabled:
            self._mount_live_revision(name)
        self.registry.append_event(name, "app_restarted", data={"published": app.enabled})
        self.diagnostics_service.append_log(
            name,
            "runtime",
            "Restarted app runtime." if app.enabled else "Restarted app runtime without live publication.",
            data={"published": app.enabled},
        )
        return self._serialize_status(name)

    def get_app_status(self, name: str) -> dict[str, Any]:
        return self._serialize_status(name)

    def get_manifest(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_current_revision(name)
        return {
            "app": app.to_dict(),
            "manifest": revision.manifest,
            "exposure": app.exposure.to_dict(),
            "revision": self._revision_metadata(revision),
            "desired_state": {
                "live": self.git_desired_state()["live"].get(name),
                "preview": self.git_desired_state()["preview"].get(name),
            },
        }

    def list_revisions(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        revisions = [self._revision_payload(revision) for revision in self.registry.list_revisions(name)]
        return {"app": app.to_dict(), "revisions": revisions}

    def get_revision_details(self, name: str, revision_number: int) -> dict[str, Any]:
        app = self._require_app(name)
        revision = self._require_revision(name, revision_number)
        return {"app": app.to_dict(), "revision": self._revision_payload(revision)}

    def list_events(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        events = [event.to_dict() for event in self.registry.list_events(name)]
        return {"app": app.to_dict(), "events": events}

    def list_apps(self) -> list[dict[str, Any]]:
        return [self._serialize_app_row(app) for app in self.registry.list_apps()]

    def preview_path(self, app_name: str, revision_number: int) -> str:
        return f"/preview/{app_name}/{revision_number}"

    def _serialize_status(self, name: str) -> dict[str, Any]:
        app = self._require_app(name)
        current_revision = self._require_current_revision(name)
        preview_revision = self.registry.get_preview_revision(name)
        rollback_revision = self.registry.get_rollback_revision(name)
        desired_state = self.git_desired_state()
        return {
            "app": self._serialize_app_row(app),
            "current_revision": self._revision_metadata(current_revision),
            "preview_revision": (
                self._revision_metadata(preview_revision) if preview_revision is not None else None
            ),
            "rollback_revision": (
                self._revision_metadata(rollback_revision) if rollback_revision is not None else None
            ),
            "draft": self.workspace_service.draft_summary(name),
            "exposure": app.exposure.to_dict(),
            "gitops": {
                "desired_live": desired_state["live"].get(name),
                "desired_preview": desired_state["preview"].get(name),
            },
            "runtime": {
                "mounted": self.dispatcher.is_mounted(app.route),
                "preview_mounted": (
                    self.dispatcher.is_mounted(self.preview_path(name, preview_revision.revision_number))
                    if preview_revision is not None
                    else False
                ),
                "published": app.enabled and self.dispatcher.is_mounted(app.route),
                "isolation": self._runtime_isolation_snapshot(),
            },
        }

    def _runtime_isolation_snapshot(self) -> dict[str, Any]:
        """Return a small read-only snapshot of the runtime-isolation flags."""

        try:
            from flask import current_app, has_app_context

            if not has_app_context():
                return {}
            config = current_app.config
        except Exception:
            return {}
        return {
            "dependency_isolation": config.get("APP_DEPENDENCY_ISOLATION", "shared"),
            "runtime_mode": config.get("APP_RUNTIME_MODE", "in_process"),
        }

    def _serialize_workspace(self, name: str, operation_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "app": self._serialize_app_row(self._require_app(name)),
            "draft": self.workspace_service.draft_summary(name),
            "operation": operation_result,
        }

    def _serialize_app_row(self, app: HostedApp) -> dict[str, Any]:
        payload = app.to_dict()
        payload["mounted"] = self.dispatcher.is_mounted(app.route)
        payload["draft_candidate_version"] = self.workspace_service.draft_summary(app.name)["candidate_version"]
        payload["published"] = app.enabled and payload["mounted"]
        payload["exposure"] = app.exposure.to_dict()
        if app.preview_revision_number is not None:
            payload["preview_path"] = self.preview_path(app.name, app.preview_revision_number)
            payload["preview_mounted"] = self.dispatcher.is_mounted(payload["preview_path"])
        else:
            payload["preview_path"] = None
            payload["preview_mounted"] = False
        return payload

    def _serialize_dashboard_catalog_entry(
        self,
        app: HostedApp,
        *,
        discover_decision: Any | None = None,
        preview_decision: Any | None = None,
    ) -> dict[str, Any]:
        app_row = self._serialize_app_row(app)
        current_revision = self.registry.get_current_revision(app.name)
        preview_revision = self.registry.get_preview_revision(app.name)
        manifest = self._dashboard_catalog_manifest(current_revision, preview_revision)
        live = {
            "path": app.route,
            "enabled": app.enabled,
            "mounted": app_row["mounted"],
            "published": app_row["published"],
            "revision_number": app.current_revision_number,
        }
        preview_available = bool(app_row["preview_path"] and app_row["preview_mounted"])
        preview_visible = preview_available and (
            preview_decision is None or bool(preview_decision.allowed)
        )
        preview = {
            "path": app_row["preview_path"],
            "available": preview_available,
            "visible": preview_visible,
            "mounted": app_row["preview_mounted"],
            "revision_number": app.preview_revision_number,
        }
        status = self._dashboard_catalog_status(app, live=live, preview=preview)
        access = (
            discover_decision.to_dict()
            if discover_decision is not None
            else {
                "allowed": True,
                "reason": "local_unfiltered",
                "capability": "dashboard.discover",
                "matched_grant": None,
                "matched_policy": None,
            }
        )
        return {
            "name": app.name,
            "title": manifest.get("title", app.title),
            "description": manifest.get("description") or f"{app.title} dashboard.",
            "template": manifest.get("template"),
            "route": app.route,
            "status": status,
            "visibility": app.visibility,
            "auth_policy": app.auth_policy,
            "live": live,
            "preview": preview,
            "draft_candidate_version": app_row["draft_candidate_version"],
            "access": access,
        }

    def _dashboard_catalog_manifest(
        self,
        current_revision: AppRevision | None,
        preview_revision: AppRevision | None,
    ) -> dict[str, Any]:
        if current_revision is not None and isinstance(current_revision.manifest, dict):
            return current_revision.manifest
        if preview_revision is not None and isinstance(preview_revision.manifest, dict):
            return preview_revision.manifest
        return {}

    def _dashboard_catalog_status(
        self,
        app: HostedApp,
        *,
        live: dict[str, Any],
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        if live["published"] and preview["visible"]:
            return {
                "label": "Live + Preview",
                "detail": "Published now, with a newer preview revision available for review.",
                "tone": "success",
                "priority": 0,
            }
        if live["published"]:
            return {
                "label": "Live",
                "detail": "Published and reachable on the primary app route.",
                "tone": "success",
                "priority": 1,
            }
        if preview["visible"]:
            return {
                "label": "Preview Only",
                "detail": "A preview revision is available, but the live route is not currently published.",
                "tone": "warning",
                "priority": 2,
            }
        if app.enabled and app.status == "running":
            return {
                "label": "Needs Attention",
                "detail": "Expected to be live, but the runtime is not mounted on the public route.",
                "tone": "danger",
                "priority": 3,
            }
        return {
            "label": "Offline",
            "detail": "Not currently published for outside users.",
            "tone": "muted",
            "priority": 4,
        }

    def _serialize_revision_details(self, app: HostedApp, revision: AppRevision) -> dict[str, Any]:
        return {"app": self._serialize_app_row(app), "revision": self._revision_payload(revision)}

    def _revision_payload(self, revision: AppRevision) -> dict[str, Any]:
        payload = revision.to_dict()
        payload["preview_path"] = self.preview_path(revision.app_name, revision.revision_number)
        return payload

    def _revision_metadata(self, revision: AppRevision) -> dict[str, Any]:
        return {
            "id": revision.id,
            "revision_number": revision.revision_number,
            "lifecycle_state": revision.lifecycle_state,
            "artifact_path": revision.artifact_path,
            "source_hash": revision.source_hash,
            "dependency_lock_hash": revision.dependency_lock_hash,
            "commit_sha": revision.commit_sha,
            "git_tag": revision.git_tag,
            "git_branch": revision.git_branch,
            "release_manifest_path": revision.release_manifest_path,
            "created_at": revision.created_at,
        }

    def _require_app(self, name: str) -> HostedApp:
        app = self.registry.get_app(name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {name} was not found.",
                details={"app": name},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return app

    def _require_current_revision(self, name: str) -> AppRevision:
        revision = self.registry.get_current_revision(name)
        if revision is None:
            raise DashServerError(
                category="revision_not_found",
                summary=f"App {name} does not have a current revision.",
                details={"app": name},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return revision

    def _latest_revision(self, name: str) -> AppRevision:
        revisions = self.registry.list_revisions(name)
        if not revisions:
            raise DashServerError(
                category="revision_not_found",
                summary=f"App {name} does not have any built revisions.",
                details={"app": name},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return revisions[-1]

    def _files_source_hash(self, files: dict[str, str]) -> str:
        payload = json.dumps(files, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _workspace_revision_comparison(
        self,
        *,
        app: HostedApp,
        name: str,
        revision: AppRevision,
        target: str,
        revision_key: str,
    ) -> dict[str, Any]:
        draft_files = self.workspace_service.read_all_files(name)
        artifact_files = self.workspace_service.artifact_files(revision.artifact_path)
        comparison = self._compare_file_sets(
            draft_files=draft_files,
            artifact_files=artifact_files,
            artifact_label=f"{revision_key}/r{revision.revision_number:06d}",
        )
        return {
            "app": self._serialize_app_row(app),
            "target": target,
            "draft": {
                "candidate_version": self.workspace_service.draft_summary(name)["candidate_version"],
                "source_hash": self._files_source_hash(draft_files),
                "file_count": len(draft_files),
            },
            revision_key: {
                "revision": self._revision_metadata(revision),
                "source_hash": revision.source_hash or self._files_source_hash(artifact_files),
                "file_count": len(artifact_files),
            },
            **comparison,
        }

    def _compare_file_sets(
        self,
        *,
        draft_files: dict[str, str],
        artifact_files: dict[str, str],
        artifact_label: str,
    ) -> dict[str, Any]:
        file_summaries: list[dict[str, Any]] = []
        diff_lines: list[str] = []
        for relative_path in sorted(set(draft_files) | set(artifact_files)):
            draft_text = draft_files.get(relative_path)
            artifact_text = artifact_files.get(relative_path)
            if draft_text is None:
                status = "artifact_only"
            elif artifact_text is None:
                status = "draft_only"
            elif draft_text == artifact_text:
                status = "unchanged"
            else:
                status = "changed"
            file_summaries.append(
                {
                    "path": relative_path,
                    "status": status,
                    "draft_bytes": len(draft_text.encode("utf-8")) if isinstance(draft_text, str) else 0,
                    "artifact_bytes": len(artifact_text.encode("utf-8")) if isinstance(artifact_text, str) else 0,
                }
            )
            diff_lines.extend(
                difflib.unified_diff(
                    (artifact_text or "").splitlines(keepends=True),
                    (draft_text or "").splitlines(keepends=True),
                    fromfile=f"{artifact_label}/{relative_path}",
                    tofile=f"draft/{relative_path}",
                )
            )
        return {
            "files": file_summaries,
            "diff": "".join(diff_lines),
        }

    def _artifact_comparison_report(
        self,
        name: str,
        *,
        traceback: dict[str, Any] | None,
        error_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_revision = self._require_current_revision(name)
        latest_built_revision = self._latest_revision(name)
        draft_vs_latest_build = self.diff_workspace_against_artifact(name)
        draft_vs_live = self.diff_workspace_against_live_revision(name)
        draft_files = self.workspace_service.read_all_files(name)
        latest_built_files = self.workspace_service.artifact_files(latest_built_revision.artifact_path)
        live_files = self.workspace_service.artifact_files(current_revision.artifact_path)
        focused_path, source_context = self._focused_traceback_path(
            name,
            traceback,
            current_revision=current_revision,
            latest_built_revision=latest_built_revision,
            draft_files=draft_files,
        )
        focused_file = None
        hints: list[str] = []
        if focused_path is not None:
            focused_file = {
                "path": focused_path,
                "source_context": source_context,
                "draft_vs_latest_build": self._file_state(
                    draft_files.get(focused_path), latest_built_files.get(focused_path)
                ),
                "draft_vs_live": self._file_state(
                    draft_files.get(focused_path), live_files.get(focused_path)
                ),
                "live_vs_latest_build": self._file_state(
                    live_files.get(focused_path), latest_built_files.get(focused_path)
                ),
                "exists_in_draft": focused_path in draft_files,
                "exists_in_latest_build": focused_path in latest_built_files,
                "exists_in_live": focused_path in live_files,
            }
            hints = self._artifact_mismatch_hints(
                focused_file=focused_file,
                latest_built_revision=latest_built_revision,
                current_revision=current_revision,
            )
        return {
            "source_context": self._comparison_source_context(
                error_record,
                source_context=source_context,
                current_revision=current_revision,
                latest_built_revision=latest_built_revision,
            ),
            "focused_file": focused_file,
            "latest_built_revision": self._revision_metadata(latest_built_revision),
            "live_revision": self._revision_metadata(current_revision),
            "draft_vs_latest_build_has_changes": any(
                entry.get("status") != "unchanged"
                for entry in draft_vs_latest_build["files"]
            ),
            "draft_vs_live_has_changes": any(
                entry.get("status") != "unchanged"
                for entry in draft_vs_live["files"]
            ),
            "hints": hints,
        }

    def _focused_traceback_path(
        self,
        name: str,
        traceback: dict[str, Any] | None,
        *,
        current_revision: AppRevision,
        latest_built_revision: AppRevision,
        draft_files: dict[str, str],
    ) -> tuple[str | None, str]:
        if not isinstance(traceback, dict):
            return None, "unknown"
        frames = traceback.get("frames")
        if not isinstance(frames, list):
            return None, "unknown"
        workspace_path = Path(self.workspace_service.workspace_location(name)["workspace_path"])
        current_artifact_path = Path(current_revision.artifact_path)
        latest_artifact_path = Path(latest_built_revision.artifact_path)
        for frame in reversed(frames):
            if not isinstance(frame, dict):
                continue
            file_value = frame.get("file")
            if not isinstance(file_value, str) or not file_value:
                continue
            frame_path = Path(file_value)
            for source_context, root_path in (
                ("current_draft", workspace_path),
                ("current_live_revision", current_artifact_path),
                ("latest_built_artifact", latest_artifact_path),
            ):
                try:
                    relative = frame_path.resolve().relative_to(root_path.resolve())
                    return relative.as_posix(), source_context
                except (ValueError, FileNotFoundError):
                    continue
            if frame_path.name in {Path(path).name for path in draft_files}:
                matches = [path for path in draft_files if Path(path).name == frame_path.name]
                if len(matches) == 1:
                    return matches[0], "unknown"
        return None, "unknown"

    def _comparison_source_context(
        self,
        error_record: dict[str, Any] | None,
        *,
        source_context: str,
        current_revision: AppRevision,
        latest_built_revision: AppRevision,
    ) -> str:
        if source_context != "unknown":
            return source_context
        if not isinstance(error_record, dict):
            return "unknown"
        source = error_record.get("source")
        revision_number = error_record.get("revision_number")
        if source == "build":
            return "latest_built_artifact"
        if source == "runtime" and revision_number == current_revision.revision_number:
            return "current_live_revision"
        if source == "runtime" and revision_number == latest_built_revision.revision_number:
            return "latest_built_artifact"
        return "unknown"

    def _file_state(self, left: str | None, right: str | None) -> str:
        if left is None and right is None:
            return "missing"
        if left is None:
            return "missing_left"
        if right is None:
            return "missing_right"
        return "same" if left == right else "different"

    def _artifact_mismatch_hints(
        self,
        *,
        focused_file: dict[str, Any],
        latest_built_revision: AppRevision,
        current_revision: AppRevision,
    ) -> list[str]:
        path = focused_file["path"]
        hints: list[str] = []
        if focused_file["draft_vs_latest_build"] == "different":
            hints.append(
                f"The draft now contains newer content for {path} than the latest built artifact."
            )
        elif focused_file["draft_vs_latest_build"] == "missing_left":
            hints.append(
                f"The latest built artifact still contains {path}, but the current draft does not."
            )
        elif focused_file["draft_vs_latest_build"] == "missing_right":
            hints.append(
                f"The current draft contains {path}, but the latest built artifact does not."
            )

        if (
            current_revision.revision_number != latest_built_revision.revision_number
            and focused_file["live_vs_latest_build"] == "different"
        ):
            hints.append(
                f"The live revision and latest built artifact differ for {path}; verify which revision the traceback came from before patching."
            )
        if (
            focused_file["source_context"] == "current_live_revision"
            and focused_file["draft_vs_live"] == "different"
        ):
            hints.append(
                f"The traceback points at the live revision, but the draft has already diverged for {path}."
            )
        if (
            focused_file["source_context"] == "latest_built_artifact"
            and focused_file["draft_vs_latest_build"] == "different"
        ):
            hints.append(
                f"The traceback points at the latest built artifact, not the current draft, for {path}."
            )
        return hints

    def _require_revision(self, name: str, revision_number: int) -> AppRevision:
        revision = self.registry.get_revision_by_number(name, revision_number)
        if revision is None:
            raise DashServerError(
                category="revision_not_found",
                summary=f"Revision {revision_number} for app {name} was not found.",
                details={"app": name, "revision_number": revision_number},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return revision

    def _create_app_from_workspace(self, app_name: str, *, status: str) -> tuple[HostedApp, AppRevision]:
        validation = self._safe_workspace_validation(app_name)
        if not validation["is_valid"] or validation["requirements"]["invalid"]:
            category = self._classify_validation_category(validation)
            error_record = self.diagnostics_service.record_error(
                app_name,
                source="build",
                category=category,
                summary="Workspace validation failed during app creation.",
                details={"validation": validation},
                traceback_text=validation["imports"].get("traceback"),
            )
            self.diagnostics_service.record_build_result(
                app_name,
                status="failed",
                summary="Workspace validation failed during app creation.",
                validation=validation,
                error=error_record,
            )
            raise DashServerError(
                category="workspace_validation_error",
                summary="Workspace validation failed during app creation.",
                details={"app": app_name, "validation": validation},
                jsonrpc_code=-32007,
                http_status=409,
            )

        manifest = validate_manifest_payload(json.loads(self.workspace_service.read_file(app_name, "dash-app.json")))
        artifact_path, source_hash, dependency_lock_hash = self._write_workspace_artifact(app_name, 1)
        git_revision = self._materialize_git_revision(
            app_name,
            1,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            artifact_path=artifact_path,
        )
        return self.registry.create_app(
            manifest,
            {
                "source_files": self.workspace_service.list_files(app_name),
                "draft_candidate_version": self.workspace_service.draft_summary(app_name)["candidate_version"],
            },
            status=status,
            artifact_path=artifact_path,
            source_hash=source_hash,
            dependency_lock_hash=dependency_lock_hash,
            commit_sha=git_revision["commit_sha"],
            git_tag=git_revision["git_tag"],
            git_branch=git_revision["git_branch"],
            release_manifest_path=git_revision["release_manifest_path"],
        )

    def _mount_live_revision(self, name: str) -> None:
        app = self._require_app(name)
        revision = self._require_current_revision(name)
        if not app.enabled:
            self.dispatcher.unmount(app.route)
            return
        self._mount_revision(revision, app.route)

    def _mount_preview_revision(self, name: str, revision_number: int) -> None:
        revision = self._require_revision(name, revision_number)
        self._mount_revision(revision, self.preview_path(name, revision_number))

    def _mount_revision(self, revision: AppRevision, mount_path: str) -> None:
        try:
            if self.runtime_mode == "isolated" and self.worker_manager is not None:
                self._mount_revision_isolated(revision, mount_path)
            else:
                wsgi_app = self._create_revision_wsgi_app(revision, mount_path)
                self.dispatcher.mount(mount_path, wsgi_app)
            self.diagnostics_service.append_log(
                revision.app_name,
                "runtime",
                f"Mounted revision {revision.revision_number} at {mount_path}.",
                revision_number=revision.revision_number,
                data={"runtime_mode": self.runtime_mode},
            )
        except DashServerError as exc:
            self._record_dash_server_error(
                revision.app_name,
                source="runtime",
                exc=exc,
                revision_number=revision.revision_number,
            )
            raise
        except Exception as exc:
            wrapped = DashServerError(
                category="runtime_mount_error",
                summary=f"Failed to mount revision {revision.revision_number} for app {revision.app_name}.",
                details={
                    "app": revision.app_name,
                    "revision_number": revision.revision_number,
                    "mount_path": mount_path,
                    "traceback_text": traceback.format_exc(),
                    "exception_type": type(exc).__name__,
                },
                jsonrpc_code=-32008,
                http_status=500,
            )
            self._record_dash_server_error(
                revision.app_name,
                source="runtime",
                exc=wrapped,
                revision_number=revision.revision_number,
            )
            raise wrapped from exc

    def _mount_revision_isolated(self, revision: AppRevision, mount_path: str) -> None:
        """Phase 3 isolated path: spawn a worker, mount the proxy in front of it.

        The artifact must contain a usable ``app.py``. Metadata-only ``app_create`` bundles
        (the ``metric-cards`` template) don't ship a ``app.py`` until they're rendered;
        for those we still fall back to in-process serving for now. A follow-up will render
        the scaffold into the artifact directory at build time so isolated mode works there too.
        """

        from .worker_proxy import WorkerProxyWSGIApp

        # Isolated mounts only happen when the manager is wired in; assert so mypy can
        # narrow `self.worker_manager` from `Any | None` to a concrete manager for the
        # rest of the function body.
        assert self.worker_manager is not None, "isolated mount without worker_manager"
        from .worker_manager import WorkerStartError

        artifact_path = Path(revision.artifact_path)
        app_source = artifact_path / "app.py"
        if not (artifact_path.is_dir() and app_source.exists()):
            # Fall back to in-process for revisions whose artifact has no app.py on disk.
            wsgi_app = self._create_revision_wsgi_app(revision, mount_path)
            self.dispatcher.mount(mount_path, wsgi_app)
            return

        # Resolve the worker's python_executable. Prefer the env id stored on the revision
        # row (recorded at build time); fall back to recomputing from requirements.txt only
        # when the build never wrote one (older revisions or builds that ran before the
        # dependency-environment service was wired in).
        python_executable: str | None = None
        environment_id: str | None = None
        if self.dependency_environment_service is not None:
            env_id = revision.dependency_environment_id
            stored_python = revision.env_python_executable
            if env_id and stored_python:
                # Fast path: trust the stored identity. lookup() still tells us if the env
                # was evicted from disk so we can fall through to recompute and rebuild.
                env_record = self.dependency_environment_service.lookup(env_id)
                if env_record is not None and isinstance(env_record.get("python_executable"), str):
                    python_executable = env_record["python_executable"]
                    environment_id = env_id
                else:
                    # Env was GC'd or never materialized. Fall through to recompute path.
                    env_id = ""
            if not environment_id:
                requirements = self._read_requirements_from_artifact(artifact_path)
                try:
                    env_id_computed = self.dependency_environment_service.compute_environment_id(
                        requirements
                    )
                    env_record = self.dependency_environment_service.lookup(env_id_computed)
                except Exception:
                    env_record = None
                    env_id_computed = None
                if env_record is not None and isinstance(env_record.get("python_executable"), str):
                    python_executable = env_record["python_executable"]
                    environment_id = env_id_computed
                    # Backfill the revision row so subsequent mounts hit the fast path.
                    try:
                        self.registry.update_revision_environment(
                            revision.id,
                            dependency_environment_id=env_id_computed,
                            env_python_executable=python_executable,
                        )
                    except Exception:
                        pass

        manifest = revision.manifest or {}

        try:
            self.worker_manager.start(
                app_name=revision.app_name,
                revision_number=revision.revision_number,
                mount_path=mount_path,
                app_source=app_source,
                manifest=manifest,
                python_executable=python_executable,
                environment_id=environment_id,
            )
        except WorkerStartError as exc:
            raise DashServerError(
                category="runtime_mount_error",
                summary=(
                    f"Failed to start isolated worker for {revision.app_name} "
                    f"revision {revision.revision_number}."
                ),
                details={
                    "app": revision.app_name,
                    "revision_number": revision.revision_number,
                    "mount_path": mount_path,
                    "worker_start_error": str(exc),
                    "python_executable": python_executable or sys.executable,
                },
                jsonrpc_code=-32008,
                http_status=500,
            ) from exc

        proxy = WorkerProxyWSGIApp(
            self.worker_manager,
            mount_path=mount_path,
            app_name=revision.app_name,
        )
        self.dispatcher.mount(mount_path, proxy)

    def _read_requirements_from_artifact(self, artifact_path: Path) -> list[str]:
        path = artifact_path / "requirements.txt"
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _create_revision_wsgi_app(self, revision: AppRevision, mount_path: str) -> Flask:
        artifact_path = Path(revision.artifact_path)
        if artifact_path.is_dir() and (artifact_path / "app.py").exists():
            wsgi_app = self._load_workspace_artifact_wsgi_app(
                artifact_path,
                mount_path,
                revision_number=revision.revision_number,
            )
        else:
            manifest, dashboard = validate_bundle(
                {"manifest": revision.manifest, "dashboard": revision.bundle}
            )
            from dash_server.dash_apps.factory import build_dash_wsgi_app

            wsgi_app = build_dash_wsgi_app(
                manifest,
                dashboard,
                mount_prefix=mount_path,
                revision_number=revision.revision_number,
            )
        self._instrument_runtime_wsgi_app(
            wsgi_app,
            app_name=revision.app_name,
            revision_number=revision.revision_number,
        )
        return wsgi_app

    def _instrument_runtime_wsgi_app(
        self,
        wsgi_app: Any,
        *,
        app_name: str,
        revision_number: int,
    ) -> None:
        if not isinstance(wsgi_app, Flask):
            return
        if getattr(wsgi_app, "_dash_server_diagnostics_instrumented", False):
            return

        def handle_runtime_exception(sender, exception, **extra):
            traceback_text = "".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            )
            path = request.path
            if path.endswith("/_dash-update-component"):
                callback_payload = request.get_json(silent=True)
                details = {
                    "path": path,
                    "method": request.method,
                    **self._callback_request_details(callback_payload),
                }
                summary = self._callback_failure_summary(details)
                self.diagnostics_service.record_callback_failure(
                    app_name,
                    summary=summary,
                    details=details,
                    traceback_text=traceback_text,
                    revision_number=revision_number,
                )
                return

            category = self.diagnostics_service.inspect_traceback(traceback_text)["traceback"][
                "category"
            ]
            summary = f"Unhandled runtime exception while serving {path}."
            self.diagnostics_service.record_error(
                app_name,
                source="runtime",
                category=category,
                summary=summary,
                details={
                    "path": path,
                    "method": request.method,
                },
                traceback_text=traceback_text,
                revision_number=revision_number,
            )

        got_request_exception.connect(handle_runtime_exception, wsgi_app, weak=False)
        # Idempotency marker (mirrored read via `getattr` at the top of this function).
        # Flask doesn't declare arbitrary attributes; this is the documented sentinel pattern.
        wsgi_app._dash_server_diagnostics_instrumented = True  # type: ignore[attr-defined]

    def _callback_request_details(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "output": None,
                "outputs": None,
                "changed_prop_ids": [],
                "inputs": [],
                "state": [],
            }
        return {
            "output": payload.get("output"),
            "outputs": payload.get("outputs"),
            "changed_prop_ids": payload.get("changedPropIds") or [],
            "inputs": payload.get("inputs") or [],
            "state": payload.get("state") or [],
        }

    def _callback_failure_summary(self, details: dict[str, Any]) -> str:
        output = details.get("output")
        if isinstance(output, str) and output:
            return f"Dash callback failed for output {output}."
        changed = details.get("changed_prop_ids")
        if isinstance(changed, list) and changed:
            return f"Dash callback failed after input change {changed[0]}."
        return "Dash callback failed during callback dispatch."

    def _load_workspace_artifact_wsgi_app(
        self,
        artifact_dir: Path,
        mount_path: str,
        *,
        revision_number: int,
    ) -> Flask:
        manifest_payload = json.loads((artifact_dir / "dash-app.json").read_text())
        manifest = validate_manifest_payload(manifest_payload)
        module_name = f"dash_server_artifact_{manifest.name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, artifact_dir / "app.py")
        assert spec is not None and spec.loader is not None
        with isolated_dash_callback_globals(), isolated_local_imports(artifact_dir):
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                raise DashServerError(
                    category="runtime_mount_error",
                    summary="Failed to import artifact app.py during runtime mount.",
                    details={
                        "artifact_path": str(artifact_dir),
                        "exception_type": type(exc).__name__,
                        "traceback_text": traceback.format_exc(),
                    },
                    jsonrpc_code=-32008,
                    http_status=500,
                ) from exc
            factory = getattr(module, "create_dash_app", None)
            if not callable(factory):
                raise DashServerError(
                    category="artifact_error",
                    summary="Artifact app.py must define create_dash_app(server, url_base_pathname, metadata).",
                    details={"artifact_path": str(artifact_dir)},
                    jsonrpc_code=-32008,
                    http_status=500,
                )
            server = Flask(f"dash_server.runtime.{manifest.name}.{uuid.uuid4().hex}")
            server.extensions.update(self.runtime_extensions)
            try:
                created = factory(
                    server=server,
                    url_base_pathname=f"{mount_path.rstrip('/')}/",
                    # `revision_number` lets the runtime helper stamp data-layer errors
                    # with the active revision, so the data_layer probe + errors resource
                    # can filter old failures out after promote/rollback. (BUG-005)
                    metadata={
                        **manifest.to_dict(),
                        "route": mount_path,
                        "revision_number": revision_number,
                    },
                )
                if isinstance(created, Dash):
                    apply_hosted_footer(
                        created,
                        mount_path=mount_path,
                        revision_number=revision_number,
                    )
                    finalize_dash_app_callbacks(created)
            except Exception as exc:
                raise DashServerError(
                    category="runtime_mount_error",
                    summary="Artifact factory raised an exception during runtime mount.",
                    details={
                        "artifact_path": str(artifact_dir),
                        "exception_type": type(exc).__name__,
                        "traceback_text": traceback.format_exc(),
                    },
                    jsonrpc_code=-32008,
                    http_status=500,
                ) from exc
            mount_check = verify_dash_mount(server)
            if mount_check["status"] != "passed":
                raise DashServerError(
                    category="runtime_mount_error",
                    summary="Artifact app did not serve the mounted Dash routes.",
                    details={
                        "artifact_path": str(artifact_dir),
                        "mount_path": mount_path,
                        "mount_check": mount_check,
                    },
                    jsonrpc_code=-32008,
                    http_status=500,
                )
        return server

    def _validate_workspace_identity(self, app: HostedApp, manifest: AppManifest) -> None:
        if manifest.name != app.name:
            raise DashServerError(
                category="build_validation_error",
                summary="Draft manifest name must match the existing app name.",
                details={"app": app.name, "manifest_name": manifest.name},
                jsonrpc_code=-32602,
            )

    def _seed_workspace_from_revision(self, app_name: str, revision: AppRevision) -> None:
        artifact_path = Path(revision.artifact_path)
        if artifact_path.is_dir():
            if self.workspace_service.workspace_exists(app_name):
                return
            files: dict[str, str] = {}
            for source in artifact_path.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
                    files[source.relative_to(artifact_path).as_posix()] = source.read_text()
            self.workspace_service.replace_workspace(
                app_name,
                files,
                candidate_version=revision.revision_number,
            )
            return

        bundle = {
            "manifest": revision.manifest,
            "dashboard": revision.bundle,
        }
        self.workspace_service.ensure_workspace_from_bundle(bundle, overwrite=False)

    def _write_workspace_artifact(self, app_name: str, revision_number: int) -> tuple[str, str, str]:
        artifact_dir = self.artifacts_root / app_name / f"r{revision_number:06d}"
        self.workspace_service.snapshot_workspace(app_name, artifact_dir)
        files = self.workspace_service.read_all_files(app_name)
        source_payload = json.dumps(files, sort_keys=True).encode("utf-8")
        requirements = self.workspace_service.read_file(app_name, "requirements.txt").encode("utf-8")
        source_hash = hashlib.sha256(source_payload).hexdigest()
        dependency_lock_hash = hashlib.sha256(requirements).hexdigest()
        return str(artifact_dir), source_hash, dependency_lock_hash

    def _materialize_git_revision(
        self,
        app_name: str,
        revision_number: int,
        *,
        source_hash: str,
        dependency_lock_hash: str,
        artifact_path: str,
    ) -> dict[str, str]:
        location = self.workspace_service.workspace_location(app_name)
        if location["storage_backend"] != "git_worktree" or not self.git_repo_service.has_commits():
            return {
                "commit_sha": "",
                "git_tag": "",
                "git_branch": "",
                "release_manifest_path": "",
            }
        try:
            return self.git_repo_service.materialize_revision(
                app_name=app_name,
                revision_number=revision_number,
                workspace_path=location["workspace_path"],
                source_hash=source_hash,
                dependency_lock_hash=dependency_lock_hash,
                artifact_path=artifact_path,
            )
        except ValueError:
            return {
                "commit_sha": "",
                "git_tag": "",
                "git_branch": "",
                "release_manifest_path": "",
            }

    def backfill_revision_git_metadata(self) -> None:
        for app in self.registry.list_apps():
            current_revision = self.registry.get_current_revision(app.name)
            for revision in self.registry.list_revisions(app.name):
                if revision.commit_sha and revision.git_tag and revision.release_manifest_path:
                    self.git_repo_service.publish_release_to_main(
                        app_name=app.name,
                        revision_number=revision.revision_number,
                        artifact_path=revision.artifact_path,
                        commit_sha=revision.commit_sha,
                        git_tag=revision.git_tag,
                        source_hash=revision.source_hash,
                        dependency_lock_hash=revision.dependency_lock_hash,
                        release_manifest_path=revision.release_manifest_path,
                    )
                    continue

                if current_revision is not None and revision.id == current_revision.id:
                    git_revision = self._materialize_git_revision(
                        app.name,
                        revision.revision_number,
                        source_hash=revision.source_hash,
                        dependency_lock_hash=revision.dependency_lock_hash,
                        artifact_path=revision.artifact_path,
                    )
                else:
                    base_commit = self.git_repo_service.head_commit()
                    if not base_commit:
                        continue
                    git_tag = revision.git_tag or self.git_repo_service.ensure_release_tag(
                        app.name,
                        revision.revision_number,
                        base_commit,
                    )
                    release_manifest_path = (
                        revision.release_manifest_path
                        or self.git_repo_service.release_manifest_path(app.name, revision.revision_number)
                    )
                    self.git_repo_service.publish_release_to_main(
                        app_name=app.name,
                        revision_number=revision.revision_number,
                        artifact_path=revision.artifact_path,
                        commit_sha=base_commit,
                        git_tag=git_tag,
                        source_hash=revision.source_hash,
                        dependency_lock_hash=revision.dependency_lock_hash,
                        release_manifest_path=release_manifest_path,
                    )
                    git_revision = {
                        "commit_sha": base_commit,
                        "git_tag": git_tag,
                        "git_branch": revision.git_branch or "main",
                        "release_manifest_path": release_manifest_path,
                    }

                self.registry.update_revision_git_metadata(
                    revision.id,
                    commit_sha=git_revision["commit_sha"],
                    git_tag=git_revision["git_tag"],
                    git_branch=git_revision["git_branch"],
                    release_manifest_path=git_revision["release_manifest_path"],
                )
            self._backfill_canonical_history(app.name)

    def _append_canonical_event(
        self,
        app_name: str,
        event_type: str,
        *,
        revision_id: int | None = None,
        data: dict[str, Any] | None = None,
        commit_message: str,
    ) -> None:
        payload = data or {}
        event = self.registry.append_event(
            app_name,
            event_type,
            revision_id=revision_id,
            data=payload,
        )
        revision_number = payload.get("revision_number")
        resolved_revision_number = revision_number if isinstance(revision_number, int) else None
        if resolved_revision_number is None and revision_id is not None:
            revision = self.registry.get_revision_by_pointer(app_name, "current_revision_id")
            if revision is not None and revision.id == revision_id:
                resolved_revision_number = revision.revision_number
            else:
                for candidate in self.registry.list_revisions(app_name):
                    if candidate.id == revision_id:
                        resolved_revision_number = candidate.revision_number
                        break
        self.git_repo_service.append_history_event(
            app_name=app_name,
            event_type=event_type,
            revision_number=resolved_revision_number,
            data=payload,
            commit_message=commit_message,
            timestamp=event.created_at,
        )

    def _backfill_canonical_history(self, app_name: str) -> None:
        canonical_types = {
            "app_seeded",
            "app_created",
            "revision_built",
            "preview_started",
            "preview_cleared",
            "revision_promoted",
            "rolled_back",
        }
        existing = {
            (
                event.get("event_type"),
                event.get("revision_number"),
                json.dumps(event.get("data", {}), sort_keys=True),
            )
            for event in self.git_repo_service.read_history_events(app_name)
            if isinstance(event, dict)
        }
        revision_number_by_id = {
            revision.id: revision.revision_number for revision in self.registry.list_revisions(app_name)
        }
        for event in self.registry.list_events(app_name):
            if event.event_type not in canonical_types:
                continue
            revision_number = event.data.get("revision_number")
            if not isinstance(revision_number, int) and event.revision_id is not None:
                revision_number = revision_number_by_id.get(event.revision_id)
            marker = (
                event.event_type,
                revision_number,
                json.dumps(event.data, sort_keys=True),
            )
            if marker in existing:
                continue
            self.git_repo_service.append_history_event(
                app_name=app_name,
                event_type=event.event_type,
                revision_number=revision_number,
                data=event.data,
                commit_message=f"app/{app_name}: backfill audit event {event.event_type}",
                timestamp=event.created_at,
            )
            existing.add(marker)

    def _write_live_desired_state_for_revision(
        self,
        app_name: str,
        revision: AppRevision,
        *,
        route: str | None = None,
        visibility: str | None = None,
        auth_policy: str | None = None,
        enabled: bool | None = None,
        permissions: dict[str, Any] | None = None,
        clear_preview: bool = False,
        commit_message: str,
    ) -> None:
        app = self._require_app(app_name)
        self.git_repo_service.publish_revision_to_main(
            app_name=app_name,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            source_hash=revision.source_hash,
            dependency_lock_hash=revision.dependency_lock_hash,
            release_manifest_path=revision.release_manifest_path,
        )
        self.git_repo_service.write_live_desired_state(
            app_name=app_name,
            revision_number=revision.revision_number,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            release_manifest_path=revision.release_manifest_path,
            route=route or app.route,
            visibility=visibility or app.visibility,
            auth_policy=auth_policy or app.auth_policy,
            enabled=app.enabled if enabled is None else enabled,
            permissions=permissions or app.permissions,
            clear_preview=clear_preview,
            commit_message=commit_message,
        )

    def _write_preview_desired_state_for_revision(
        self,
        app_name: str,
        revision: AppRevision,
        *,
        commit_message: str,
    ) -> None:
        self.git_repo_service.publish_revision_to_main(
            app_name=app_name,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            source_hash=revision.source_hash,
            dependency_lock_hash=revision.dependency_lock_hash,
            release_manifest_path=revision.release_manifest_path,
        )
        self.git_repo_service.write_preview_desired_state(
            app_name=app_name,
            revision_number=revision.revision_number,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            release_manifest_path=revision.release_manifest_path,
            commit_message=commit_message,
        )

    def _desired_live_routes(self, desired_live: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
        routes: dict[str, list[str]] = {}
        for app_name, desired in desired_live.items():
            spec = desired.get("spec")
            if not isinstance(spec, dict):
                continue
            route = spec.get("route")
            if isinstance(route, str) and route:
                routes.setdefault(route, []).append(app_name)
        return routes

    def _reconcile_app_desired_state(
        self,
        app_name: str,
        live_desired: dict[str, Any] | None,
        preview_desired: dict[str, Any] | None,
        desired_live_routes: dict[str, list[str]],
    ) -> dict[str, Any]:
        app = self._require_app(app_name)
        previous_route = app.route
        if live_desired is not None:
            spec = live_desired.get("spec")
            if not isinstance(spec, dict):
                raise DashServerError(
                    category="gitops_reconcile_error",
                    summary=f"Live desired state for app {app_name} is malformed.",
                    details={"app": app_name, "desired_state": live_desired},
                    jsonrpc_code=-32010,
                )
            route = self._normalize_live_route(str(spec.get("route", app.route)))
            if len(desired_live_routes.get(route, [])) > 1:
                raise DashServerError(
                    category="route_conflict",
                    summary=f"Live desired state uses duplicate route {route}.",
                    details={"app": app_name, "route": route, "apps": desired_live_routes[route]},
                    jsonrpc_code=-32009,
                )
            self._ensure_route_available(route, excluding_app=app_name)
            visibility = self._normalize_visibility(str(spec.get("visibility", app.visibility)))
            auth_policy = self._normalize_auth_policy(str(spec.get("authPolicy", app.auth_policy)))
            enabled = bool(spec.get("enabled", app.enabled))
            spec_permissions = spec.get("permissions")
            permissions = self._normalize_permissions(
                spec_permissions if isinstance(spec_permissions, dict) else app.permissions
            )
            updated = self.registry.update_exposure(
                app_name,
                route=route,
                visibility=visibility,
                auth_policy=auth_policy,
                enabled=enabled,
                permissions=permissions,
            )
            assert updated is not None
            app = updated
            desired_live_revision = self._resolve_desired_revision(app_name, live_desired)
            if desired_live_revision is not None and (
                app.current_revision_id != desired_live_revision.id
            ):
                previous_current = self.registry.get_current_revision(app_name)
                self.registry.promote_revision(app_name, desired_live_revision.id)
                self.registry.update_revision_state(
                    desired_live_revision.id,
                    "live",
                    rollout_metadata={"reconciled_to": app.route},
                )
                if previous_current is not None and previous_current.id != desired_live_revision.id:
                    self.registry.update_revision_state(
                        previous_current.id,
                        "archived",
                        rollout_metadata={"replaced_by": desired_live_revision.revision_number},
                    )
                app = self._require_app(app_name)
            if previous_route != app.route and self.dispatcher.is_mounted(previous_route):
                self.dispatcher.unmount(previous_route)
            if app.enabled and app.status == "running":
                self._mount_live_revision(app_name)
            else:
                self.dispatcher.unmount(app.route)
        else:
            desired_live_revision = None

        if preview_desired is not None:
            desired_preview_revision = self._resolve_desired_revision(app_name, preview_desired)
            if desired_preview_revision is None:
                raise DashServerError(
                    category="gitops_reconcile_error",
                    summary=f"Preview desired state for app {app_name} could not be resolved.",
                    details={"app": app_name, "desired_state": preview_desired},
                    jsonrpc_code=-32010,
                )
            self.registry.set_preview_revision(app_name, desired_preview_revision.id)
            self.registry.update_revision_state(
                desired_preview_revision.id,
                "warming",
                rollout_metadata={"preview_path": self.preview_path(app_name, desired_preview_revision.revision_number)},
            )
            self._mount_preview_revision(app_name, desired_preview_revision.revision_number)
        else:
            existing_preview = self.registry.get_preview_revision(app_name)
            if existing_preview is not None:
                self.dispatcher.unmount(self.preview_path(app_name, existing_preview.revision_number))
                self.registry.set_preview_revision(app_name, None)
            desired_preview_revision = None

        return {
            "app": app_name,
            "status": "reconciled",
            "live_revision": desired_live_revision.revision_number if live_desired is not None and desired_live_revision is not None else None,
            "preview_revision": desired_preview_revision.revision_number if desired_preview_revision is not None else None,
            "route": self._require_app(app_name).route,
        }

    def _resolve_desired_revision(
        self,
        app_name: str,
        desired_state: dict[str, Any] | None,
    ) -> AppRevision | None:
        if desired_state is None:
            return None
        spec = desired_state.get("spec")
        if not isinstance(spec, dict):
            return None
        target_revision = spec.get("targetRevision")
        if not isinstance(target_revision, str) or not target_revision.startswith("r"):
            return None
        try:
            revision_number = int(target_revision[1:])
        except ValueError:
            return None
        revision = self.registry.get_revision_by_number(app_name, revision_number)
        if revision is None:
            return None
        git_tag = spec.get("gitTag")
        if isinstance(git_tag, str) and git_tag and revision.git_tag and git_tag != revision.git_tag:
            raise DashServerError(
                category="gitops_reconcile_error",
                summary=f"Desired state for app {app_name} references a mismatched git tag.",
                details={
                    "app": app_name,
                    "target_revision": target_revision,
                    "desired_git_tag": git_tag,
                    "observed_git_tag": revision.git_tag,
                },
                jsonrpc_code=-32010,
            )
        return revision

    def _resolve_desired_revision_from_list(
        self,
        revisions: list[AppRevision],
        desired_state: dict[str, Any] | None,
    ) -> AppRevision | None:
        if desired_state is None:
            return None
        spec = desired_state.get("spec")
        if not isinstance(spec, dict):
            return None
        target_revision = spec.get("targetRevision")
        if not isinstance(target_revision, str) or not target_revision.startswith("r"):
            return None
        try:
            revision_number = int(target_revision[1:])
        except ValueError:
            return None
        for revision in revisions:
            if revision.revision_number == revision_number:
                return revision
        return None

    def _previous_revision(
        self,
        revisions: list[AppRevision],
        current_revision: AppRevision,
    ) -> AppRevision | None:
        earlier = [revision for revision in revisions if revision.revision_number < current_revision.revision_number]
        return earlier[-1] if earlier else None

    def _exposure_from_desired_state(
        self,
        desired_state: dict[str, Any] | None,
        *,
        fallback_manifest: AppManifest,
        fallback_app: HostedApp | None,
    ) -> dict[str, Any]:
        spec = desired_state.get("spec") if isinstance(desired_state, dict) else None
        permissions = (
            fallback_app.permissions
            if fallback_app is not None
            else {
                "filesystem": {"mode": "workspace-write"},
                "network": {"mode": "inherit"},
                "env": {"mode": "inherit"},
            }
        )
        return {
            "route": str(spec.get("route", fallback_app.route if fallback_app is not None else fallback_manifest.route))
            if isinstance(spec, dict)
            else (fallback_app.route if fallback_app is not None else fallback_manifest.route),
            "visibility": str(spec.get("visibility", fallback_app.visibility if fallback_app is not None else "private"))
            if isinstance(spec, dict)
            else (fallback_app.visibility if fallback_app is not None else "private"),
            "auth_policy": str(spec.get("authPolicy", fallback_app.auth_policy if fallback_app is not None else "inherited"))
            if isinstance(spec, dict)
            else (fallback_app.auth_policy if fallback_app is not None else "inherited"),
            "enabled": bool(spec.get("enabled", fallback_app.enabled if fallback_app is not None else True))
            if isinstance(spec, dict)
            else (fallback_app.enabled if fallback_app is not None else True),
            "permissions": self._normalize_permissions(spec.get("permissions", permissions))
            if isinstance(spec, dict) and isinstance(spec.get("permissions", permissions), dict)
            else permissions,
        }

    def _upsert_revision_from_release(
        self,
        app_name: str,
        fallback_manifest: AppManifest,
        release_payload: dict[str, Any],
    ) -> AppRevision | None:
        metadata = release_payload.get("metadata")
        spec = release_payload.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            return None
        revision_label = metadata.get("revision")
        if not isinstance(revision_label, str) or not revision_label.startswith("r"):
            return None
        try:
            revision_number = int(revision_label[1:])
        except ValueError:
            return None
        existing = self.registry.get_revision_by_number(app_name, revision_number)
        artifact_path = str(spec.get("artifactPath", ""))
        revision_manifest = self._manifest_from_artifact_or_fallback(artifact_path, fallback_manifest)
        return self.registry.upsert_revision_cache(
            app_name,
            revision_number=revision_number,
            manifest=revision_manifest.to_dict(),
            bundle={
                "source_files": self._artifact_source_files(artifact_path),
                "reconstructed_from": "git_cache_rebuild",
            },
            lifecycle_state="archived",
            artifact_path=artifact_path,
            source_hash=self._strip_hash_prefix(str(spec.get("manifestHash", ""))),
            dependency_lock_hash=self._strip_hash_prefix(str(spec.get("dependencyLockHash", ""))),
            commit_sha=str(spec.get("commit", "")),
            git_tag=str(spec.get("gitTag", "")),
            git_branch=existing.git_branch if existing is not None and existing.git_branch else "main",
            release_manifest_path=str(release_payload.get("path", "")),
            rollout_metadata={"reconstructed_from": "git_cache_rebuild"},
        )

    def _manifest_from_artifact_or_fallback(
        self,
        artifact_path: str,
        fallback_manifest: AppManifest,
    ) -> AppManifest:
        artifact_manifest_path = Path(artifact_path) / "dash-app.json"
        if artifact_manifest_path.exists():
            return validate_manifest_payload(json.loads(artifact_manifest_path.read_text()))
        return fallback_manifest

    def _artifact_source_files(self, artifact_path: str) -> list[str]:
        artifact_dir = Path(artifact_path)
        if not artifact_dir.exists() or not artifact_dir.is_dir():
            return []
        files: list[str] = []
        for candidate in sorted(artifact_dir.rglob("*")):
            if candidate.is_file() and "__pycache__" not in candidate.parts and candidate.suffix != ".pyc":
                files.append(candidate.relative_to(artifact_dir).as_posix())
        return files

    def _strip_hash_prefix(self, value: str) -> str:
        return value.split("sha256:", 1)[1] if value.startswith("sha256:") else value

    def _reconcile_or_raise(self, app_name: str) -> dict[str, Any]:
        summary = self.reconcile_git_desired_state()
        result = next((item for item in summary["results"] if item["app"] == app_name), None)
        if result is None:
            raise DashServerError(
                category="gitops_reconcile_error",
                summary=f"Git desired-state reconcile did not return a result for app {app_name}.",
                details={"app": app_name, "summary": summary},
                jsonrpc_code=-32010,
            )
        if result.get("status") == "failed":
            raise DashServerError(
                category=str(result.get("reason") or "gitops_reconcile_error"),
                summary=f"Git desired-state reconcile failed for app {app_name}.",
                details={"app": app_name, **(result.get("details") or {})},
                jsonrpc_code=-32010,
            )
        return summary

    def _safe_workspace_validation(
        self,
        app_name: str,
        *,
        force_clean: bool = False,
    ) -> dict[str, Any]:
        try:
            app = self.registry.get_app(app_name)
            mount_path = app.route if app is not None else None
            return self.workspace_service.validate_workspace(
                app_name,
                mount_path=mount_path,
                force_clean=force_clean,
            )
        except DashServerError as exc:
            self._record_dash_server_error(app_name, source="build", exc=exc)
            raise

    def _classify_validation_category(self, validation: dict[str, Any]) -> str:
        if validation["syntax"]["status"] == "failed":
            return "syntax_error"
        if validation["dependency_install"]["status"] == "failed":
            return "environment_missing_dependency"
        if validation.get("callbacks", {}).get("status") == "failed":
            return "callback_validation_error"
        if validation["imports"].get("category") == "environment_missing_dependency":
            return "environment_missing_dependency"
        if validation["imports"].get("category") == "route_misconfiguration":
            return "route_misconfiguration"
        if validation["imports"]["status"] == "failed":
            return "import_error"
        if validation["requirements"]["invalid"]:
            return "dependency_conflict"
        return "manifest_error"

    def _record_dash_server_error(
        self,
        app_name: str,
        *,
        source: str,
        exc: DashServerError,
        revision_number: int | None = None,
    ) -> dict[str, Any]:
        category = self._diagnostic_category_for_error(exc)
        return self.diagnostics_service.record_error(
            app_name,
            source=source,
            category=category,
            summary=exc.summary,
            details=exc.details,
            traceback_text=exc.details.get("traceback_text"),
            revision_number=revision_number,
        )

    def _diagnostic_category_for_error(self, exc: DashServerError) -> str:
        if exc.category == "workspace_validation_error":
            validation = exc.details.get("validation")
            if isinstance(validation, dict):
                return self._classify_validation_category(validation)
            return "manifest_error"
        if exc.category in {"build_validation_error"}:
            return "manifest_error"
        if exc.category in {"route_conflict", "exposure_validation_error"}:
            return "exposure_routing_error"
        if exc.category in {"artifact_error", "runtime_mount_error"}:
            mount_check = exc.details.get("mount_check")
            if isinstance(mount_check, dict) and mount_check.get("category") == "route_misconfiguration":
                return "route_misconfiguration"
            traceback_text = exc.details.get("traceback_text")
            if isinstance(traceback_text, str) and traceback_text.strip():
                return self.diagnostics_service.inspect_traceback(traceback_text)["traceback"]["category"]
            return "runtime_crash"
        if exc.category == "workspace_constraint_error":
            return "permission_violation"
        return "runtime_crash"

    def _http_probe(
        self,
        path: str,
        *,
        probe_name: str,
        follow_redirects: bool = False,
        accepted_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        client = WSGIClient(self.dispatcher, WSGIResponse)
        return self._client_http_probe(
            client,
            path,
            probe_name=probe_name,
            follow_redirects=follow_redirects,
            accepted_statuses=accepted_statuses,
        )

    def _client_http_probe(
        self,
        client: WSGIClient,
        path: str,
        *,
        probe_name: str,
        follow_redirects: bool = False,
        accepted_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        response = client.get(path, follow_redirects=follow_redirects)
        if accepted_statuses is None:
            accepted_statuses = set(range(200, 400))
        if response.status_code in accepted_statuses:
            return {
                "status": "passed",
                "details": {
                    "path": path,
                    "status_code": response.status_code,
                },
            }
        return self._failed_probe(
            probe_name,
            f"Unexpected status code {response.status_code}.",
            details={"path": path, "status_code": response.status_code},
        )

    def _asset_probe(self, route: str) -> dict[str, Any]:
        client = WSGIClient(self.dispatcher, WSGIResponse)
        return self._client_asset_probe(client, route)

    def _client_asset_probe(self, client: WSGIClient, route: str) -> dict[str, Any]:
        response = client.get(route, follow_redirects=True)
        if response.status_code >= 400:
            return self._failed_probe(
                "static_assets",
                f"Homepage returned {response.status_code}.",
                details={"path": route, "status_code": response.status_code},
            )
        body = response.get_data(as_text=True)
        asset_path = None
        for marker in ('src="', "href=\""):
            start = body.find(marker)
            while start != -1:
                end = body.find('"', start + len(marker))
                candidate = body[start + len(marker) : end]
                if candidate.startswith(route) and (
                    "_dash-component-suites" in candidate or "_favicon.ico" in candidate
                ):
                    asset_path = candidate
                    break
                start = body.find(marker, start + len(marker))
            if asset_path is not None:
                break
        if asset_path is None:
            return self._failed_probe(
                "static_assets",
                "No static asset reference was found in the app shell.",
                details={"path": route, "status_code": response.status_code},
            )
        asset_response = client.get(asset_path)
        if 200 <= asset_response.status_code < 400:
            return {
                "status": "passed",
                "details": {
                    "path": asset_path,
                    "status_code": asset_response.status_code,
                },
            }
        return self._failed_probe(
            "static_assets",
            f"Asset returned {asset_response.status_code}.",
            details={"path": asset_path, "status_code": asset_response.status_code},
        )

    def _failed_probe(
        self,
        probe_name: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {"message": message}
        if details:
            payload.update(details)
        return {"name": probe_name, "status": "failed", "details": payload}

    def _skipped_probe(self, probe_name: str, message: str) -> dict[str, Any]:
        return {"name": probe_name, "status": "skipped", "details": {"message": message}}

    def _sql_smoke_probe(self, app_name: str, revision: AppRevision) -> dict[str, Any]:
        """Actively run each `queries/**/*.sql` file with ``WHERE 1=0`` against the bound profile.

        Returns ``not_applicable`` when the manifest has no data source (or no Exasol service is
        wired in), ``passed`` when every file parses cleanly, ``failed`` when any file fails to
        parse/bind or the connection itself can't open. This is the probe that closes BUG-002 —
        a dashboard with broken queries used to report ``all probes passed`` because the existing
        ``data_layer`` probe only inspected already-recorded errors.
        """

        from ..exasol.sql_smoke import collect_sql_files, run_sql_smoke

        exasol_service = self.runtime_extensions.get("exasol_dashboard_service")
        if exasol_service is None:
            return {"name": "sql_smoke", "status": "not_applicable", "details": {"reason": "no_exasol_service"}}

        profile_name = self._manifest_profile_name(revision)
        if not profile_name:
            return {
                "name": "sql_smoke",
                "status": "not_applicable",
                "details": {"reason": "no_bound_profile"},
            }

        try:
            profile = exasol_service.profile_store.get_profile(profile_name)
        except Exception as exc:
            return {
                "name": "sql_smoke",
                "status": "failed",
                "details": {
                    "reason": "profile_not_found",
                    "profile": profile_name,
                    "error": str(exc),
                },
            }

        sql_files = collect_sql_files(Path(revision.artifact_path))
        if not sql_files:
            return {
                "name": "sql_smoke",
                "status": "not_applicable",
                "details": {"reason": "no_sql_files"},
            }

        report = run_sql_smoke(
            profile=profile,
            sql_files=sql_files,
            connection_manager=exasol_service.connection_manager,
        )
        if report.overall_status == "passed":
            return {
                "name": "sql_smoke",
                "status": "passed",
                "details": {
                    "profile": profile_name,
                    "files_tested": [f.relative_path for f in report.files if f.outcome == "passed"],
                    "files_skipped": [
                        {"path": f.relative_path, "reason": f.skip_reason}
                        for f in report.files
                        if f.outcome == "skipped"
                    ],
                },
            }
        if report.overall_status == "skipped":
            return {
                "name": "sql_smoke",
                "status": "not_applicable",
                "details": {
                    "reason": "all_files_skipped",
                    "files": [
                        {"path": f.relative_path, "reason": f.skip_reason}
                        for f in report.files
                    ],
                },
            }
        # failed — surface the first failure plus the connection error if any.
        first = report.first_failure
        return {
            "name": "sql_smoke",
            "status": "failed",
            "details": {
                "profile": profile_name,
                "connection_error": report.connection_error,
                "first_failed_file": first.relative_path if first is not None else None,
                "latest_error": first.error_text if first is not None else report.connection_error,
                "failed_files": [
                    {"path": f.relative_path, "error": f.error_text}
                    for f in report.files
                    if f.outcome == "failed"
                ],
            },
        }

    def _manifest_profile_name(self, revision: AppRevision) -> str | None:
        """Return ``manifest.data_sources.primary.profile`` if it's set to an Exasol profile, else None."""

        manifest = revision.manifest or {}
        data_sources = manifest.get("data_sources") if isinstance(manifest, dict) else None
        if not isinstance(data_sources, dict):
            return None
        primary = data_sources.get("primary")
        if not isinstance(primary, dict):
            return None
        if primary.get("kind") != "exasol":
            return None
        profile_name = primary.get("profile")
        return profile_name if isinstance(profile_name, str) and profile_name else None

    def _data_layer_probe(self, app_name: str, *, revision_number: int | None) -> dict[str, Any]:
        """Probe the per-app errors stream for recent data_layer (Exasol) failures.

        Filters errors with both (a) the active revision number — so stale errors from
        a rolled-back revision don't keep the probe red after a promote — and (b) the
        acknowledge watermark, so operators can explicitly clear the probe after fixing
        SQL in-place without promoting a new revision.

        Returns a "passed" probe when no recent data_layer errors apply, a "failed"
        probe when one or more are present. This is how scaffolded Exasol dashboards
        bubble query failures up to app_run_healthcheck.
        """

        errors = self.diagnostics_service.list_errors(app_name, limit=0, source="data_layer")
        records = errors.get("errors", [])
        watermark = self.diagnostics_service.data_layer_ack_watermark(app_name)
        applicable = [
            record
            for record in records
            if _data_layer_record_applies_to(
                record, revision_number=revision_number, watermark=watermark
            )
        ]
        if not applicable:
            return {
                "name": "data_layer",
                "status": "passed",
                "details": {
                    "message": (
                        "No data-layer errors recorded for the current revision since the last acknowledge."
                        if watermark or revision_number is not None
                        else "No data-layer errors recorded since last reset."
                    ),
                    "watermark": watermark,
                    "revision_number": revision_number,
                },
            }
        latest = applicable[-1]
        sql_file = (latest.get("details") or {}).get("sql_file")
        return {
            "name": "data_layer",
            "status": "failed",
            "details": {
                "message": "One or more recent Exasol queries failed.",
                "sql_file": sql_file,
                "error_count": len(applicable),
                "latest_error": (latest.get("details") or {}).get("error"),
                "latest_timestamp": latest.get("timestamp"),
                "watermark": watermark,
                "revision_number": revision_number,
            },
        }

    def _worker_http_probe(
        self, *, mount_path: str, revision_number: int | None
    ) -> dict[str, Any]:
        """Probe a worker via its last-seen HTTP response status from the proxy.

        The proxy calls ``manager.set_last_response_status(...)`` on every forwarded
        request, so the probe is a pure read — no extra HTTP roundtrip to the worker.
        Returns ``not_applicable`` outside isolated mode.
        """

        if self.runtime_mode != "isolated" or self.worker_manager is None:
            return {
                "name": "worker_http",
                "status": "not_applicable",
                "details": {"runtime_mode": self.runtime_mode},
            }
        record = self.worker_manager.get_record(mount_path)
        if record is None or record.last_response_status is None:
            return {
                "name": "worker_http",
                "status": "skipped",
                "details": {
                    "message": (
                        "No HTTP request has reached the worker yet for the current revision."
                    ),
                    "mount_path": mount_path,
                    "revision_number": revision_number,
                },
            }
        status_code = record.last_response_status
        return {
            "name": "worker_http",
            "status": "passed" if status_code < 500 else "failed",
            "details": {
                "last_response_status": status_code,
                "last_request_at": record.last_request_at,
                "mount_path": mount_path,
                "revision_number": revision_number,
            },
        }

    def _worker_alive_probe(
        self, *, mount_path: str, revision_number: int | None
    ) -> dict[str, Any]:
        """Probe an isolated-mode worker for liveness.

        Returns ``status="not_applicable"`` when the server is running in_process so the
        probe list stays the same shape across modes.
        """

        if self.runtime_mode != "isolated" or self.worker_manager is None:
            return {
                "name": "worker_alive",
                "status": "not_applicable",
                "details": {"runtime_mode": self.runtime_mode},
            }
        record = self.worker_manager.get_record(mount_path)
        if record is None:
            return {
                "name": "worker_alive",
                "status": "failed",
                "details": {
                    "message": "No worker record found for this mount path.",
                    "mount_path": mount_path,
                },
            }
        alive = self.worker_manager.ensure_running(mount_path) is not None
        rss = self.worker_manager.sample_rss(mount_path)
        return {
            "name": "worker_alive",
            "status": "passed" if alive else "failed",
            "details": {
                "pid": record.pid,
                "endpoint": f"{record.host}:{record.port}",
                "environment_id": record.environment_id,
                "revision_number": revision_number,
                "started_at": record.started_at,
                "last_request_at": record.last_request_at,
                "rss_bytes": rss,
            },
        }

    def _preflight_mount_path(self, app_name: str, revision_number: int) -> str:
        return f"/__dash-server/preflight/{app_name}/{revision_number}/{uuid.uuid4().hex}"

    def _preflight_failure_category(self, preflight: dict[str, Any]) -> str:
        captured_errors = preflight.get("captured_errors")
        if isinstance(captured_errors, list) and captured_errors:
            latest = captured_errors[-1]
            if isinstance(latest, dict):
                category = latest.get("category")
                if isinstance(category, str) and category:
                    return category
        error = preflight.get("error")
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict):
                traceback_text = details.get("traceback_text")
                if isinstance(traceback_text, str) and traceback_text.strip():
                    return self.diagnostics_service.inspect_traceback(traceback_text)["traceback"][
                        "category"
                    ]
            category = error.get("category")
            if isinstance(category, str) and category:
                return category
        failed_probe = next(
            (
                probe
                for probe in preflight.get("probes", [])
                if isinstance(probe, dict) and probe.get("status") == "failed"
            ),
            None,
        )
        if isinstance(failed_probe, dict) and failed_probe.get("name") == "dash_layout":
            return "dash_layout_error"
        return "runtime_crash"

    def _preflight_traceback_text(self, preflight: dict[str, Any]) -> str | None:
        captured_errors = preflight.get("captured_errors")
        if isinstance(captured_errors, list) and captured_errors:
            latest = captured_errors[-1]
            if isinstance(latest, dict):
                traceback_text = latest.get("traceback_text")
                if isinstance(traceback_text, str) and traceback_text.strip():
                    return traceback_text
        error = preflight.get("error")
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict):
                traceback_text = details.get("traceback_text")
                if isinstance(traceback_text, str) and traceback_text.strip():
                    return traceback_text
        return None

    def _ensure_route_available(self, route: str, excluding_app: str | None = None) -> None:
        existing = self.registry.get_app_by_route(route)
        if existing is None:
            return
        if excluding_app is not None and existing.name == excluding_app:
            return
        raise DashServerError(
            category="route_conflict",
            summary=f"Route {route} is already assigned to app {existing.name}.",
            details={"route": route, "existing_app": existing.name},
            jsonrpc_code=-32009,
            http_status=409,
        )

    def _normalize_live_route(self, mount_path: str) -> str:
        if not isinstance(mount_path, str) or not mount_path.startswith("/apps/"):
            raise DashServerError(
                category="exposure_validation_error",
                summary="Live mount paths must start with /apps/.",
                details={"mount_path": mount_path},
                jsonrpc_code=-32602,
            )
        normalized = mount_path.rstrip("/") or mount_path
        if normalized.startswith("/preview/"):
            raise DashServerError(
                category="exposure_validation_error",
                summary="Live mount paths cannot use the preview namespace.",
                details={"mount_path": mount_path},
                jsonrpc_code=-32602,
            )
        return normalized

    def _normalize_visibility(self, visibility: str) -> str:
        if visibility not in {"private", "public", "internal"}:
            raise DashServerError(
                category="exposure_validation_error",
                summary="Visibility must be one of private, public, or internal.",
                details={"visibility": visibility},
                jsonrpc_code=-32602,
            )
        return visibility

    def _normalize_auth_policy(self, auth_policy: str) -> str:
        if auth_policy not in {"inherited", "required", "none", "custom"}:
            raise DashServerError(
                category="exposure_validation_error",
                summary="Auth policy must be one of inherited, required, none, or custom.",
                details={"auth_policy": auth_policy},
                jsonrpc_code=-32602,
            )
        return auth_policy

    def _normalize_permissions(self, permissions: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(permissions, dict):
            raise DashServerError(
                category="exposure_validation_error",
                summary="Permissions must be an object.",
                details={"permissions": permissions},
                jsonrpc_code=-32602,
            )
        normalized = {
            "filesystem": permissions.get("filesystem", {"mode": "workspace-write"}),
            "network": permissions.get("network", {"mode": "inherit"}),
            "env": permissions.get("env", {"mode": "inherit"}),
        }
        for key in ("filesystem", "network", "env"):
            if not isinstance(normalized[key], dict):
                raise DashServerError(
                    category="exposure_validation_error",
                    summary=f"Permission section {key} must be an object.",
                    details={"section": key},
                    jsonrpc_code=-32602,
                )
        return normalized


def _data_layer_record_applies_to(
    record: dict[str, Any],
    *,
    revision_number: int | None,
    watermark: str | None,
) -> bool:
    """Return True when a data-layer error record is still relevant.

    Filters out:
      - errors from a different revision (when `revision_number` is given and the
        record carries a non-None `revision_number` of its own — legacy records with
        ``revision_number: null`` pass through so we don't suddenly hide old data).
      - errors recorded before the ack watermark.
    """

    if revision_number is not None:
        stamped = record.get("revision_number")
        if isinstance(stamped, int) and stamped != revision_number:
            return False
    if watermark is not None:
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp <= watermark:
            # ISO-8601 strings compare lexically when same timezone — both are UTC
            # `...Z` strings produced by `DiagnosticsService._timestamp()`.
            return False
    return True
