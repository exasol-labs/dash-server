"""Revision mounting + WSGI instrumentation for :class:`AppRuntimeService`.

``RuntimeMounter`` owns everything about turning a revision into a live WSGI
mount: in-process artifact loading, isolated-worker spawn + proxy mounting, and
the ``got_request_exception`` instrumentation that funnels runtime/callback
failures into diagnostics. The service facade delegates its ``_mount_*`` /
``_create_revision_wsgi_app`` methods here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dash import Dash
from flask import Flask

from dash_server.artifacts_io import (
    APP_ENTRYPOINT_FILENAME,
    APP_MANIFEST_FILENAME,
    REQUIREMENTS_FILENAME,
)
from dash_server.dash_apps.branding import apply_hosted_footer
from dash_server.dash_apps.callback_isolation import (
    finalize_dash_app_callbacks,
    isolated_dash_callback_globals,
)
from dash_server.dash_apps.factory import validate_bundle, validate_manifest_payload
from dash_server.diagnostics.runtime_capture import record_request_exceptions
from dash_server.dash_apps.runtime_checks import verify_dash_mount
from dash_server.exceptions import DashServerError
from dash_server.imports import isolated_local_imports
from dash_server.registry.models import AppRevision

if TYPE_CHECKING:
    from .service import AppRuntimeService


class RuntimeMounter:
    """Mounting + WSGI instrumentation owned by the runtime service facade."""

    def __init__(self, svc: AppRuntimeService) -> None:
        self.svc = svc

    def _mount_live_revision(self, name: str) -> None:
        app = self.svc._require_app(name)
        revision = self.svc._require_current_revision(name)
        if not app.enabled:
            self.svc.dispatcher.unmount(app.route)
            return
        self._mount_revision(revision, app.route)

    def _mount_preview_revision(self, name: str, revision_number: int) -> None:
        revision = self.svc._require_revision(name, revision_number)
        self._mount_revision(revision, self.svc.preview_path(name, revision_number))

    def _mount_revision(self, revision: AppRevision, mount_path: str) -> None:
        try:
            if self.svc.runtime_mode == "isolated" and self.svc.worker_manager is not None:
                self._mount_revision_isolated(revision, mount_path)
            else:
                wsgi_app = self._create_revision_wsgi_app(revision, mount_path)
                self.svc.dispatcher.mount(mount_path, wsgi_app)
            self.svc.diagnostics_service.append_log(
                revision.app_name,
                "runtime",
                f"Mounted revision {revision.revision_number} at {mount_path}.",
                revision_number=revision.revision_number,
                data={"runtime_mode": self.svc.runtime_mode},
            )
        except DashServerError as exc:
            self.svc._record_dash_server_error(
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
            )
            self.svc._record_dash_server_error(
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
        # narrow `self.svc.worker_manager` from `Any | None` to a concrete manager for the
        # rest of the function body.
        assert self.svc.worker_manager is not None, "isolated mount without worker_manager"
        from .worker_manager import WorkerStartError

        artifact_path = Path(revision.artifact_path)
        app_source = artifact_path / APP_ENTRYPOINT_FILENAME
        if not (artifact_path.is_dir() and app_source.exists()):
            # Fall back to in-process for revisions whose artifact has no app.py on disk.
            wsgi_app = self._create_revision_wsgi_app(revision, mount_path)
            self.svc.dispatcher.mount(mount_path, wsgi_app)
            return

        # Resolve the worker's python_executable. Prefer the env id stored on the revision
        # row (recorded at build time); fall back to recomputing from requirements.txt only
        # when the build never wrote one (older revisions or builds that ran before the
        # dependency-environment service was wired in).
        python_executable: str | None = None
        environment_id: str | None = None
        if self.svc.dependency_environment_service is not None:
            env_id = revision.dependency_environment_id
            stored_python = revision.env_python_executable
            if env_id and stored_python:
                # Fast path: trust the stored identity. lookup() still tells us if the env
                # was evicted from disk so we can fall through to recompute and rebuild.
                env_record = self.svc.dependency_environment_service.lookup(env_id)
                if env_record is not None and isinstance(env_record.get("python_executable"), str):
                    python_executable = env_record["python_executable"]
                    environment_id = env_id
                else:
                    # Env was GC'd or never materialized. Fall through to recompute path.
                    env_id = ""
            if not environment_id:
                requirements = self._read_requirements_from_artifact(artifact_path)
                try:
                    env_id_computed = self.svc.dependency_environment_service.compute_environment_id(requirements)
                    env_record = self.svc.dependency_environment_service.lookup(env_id_computed)
                except Exception:
                    env_record = None
                    env_id_computed = None
                if env_record is not None and isinstance(env_record.get("python_executable"), str):
                    python_executable = env_record["python_executable"]
                    environment_id = env_id_computed
                    # Backfill the revision row so subsequent mounts hit the fast path.
                    try:
                        self.svc.registry.update_revision_environment(
                            revision.id,
                            dependency_environment_id=env_id_computed,
                            env_python_executable=python_executable,
                        )
                    except Exception:
                        pass

        manifest = revision.manifest or {}

        try:
            self.svc.worker_manager.start(
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
                    f"Failed to start isolated worker for {revision.app_name} revision {revision.revision_number}."
                ),
                details={
                    "app": revision.app_name,
                    "revision_number": revision.revision_number,
                    "mount_path": mount_path,
                    "worker_start_error": str(exc),
                    "python_executable": python_executable or sys.executable,
                },
            ) from exc

        proxy = WorkerProxyWSGIApp(
            self.svc.worker_manager,
            mount_path=mount_path,
            app_name=revision.app_name,
        )
        self.svc.dispatcher.mount(mount_path, proxy)

    def _read_requirements_from_artifact(self, artifact_path: Path) -> list[str]:
        path = artifact_path / REQUIREMENTS_FILENAME
        if not path.exists():
            return []
        return [
            line.strip() for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#")
        ]

    def _create_revision_wsgi_app(self, revision: AppRevision, mount_path: str) -> Flask:
        artifact_path = Path(revision.artifact_path)
        if artifact_path.is_dir() and (artifact_path / APP_ENTRYPOINT_FILENAME).exists():
            wsgi_app = self._load_workspace_artifact_wsgi_app(
                artifact_path,
                mount_path,
                revision_number=revision.revision_number,
            )
        else:
            manifest, dashboard = validate_bundle({"manifest": revision.manifest, "dashboard": revision.bundle})
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
        record_request_exceptions(
            wsgi_app, self.svc.diagnostics_service, app_name=app_name, revision_number=revision_number
        )

    def _load_workspace_artifact_wsgi_app(
        self,
        artifact_dir: Path,
        mount_path: str,
        *,
        revision_number: int,
    ) -> Flask:
        manifest_payload = json.loads((artifact_dir / APP_MANIFEST_FILENAME).read_text())
        manifest = validate_manifest_payload(manifest_payload)
        module_name = f"dash_server_artifact_{manifest.name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, artifact_dir / APP_ENTRYPOINT_FILENAME)
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
                ) from exc
            factory = getattr(module, "create_dash_app", None)
            if not callable(factory):
                raise DashServerError(
                    category="artifact_error",
                    summary="Artifact app.py must define create_dash_app(server, url_base_pathname, metadata).",
                    details={"artifact_path": str(artifact_dir)},
                )
            server = Flask(f"dash_server.runtime.{manifest.name}.{uuid.uuid4().hex}")
            server.extensions.update(self.svc.runtime_extensions)
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
                        app_name=manifest.name,
                        has_consumption_outputs=bool((manifest.consumption or {}).get("outputs")),
                        wrap=True,
                        session_channel=self.svc.session_channel_enabled,
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
                )
        return server
