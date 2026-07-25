"""Health-probe suite for :class:`AppRuntimeService`.

``HealthProber`` owns the runtime health probes (HTTP readiness, Dash layout/
dependencies, static assets, data-layer, SQL smoke, and the isolated-worker
liveness/HTTP probes) plus the small helpers that assemble the HTTP probe suite
and downgrade the overall status. The service keeps ``run_healthcheck`` /
``preflight_revision`` and delegates the individual probes here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from werkzeug.test import Client as WSGIClient
from werkzeug.wrappers import Response as WSGIResponse

from dash_server.registry.models import AppRevision

if TYPE_CHECKING:
    from .service import AppRuntimeService


class HealthProber:
    """Runtime health-probe suite owned by the runtime service facade."""

    def __init__(self, svc: AppRuntimeService) -> None:
        self.svc = svc

    def _run_http_probe_suite(self, mount_path: str) -> tuple[list[dict[str, Any]], str]:
        """Run the four HTTP probes against a mounted app; return (probes, overall)."""

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
        probes = [homepage_probe, layout_probe, dependency_probe, asset_probe]
        overall_status = (
            "healthy"
            if all(probe["status"] == "passed" for probe in probes)
            else "unhealthy"
        )
        # Ensure every probe carries its canonical name even for the passed-dict
        # shapes returned by `_http_probe` / `_asset_probe`.
        homepage_probe["name"] = "http_ready"
        layout_probe["name"] = "dash_layout"
        dependency_probe["name"] = "dash_dependencies"
        asset_probe["name"] = "static_assets"
        return probes, overall_status

    def _skipped_http_probes(self, message: str) -> list[dict[str, Any]]:
        """The four HTTP probes marked skipped with ``message`` (name-carrying)."""

        return [
            self._skipped_probe("http_ready", message),
            self._skipped_probe("dash_layout", message),
            self._skipped_probe("dash_dependencies", message),
            self._skipped_probe("static_assets", message),
        ]

    @staticmethod
    def _downgrade(overall: str, probe: dict[str, Any]) -> str:
        """Downgrade a ``healthy`` overall to ``degraded`` when ``probe`` failed."""

        if probe["status"] == "failed" and overall == "healthy":
            return "degraded"
        return overall

    def _http_probe(
        self,
        path: str,
        *,
        probe_name: str,
        follow_redirects: bool = False,
        accepted_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        client = WSGIClient(self.svc.dispatcher, WSGIResponse)
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
        client = WSGIClient(self.svc.dispatcher, WSGIResponse)
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

        from ..exasol.sql_smoke import collect_sql_files, collect_sql_smoke_params, run_sql_smoke

        exasol_service = self.svc.runtime_extensions.get("exasol_dashboard_service")
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
            smoke_params=collect_sql_smoke_params(Path(revision.artifact_path)),
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

        errors = self.svc.diagnostics_service.list_errors(app_name, limit=0, source="data_layer")
        records = errors.get("errors", [])
        watermark = self.svc.diagnostics_service.data_layer_ack_watermark(app_name)
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

        if self.svc.runtime_mode != "isolated" or self.svc.worker_manager is None:
            return {
                "name": "worker_http",
                "status": "not_applicable",
                "details": {"runtime_mode": self.svc.runtime_mode},
            }
        record = self.svc.worker_manager.get_record(mount_path)
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

        if self.svc.runtime_mode != "isolated" or self.svc.worker_manager is None:
            return {
                "name": "worker_alive",
                "status": "not_applicable",
                "details": {"runtime_mode": self.svc.runtime_mode},
            }
        record = self.svc.worker_manager.get_record(mount_path)
        if record is None:
            return {
                "name": "worker_alive",
                "status": "failed",
                "details": {
                    "message": "No worker record found for this mount path.",
                    "mount_path": mount_path,
                },
            }
        alive = self.svc.worker_manager.ensure_running(mount_path) is not None
        rss = self.svc.worker_manager.sample_rss(mount_path)
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
