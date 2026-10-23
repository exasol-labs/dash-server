"""Health-probe suite for :class:`AppRuntimeService`.

``HealthProber`` owns the runtime health probes (HTTP readiness, Dash layout/
dependencies, static assets, data-layer, SQL smoke, and the isolated-worker
liveness/HTTP probes) plus the small helpers that assemble the HTTP probe suite
and downgrade the overall status. The service keeps ``run_healthcheck`` /
``preflight_revision`` and delegates the individual probes here.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from werkzeug.test import Client as WSGIClient
from werkzeug.wrappers import Response as WSGIResponse

from dash_server.registry.models import AppRevision
from dash_server.timestamps import parse_iso8601

if TYPE_CHECKING:
    from .service import AppRuntimeService

# Exasol Semantic Views AGENT_READINESS values that make a model's numbers untrustworthy.
_UNUSABLE_SEMANTIC_READINESS = {"INVALID", "PRECONDITION", "NOT_PUBLISHED"}


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
        semantic_models = [asdict(ref) for ref in report.semantic_models]
        warnings = [
            {"path": f.relative_path, "message": message} for f in report.files for message in f.warnings
        ]
        extra: dict[str, Any] = {}
        if semantic_models:
            extra["semantic_models"] = semantic_models
        definitions = [
            {"path": f.relative_path, **f.definitions} for f in report.files if f.definitions is not None
        ]
        if definitions:
            extra["definitions"] = definitions
        if warnings:
            extra["warnings"] = warnings
        # Query files that bypass the semantic layer: saved compiler output ("frozen") or
        # hand-written SQL over a model's source tables ("raw").
        bypasses = [
            {
                "path": f.relative_path,
                "lane": "frozen" if f.frozen_semantic_sql else "raw",
                "models": list(f.governed_reads),
            }
            for f in report.files
            if f.frozen_semantic_sql or f.governed_reads
        ]
        if bypasses:
            extra["semantic_bypasses"] = bypasses
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
                    **extra,
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
                **extra,
            },
        }

    def _semantic_models_probe(
        self,
        sql_smoke_probe: dict[str, Any],
        *,
        built_definitions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Report the readiness of the Exasol Semantic Views models the app's queries read.

        Derived from the `sql_smoke` probe, whose session already looked the models up.
        Fails when a model is unusable (INVALID, PRECONDITION, or NOT_PUBLISHED): its
        numbers can't be trusted even if today's queries still run. WARNING passes, as in
        the semantic layer's own agent views, but is listed so it stays visible.

        With the definitions recorded when the revision was built, also lists every field
        whose definition has changed since. The dashboard already shows the new definition
        (it queries the model live), so this is for review, not a failure.
        """

        details_in = sql_smoke_probe.get("details", {})
        models = details_in.get("semantic_models") or []
        bypasses = details_in.get("semantic_bypasses") or []
        if not models and not bypasses:
            return {"name": "semantic_models", "status": "not_applicable", "details": {"reason": "no_semantic_queries"}}
        unusable = [model for model in models if model.get("readiness") in _UNUSABLE_SEMANTIC_READINESS]
        details: dict[str, Any] = {"models": models}
        frozen = [entry for entry in bypasses if entry["lane"] == "frozen"]
        raw = [entry for entry in bypasses if entry["lane"] == "raw"]
        if frozen:
            details["frozen_files"] = frozen
        if raw:
            details["raw_governed_reads"] = raw
            details["raw_governed_reads_note"] = (
                "These files compute numbers from tables a semantic model governs instead of querying the model. "
                "They don't follow certified definition changes and can disagree with governed dashboards."
            )
        fusion = [_fusion_summary(model) for model in models if model.get("fusion_layout")]
        if fusion:
            details["fusion"] = fusion
        fusion_changes: dict[str, list[str]] = {}
        if built_definitions is not None:
            fusion_changes = _fusion_changes(built_definitions, models)
            if fusion_changes:
                details["fusion_changed_since_build"] = [
                    {"model": model, "change": change} for model, changes in fusion_changes.items() for change in changes
                ]
                details["fusion_changed_since_build_note"] = (
                    "How these fused models combine their sources, or how their reconciled attributes are answered, "
                    "changed after this revision was built. The definitions may be unchanged, but the numbers move: "
                    "check the affected panels, and show coverage and unresolved keys to viewers."
                )
            changed = _changed_definitions(built_definitions, details_in.get("definitions") or [])
            if changed:
                layout_moved = {model for model, changes in fusion_changes.items() if any(c.startswith("layout:") for c in changes)}
                for entry in changed:
                    if entry.get("model") in layout_moved:
                        entry["fusion_layout_changed"] = True
                details["changed_since_build"] = changed
                details["changed_since_build_note"] = (
                    "These metric or dimension definitions changed after this revision was built. The dashboard "
                    "queries the model live, so it already shows the new definitions; check that each panel's "
                    "label and meaning still match."
                    + (
                        " Entries with fusion_layout_changed belong to a model whose fusion layout also changed "
                        "(see fusion_changed_since_build): a partition boundary or authority change moves the hash "
                        "of every field on the affected entity even when its expression is unchanged."
                        if layout_moved
                        else ""
                    )
                )
        warnings = [model["model"] for model in models if model.get("readiness") == "WARNING"]
        if warnings:
            details["models_with_warnings"] = warnings
        unknown = [model["model"] for model in models if model.get("readiness") == "UNKNOWN"]
        if unknown:
            details["models_not_assessed"] = unknown
            details["models_not_assessed_note"] = (
                "The newest validation run of these models never finished, and this profile can't read "
                "SEMANTIC_SOURCE.VALIDATION_RUNS to find the last finished one. Grant the profile's user "
                "SEMANTIC_USER, or run SEMANTIC_ADMIN.VALIDATE_MODEL."
            )
        if unusable:
            first = unusable[0]
            details["latest_error"] = (
                f"Semantic model {first['model']!r} ({first['published_schema']}) is {first['readiness']}"
                + _first_issue_text(first)
                + ". Check SEMANTIC_AGENT.MODELS_FOR_AGENT and SEMANTIC_SOURCE.VALIDATION_RESULTS."
            )
            return {"name": "semantic_models", "status": "failed", "details": details}
        if frozen:
            details["latest_error"] = (
                f"{frozen[0]['path']} is SQL saved from the semantic layer's compiler: it keeps the metric "
                "definitions of the day it was compiled and won't follow certified changes. Query the model's "
                "published schema with Semantic SQL instead (dash://exasol/help/semantic-layer)."
            )
            return {"name": "semantic_models", "status": "failed", "details": details}
        return {"name": "semantic_models", "status": "passed", "details": details}

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
        diagnostics = self.svc.diagnostics_service
        applicable = [
            record
            for record in records
            if _data_layer_record_applies_to(record, revision_number=revision_number, watermark=watermark)
            and not _data_layer_record_resolved(diagnostics, app_name, record)
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


def _fusion_summary(model: dict[str, Any]) -> dict[str, Any]:
    """A fused model's partitions and reconciled attributes, as the probe reports them."""

    partitions = [
        {"entity": part["entity"], "representation": part["representation"], "coverage": part["coverage"]}
        for part in model["fusion_layout"]
        if part["aspect"] == "PARTITION"
    ]
    summary: dict[str, Any] = {"model": model["model"]}
    if partitions:
        summary["partitions"] = partitions
    if model.get("fusion_health"):
        summary["reconciled_attributes"] = model["fusion_health"]
        unresolved = [
            f"{health['entity']}.{health['attribute']}: {health['unresolved']} of {health['total_keys']} keys"
            for health in model["fusion_health"]
            if health.get("unresolved")
        ]
        if unresolved:
            summary["unresolved"] = unresolved
        errors = [health["probe_error"] for health in model["fusion_health"] if health.get("probe_error")]
        if errors:
            summary["probe_errors"] = errors
    return summary


def _fusion_part_key(part: dict[str, Any]) -> tuple[Any, ...]:
    return (part["entity"], part["aspect"], part["representation"], part["attribute"])


def _fusion_part_name(key: tuple[Any, ...]) -> str:
    entity, aspect, representation, attribute = key
    return " ".join(str(value) for value in (entity, aspect.lower(), representation or attribute) if value)


def _fusion_changes(built: list[dict[str, Any]], models: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What changed in each fused model's layout and attribute health since the build snapshot."""

    current = {model["model"]: model for model in models if model.get("fusion_layout")}
    changes: dict[str, list[str]] = {}
    for entry in built:
        if entry.get("lane") != "fusion" or entry.get("model") not in current:
            continue
        model = current[entry["model"]]
        found: list[str] = []
        before = {_fusion_part_key(part): part for part in entry.get("fusion_layout") or []}
        after = {_fusion_part_key(part): part for part in model["fusion_layout"]}
        for key in sorted(before.keys() | after.keys(), key=lambda value: tuple(str(item) for item in value)):
            old, new = before.get(key), after.get(key)
            name = _fusion_part_name(key)
            if old is None:
                found.append(f"layout: added {name}")
            elif new is None:
                found.append(f"layout: removed {name}")
            elif (old["coverage"], old["strategy"]) != (new["coverage"], new["strategy"]):
                if old["coverage"] != new["coverage"]:
                    found.append(f"layout: {name} coverage {old['coverage']} -> {new['coverage']}")
                else:
                    found.append(f"layout: {name} strategy {old['strategy']} -> {new['strategy']}")
        built_health = {(h["entity"], h["attribute"]): h for h in entry.get("fusion_health") or []}
        for health in model.get("fusion_health") or []:
            old_health = built_health.get((health["entity"], health["attribute"]))
            if old_health is None:
                continue
            moved = [
                f"{label} {old_health[field]} -> {health[field]}"
                for field, label in (("unresolved", "unresolved"), ("filled", "filled"), ("total_keys", "keys"))
                if old_health.get(field) != health.get(field)
            ]
            if moved:
                found.append(f"health: {health['entity']}.{health['attribute']} " + ", ".join(moved))
        if found:
            changes[entry["model"]] = found
    return changes


def _first_issue_text(model: dict[str, Any]) -> str:
    """``": RULE_CODE on object: message"`` for a model's first validation issue, or ``""``."""

    issues = model.get("issues") or []
    if not issues:
        return ""
    issue = issues[0]
    where = f" on {issue['object_name']}" if issue.get("object_name") else ""
    return f": {issue['rule_code']}{where}: {str(issue['message']).rstrip('.')}"


def _changed_definitions(built: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fields whose definition hash differs between the build-time and the current compile."""

    current_by_path = {entry.get("path"): entry for entry in current}
    changed = []
    for entry in built:
        now = current_by_path.get(entry.get("path"))
        if now is None:
            continue
        for kind in ("metrics", "dimensions"):
            for field, built_hash in (entry.get(kind) or {}).items():
                current_hash = (now.get(kind) or {}).get(field)
                if current_hash is not None and current_hash != built_hash:
                    changed.append(
                        {
                            "path": entry.get("path"),
                            "model": entry.get("model"),
                            "field": field,
                            "kind": kind[:-1],
                            "built_hash": built_hash,
                            "current_hash": current_hash,
                        }
                    )
    return changed


def _data_layer_record_resolved(diagnostics: Any, app_name: str, record: dict[str, Any]) -> bool:
    """True when the record's SQL file has run successfully in its revision since the error."""

    sql_file = (record.get("details") or {}).get("sql_file")
    timestamp = record.get("timestamp")
    if not isinstance(sql_file, str) or not isinstance(timestamp, str):
        return False
    last_success = diagnostics.data_layer_last_success(
        app_name, sql_file=sql_file, revision_number=record.get("revision_number")
    )
    if last_success is None:
        return False
    # Compare as datetimes: the stored strings drop the fraction when it is zero, so
    # they don't sort lexically.
    success_at, error_at = parse_iso8601(last_success), parse_iso8601(timestamp)
    return success_at is not None and error_at is not None and success_at > error_at


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
