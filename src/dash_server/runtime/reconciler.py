"""Git desired-state read/write/reconcile for :class:`AppRuntimeService`.

``GitReconciler`` owns the GitOps desired-state surface: reading the parsed
desired state, rebuilding the SQLite cache from Git, drift reporting, writing
live/preview desired state, and reconciling one app (or the whole fleet) against
that desired state. The service facade delegates these methods here.

The per-app reconcile loop calls back through ``self.svc._reconcile_app_desired_state``
so tests can monkeypatch that hook on the service instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dash_server.artifacts_io import list_artifact_files, load_manifest_from_dir
from dash_server.dash_apps.factory import validate_manifest_payload
from dash_server.exceptions import DashServerError
from dash_server.registry.models import AppManifest, AppRevision, HostedApp

if TYPE_CHECKING:
    from .service import AppRuntimeService


class GitReconciler:
    """Git desired-state read/write/reconcile owned by the runtime service facade."""

    def __init__(self, svc: AppRuntimeService) -> None:
        self.svc = svc

    def git_desired_state(self) -> dict[str, Any]:
        """Return the parsed Git-backed desired live and preview state."""

        return self.svc.git_repo_service.desired_state()

    def rebuild_cache_from_git(self) -> dict[str, Any]:
        """Reconstruct app and revision cache rows from the authoritative GitOps repository."""

        if not self.svc.git_repo_service.has_commits():
            return {"apps": [], "status": "skipped"}

        desired = self.git_desired_state()
        rebuilt_apps: list[dict[str, Any]] = []
        for app_name in self.svc.git_repo_service.tracked_apps():
            manifest_payload = self.svc.git_repo_service.read_app_manifest(app_name)
            if not isinstance(manifest_payload, dict):
                continue
            try:
                source_manifest = validate_manifest_payload(manifest_payload)
            except DashServerError as exc:
                self.svc._record_dash_server_error(app_name, source="runtime", exc=exc)
                continue

            release_payloads = self.svc.git_repo_service.read_release_manifests(app_name)
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

            existing_app = self.svc.registry.get_app(app_name)
            status = existing_app.status if existing_app is not None else "running"
            exposure = self._exposure_from_desired_state(
                live_desired,
                fallback_manifest=source_manifest,
                fallback_app=existing_app,
            )

            self.svc.registry.upsert_app_cache(
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
                self.svc.registry.update_revision_state(
                    revision.id,
                    lifecycle_state,
                    rollout_metadata=revision.rollout_metadata,
                )

            if not self.svc.registry.list_events(app_name):
                revision_map = {revision.revision_number: revision.id for revision in revisions}
                for event_payload in self.svc.git_repo_service.read_history_events(app_name):
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
                    self.svc.registry.ensure_event(
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
                *[app.name for app in self.svc.registry.list_apps()],
                *desired["live"].keys(),
                *desired["preview"].keys(),
            }
        )
        for app_name in app_names:
            app = self.svc.registry.get_app(app_name)
            live_desired = desired["live"].get(app_name)
            preview_desired = desired["preview"].get(app_name)
            live_status = "missing"
            preview_status = "missing"
            if app is not None and live_desired is not None:
                current_revision = self.svc.registry.get_current_revision(app_name)
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
                preview_revision = self.svc.registry.get_preview_revision(app_name)
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
            observed_current = self.svc.registry.get_current_revision(app_name) if app is not None else None
            observed_preview = self.svc.registry.get_preview_revision(app_name) if app is not None else None
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
            "repo": self.svc.git_repo_service.status()["repo"],
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
                *[app.name for app in self.svc.registry.list_apps()],
                *desired["live"].keys(),
                *desired["preview"].keys(),
            }
        ):
            app = self.svc.registry.get_app(app_name)
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
                result = self.svc._reconcile_app_desired_state(
                    app_name,
                    desired["live"].get(app_name),
                    desired["preview"].get(app_name),
                    desired_live_routes,
                )
            except DashServerError as exc:
                self.svc._record_dash_server_error(app_name, source="runtime", exc=exc)
                result = {
                    "app": app_name,
                    "status": "failed",
                    "reason": exc.category,
                    "details": exc.details,
                }
            results.append(result)
        return {
            "repo": self.svc.git_repo_service.status()["repo"],
            "desired_state": desired,
            "results": results,
        }

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
        app = self.svc._require_app(app_name)
        self.svc.git_repo_service.publish_revision_to_main(
            app_name=app_name,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            source_hash=revision.source_hash,
            dependency_lock_hash=revision.dependency_lock_hash,
            release_manifest_path=revision.release_manifest_path,
        )
        self.svc.git_repo_service.write_live_desired_state(
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
        self.svc.git_repo_service.publish_revision_to_main(
            app_name=app_name,
            revision_number=revision.revision_number,
            artifact_path=revision.artifact_path,
            commit_sha=revision.commit_sha,
            git_tag=revision.git_tag,
            source_hash=revision.source_hash,
            dependency_lock_hash=revision.dependency_lock_hash,
            release_manifest_path=revision.release_manifest_path,
        )
        self.svc.git_repo_service.write_preview_desired_state(
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
        app = self.svc._require_app(app_name)
        previous_route = app.route
        if live_desired is not None:
            spec = live_desired.get("spec")
            if not isinstance(spec, dict):
                raise DashServerError(
                    category="gitops_reconcile_error",
                    summary=f"Live desired state for app {app_name} is malformed.",
                    details={"app": app_name, "desired_state": live_desired},
                )
            route = self.svc._normalize_live_route(str(spec.get("route", app.route)))
            if len(desired_live_routes.get(route, [])) > 1:
                raise DashServerError(
                    category="route_conflict",
                    summary=f"Live desired state uses duplicate route {route}.",
                    details={"app": app_name, "route": route, "apps": desired_live_routes[route]},
                )
            self.svc._ensure_route_available(route, excluding_app=app_name)
            visibility = self.svc._normalize_visibility(str(spec.get("visibility", app.visibility)))
            auth_policy = self.svc._normalize_auth_policy(str(spec.get("authPolicy", app.auth_policy)))
            enabled = bool(spec.get("enabled", app.enabled))
            spec_permissions = spec.get("permissions")
            permissions = self.svc._normalize_permissions(
                spec_permissions if isinstance(spec_permissions, dict) else app.permissions
            )
            updated = self.svc.registry.update_exposure(
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
                previous_current = self.svc.registry.get_current_revision(app_name)
                self.svc.registry.promote_revision(app_name, desired_live_revision.id)
                self.svc.registry.update_revision_state(
                    desired_live_revision.id,
                    "live",
                    rollout_metadata={"reconciled_to": app.route},
                )
                if previous_current is not None and previous_current.id != desired_live_revision.id:
                    self.svc.registry.update_revision_state(
                        previous_current.id,
                        "archived",
                        rollout_metadata={"replaced_by": desired_live_revision.revision_number},
                    )
                app = self.svc._require_app(app_name)
            if previous_route != app.route and self.svc.dispatcher.is_mounted(previous_route):
                self.svc.dispatcher.unmount(previous_route)
            if app.enabled and app.status == "running":
                self.svc._mount_live_revision(app_name)
            else:
                self.svc.dispatcher.unmount(app.route)
        else:
            desired_live_revision = None

        if preview_desired is not None:
            desired_preview_revision = self._resolve_desired_revision(app_name, preview_desired)
            if desired_preview_revision is None:
                raise DashServerError(
                    category="gitops_reconcile_error",
                    summary=f"Preview desired state for app {app_name} could not be resolved.",
                    details={"app": app_name, "desired_state": preview_desired},
                )
            self.svc.registry.set_preview_revision(app_name, desired_preview_revision.id)
            self.svc.registry.update_revision_state(
                desired_preview_revision.id,
                "warming",
                rollout_metadata={"preview_path": self.svc.preview_path(app_name, desired_preview_revision.revision_number)},
            )
            self.svc._mount_preview_revision(app_name, desired_preview_revision.revision_number)
        else:
            existing_preview = self.svc.registry.get_preview_revision(app_name)
            if existing_preview is not None:
                self.svc.dispatcher.unmount(self.svc.preview_path(app_name, existing_preview.revision_number))
                self.svc.registry.set_preview_revision(app_name, None)
                # PS26-BUG-019: clearing the preview pointer here never updated the
                # revision's own `lifecycle_state`, which was left at "warming"
                # (set when it *entered* preview, a few reconcile passes ago)
                # forever - it doesn't settle to "archived" like every other
                # non-current, non-preview revision. Skip the case where this
                # revision was simultaneously promoted to live above (it's already
                # correctly "live"; overwriting that back to "archived" would be
                # its own bug).
                if desired_live_revision is None or existing_preview.id != desired_live_revision.id:
                    self.svc.registry.update_revision_state(
                        existing_preview.id,
                        "archived",
                        rollout_metadata={"cleared_preview_without_promotion": True},
                    )
            desired_preview_revision = None

        # PS26-BUG-016: an app with no Git desired-state at all (never committed to
        # the GitOps repo, or committed then removed) used to get the same
        # `"reconciled"` label as an app whose desired state was actually applied -
        # indistinguishable from "in sync" unless a caller separately noticed both
        # revision fields were null. `repo_reconcile` on a registry containing such
        # apps should let a caller find them by status alone.
        status = "reconciled" if (live_desired is not None or preview_desired is not None) else "untracked"
        return {
            "app": app_name,
            "status": status,
            "live_revision": desired_live_revision.revision_number if live_desired is not None and desired_live_revision is not None else None,
            "preview_revision": desired_preview_revision.revision_number if desired_preview_revision is not None else None,
            "route": self.svc._require_app(app_name).route,
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
        revision = self.svc.registry.get_revision_by_number(app_name, revision_number)
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
            "permissions": self.svc._normalize_permissions(spec.get("permissions", permissions))
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
        existing = self.svc.registry.get_revision_by_number(app_name, revision_number)
        artifact_path = str(spec.get("artifactPath", ""))
        revision_manifest = self._manifest_from_artifact_or_fallback(artifact_path, fallback_manifest)
        return self.svc.registry.upsert_revision_cache(
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
        manifest_payload = load_manifest_from_dir(Path(artifact_path))
        if manifest_payload is not None:
            return validate_manifest_payload(manifest_payload)
        return fallback_manifest

    def _artifact_source_files(self, artifact_path: str) -> list[str]:
        artifact_dir = Path(artifact_path)
        if not artifact_dir.exists() or not artifact_dir.is_dir():
            return []
        return list_artifact_files(artifact_dir)

    def _strip_hash_prefix(self, value: str) -> str:
        return value.split("sha256:", 1)[1] if value.startswith("sha256:") else value

    def reconcile_app(self, app_name: str) -> dict[str, Any]:
        """Reconcile one app's Git desired state without touching the rest of the fleet.

        Single-app mutations (route/visibility/exposure changes, promote,
        rollback, preview) must not re-resolve or re-mount unrelated apps; the
        fleet-wide `reconcile_git_desired_state` remains for bootstrap and
        drift reporting.
        """
        app = self.svc.registry.get_app(app_name)
        if app is None:
            return {"app": app_name, "status": "skipped", "reason": "app_not_registered"}
        desired = self.git_desired_state()
        desired_live_routes = self._desired_live_routes(desired["live"])
        try:
            return self.svc._reconcile_app_desired_state(
                app_name,
                desired["live"].get(app_name),
                desired["preview"].get(app_name),
                desired_live_routes,
            )
        except DashServerError as exc:
            self.svc._record_dash_server_error(app_name, source="runtime", exc=exc)
            return {
                "app": app_name,
                "status": "failed",
                "reason": exc.category,
                "details": exc.details,
            }

    def _reconcile_or_raise(self, app_name: str) -> dict[str, Any]:
        result = self.svc.reconcile_app(app_name)
        if result.get("status") == "failed":
            raise DashServerError(
                category=str(result.get("reason") or "gitops_reconcile_error"),
                summary=f"Git desired-state reconcile failed for app {app_name}.",
                details={"app": app_name, **(result.get("details") or {})},
            )
        return result
