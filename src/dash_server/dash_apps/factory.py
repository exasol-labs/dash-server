"""Manifest validation and Dash app construction for hosted apps."""

from __future__ import annotations

import re
from typing import Any, cast

from dash import Dash, Input, Output, dcc, html
from flask import Flask

from dash_server.dash_apps.branding import apply_hosted_footer
from dash_server.exceptions import DashServerError
from dash_server.consumption import consumption_contract_hash, normalize_consumption_contract
from dash_server.registry.models import AppManifest

_APP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_APP_CREATE_HELP_RESOURCE = "dash://meta/app-create-schema"
_APP_CREATE_FROM_FILES_HELP_RESOURCE = "dash://meta/app-create-from-files-schema"
_APP_AUTHORING_GUIDE_RESOURCE = "dash://meta/app-authoring-guide"
_SUPPORTED_TEMPLATES = ("metric-cards", "exasol-analytics")


def app_create_example_bundle() -> dict[str, Any]:
    return {
        "manifest": {
            "name": "support",
            "title": "Support Dashboard",
            "route": "/apps/support",
            "description": "Support dashboard hosted by dash-server.",
            "template": "metric-cards",
        },
        "dashboard": {
            "headline": "Support Dashboard",
            "summary": "Key support metrics for the week.",
            "metrics": [
                {"label": "Tickets", "value": "184"},
                {"label": "SLA", "value": "98.2%"},
            ],
        },
    }


def app_create_schema_help() -> dict[str, Any]:
    return {
        "tool": "app_create",
        "help_resource": _APP_CREATE_HELP_RESOURCE,
        "summary": (
            "app_create creates a starter hosted app from metadata. Use a canonical "
            "manifest/dashboard bundle, or use top-level name/title/route shorthand when "
            "you only need a minimal starter app."
        ),
        "template_guide": {
            "metric-cards": "Generic Dash starter with simple metric cards and no profile-bound SQL helpers.",
            "exasol-analytics": "Profile-bound Exasol scaffold with SQL files and runtime helpers. Prefer app_create_exasol_dashboard or app_scaffold_from_schema for this path.",
        },
        "required_shape": {
            "metadata_bundle": {
                "bundle": {
                    "manifest": {
                        "name": "lowercase letters, numbers, hyphens",
                        "title": "non-empty string",
                        "route": "string starting with /apps/ (optional, defaults to /apps/{name})",
                        "description": "string (optional)",
                        "template": "must be one of metric-cards or exasol-analytics if provided",
                        "data_sources": "optional datasource binding metadata",
                        "consumption": "optional registered-output contract; see docs/consumption.md",
                    },
                    "dashboard": {
                        "headline": "non-empty string (optional)",
                        "summary": "non-empty string (optional)",
                        "metrics": [
                            {
                                "label": "non-empty string",
                                "value": "non-empty string",
                            }
                        ],
                    },
                }
            },
            "top_level_shorthand": {
                "name": "lowercase letters, numbers, hyphens",
                "title": "optional, defaults to a humanized name",
                "route": "optional, defaults to /apps/{name}",
            },
        },
        "common_mistakes": [
            "Putting manifest fields directly at bundle root instead of under bundle.manifest.",
            "Trying to send source files to app_create. Use app_create_from_files for name + files bootstrap.",
            "Passing dashboard as a string or array instead of an object.",
            "Using a route that does not start with /apps/.",
            "Using a template value other than the supported scaffold templates.",
        ],
        "related_tools": ["app_create_from_files", "app_put_files", "app_validate"],
        "example": app_create_example_bundle(),
    }


def app_create_from_files_example() -> dict[str, Any]:
    return {
        "name": "markets-dashboard",
        "title": "Markets Dashboard",
        "files": [
            {
                "path": "app.py",
                "content": (
                    "from dash import Dash, html\n\n"
                    "def create_dash_app(server, url_base_pathname, metadata):\n"
                    "    app = Dash(\n"
                    "        __name__,\n"
                    "        server=server,\n"
                    "        routes_pathname_prefix='/',\n"
                    "        requests_pathname_prefix=url_base_pathname.rstrip('/') + '/',\n"
                    "        title=metadata.get('title', 'Markets Dashboard'),\n"
                    "    )\n"
                    "    app.layout = html.Div([html.H1(metadata.get('title', 'Markets Dashboard'))])\n"
                    "    return app\n"
                ),
            }
        ],
    }


def app_create_from_files_schema_help() -> dict[str, Any]:
    return {
        "tool": "app_create_from_files",
        "help_resource": _APP_CREATE_FROM_FILES_HELP_RESOURCE,
        "summary": (
            "app_create_from_files creates a hosted app and seeds its draft workspace from "
            "explicit files. Use this when you already have app.py, requirements.txt, or assets."
        ),
        "template_guide": {
            "metric-cards": "Use for generic dashboards when uploaded files do not rely on an Exasol profile-bound scaffold.",
            "exasol-analytics": "Use only when uploaded files follow the Exasol SQL-helper scaffold shape and keep credentials in a server-side profile.",
        },
        "required_shape": {
            "name": "lowercase letters, numbers, hyphens",
            "files": [
                {
                    "path": "workspace-relative file path such as app.py or assets/theme.css",
                    "content": "file contents",
                }
            ],
            "title": "optional, defaults to a humanized name",
            "route": "optional, defaults to /apps/{name}",
            "start_immediately": "optional boolean, defaults to true",
        },
        "common_mistakes": [
            "Calling app_create with files instead of using app_create_from_files.",
            "Uploading app.py without defining create_dash_app(server, url_base_pathname, metadata).",
            "Using routes_pathname_prefix=url_base_pathname instead of '/'.",
            "Omitting the files array or sending an empty list.",
            "Forgetting dash_server_exasol.py when template=exasol-analytics is set. The server "
            "auto-injects the canonical helper if you don't supply it, but you should still include "
            "it explicitly when you want to customize it.",
        ],
        "related_tools": ["app_validate", "app_deploy_draft", "app_put_files"],
        "example": app_create_from_files_example(),
    }


def app_authoring_guide() -> dict[str, Any]:
    return {
        "resource": _APP_AUTHORING_GUIDE_RESOURCE,
        "summary": "Author Dash apps as factories that mount cleanly under a dynamic prefix.",
        "factory_signature": "create_dash_app(server, url_base_pathname, metadata)",
        "required_rules": [
            "Return a dash.Dash instance.",
            "Use routes_pathname_prefix='/'",
            "Use requests_pathname_prefix=url_base_pathname.rstrip('/') + '/'",
            "Prefer app.callback(...) or @app.callback inside the factory.",
            "Do not use global dash.callback for hosted apps.",
            "Do not embed database credentials, DSNs, or tokens in hosted app source or manifests.",
            "For Exasol-backed apps, bind metadata.data_sources.primary.profile and let the server resolve credentials.",
            "For Exasol-backed apps, do not call pyexasol.connect(...) directly from app code.",
        ],
        "exasol_rules": [
            "Prefer app_create_exasol_dashboard when starting an Exasol-backed app.",
            "If an external Exasol MCP server is available, use it for schema discovery and SQL design, not for runtime execution inside the hosted app.",
            "If editing an Exasol-backed app, keep the profile name in dash-app.json and let dash_server_exasol.py or server-side helpers execute queries.",
            "Do not read EXASOL_* or EXA_* credential environment variables in app code.",
            "Do not hardcode DSN, user, password, PAT, or token values in app.py or helper modules.",
        ],
        "minimal_app_py": app_create_from_files_example()["files"][0]["content"],
        "common_failures": [
            {
                "problem": "404 at /apps/{name}",
                "cause": "Dash internal prefixes do not match the mounted route.",
                "fix": "Use routes_pathname_prefix='/' and requests_pathname_prefix=url_base_pathname.rstrip('/') + '/'.",
            },
            {
                "problem": "DuplicateCallback during preview or promotion",
                "cause": "Global dash.callback leaked callback registrations across loads.",
                "fix": "Register callbacks on the app instance inside create_dash_app.",
            },
            {
                "problem": "Validation import fails",
                "cause": "Missing dependency or invalid import in app.py.",
                "fix": "Update requirements.txt and run app_validate before app_build.",
            },
            {
                "problem": "Credential safety validation fails",
                "cause": "The draft embeds Exasol connection details or direct pyexasol.connect(...) calls.",
                "fix": "Move credentials into an Exasol profile and use app_create_exasol_dashboard or server-side Exasol helpers.",
            },
            {
                "problem": "app_validate fails with category exasol_query_outside_callback "
                "(or an import_error mentioning 'Exasol dashboard service is not registered')",
                "cause": "load_rows/load_row/query_rows/query_one was called directly in the body of "
                "create_dash_app(), not inside an @app.callback. The validation sandbox and the real "
                "server both only wire the Exasol connection onto the server once request handling "
                "starts, so this fails every time regardless of whether the profile/query are valid.",
                "fix": "Move the query into a callback. For data that only needs to load once at "
                "startup, trigger it from a dcc.Interval(max_intervals=1) callback instead of calling "
                "the helper directly in the factory body.",
            },
            {
                "problem": "app_validate reports is_valid: true, but a SQL file references a "
                "nonexistent column or table",
                "cause": "app_validate is a static/offline check - it never opens an Exasol "
                "connection, so it cannot verify schema/table/column references.",
                "fix": "Run app_build (or app_deploy_draft) to exercise sql_smoke, which actually "
                "runs each queries/*.sql file against the bound profile and catches this. Run it "
                "early rather than trusting app_validate alone for schema correctness.",
            },
            {
                "problem": "app_validate fails with callbacks.status: failed and "
                "missing_layout_ids containing something like "
                '\'{"index":["ALL"],"type":"my-id"}\'',
                "cause": "A callback uses a Dash pattern-matching id (Input({'type': ..., "
                "'index': ALL}, ...) or MATCH/ALLSMALLER) whose concrete components are "
                "rendered dynamically by another callback, not present in the app's static "
                "initial layout - the standard Dash idiom for e.g. 'acknowledge one of N "
                "dynamically-rendered rows'. The response's callbacks.hint field names this "
                "directly when it applies.",
                "fix": "Set suppress_callback_exceptions=True in the Dash(__name__, ...) "
                "constructor. This is expected and safe for this pattern - it does not "
                "suppress genuine errors in registered callbacks, only the layout-presence "
                "check for ids Dash can't yet see in the initial render.",
            },
            {
                "problem": "A dashboard's 'no filter selected -> show everything' KPI is "
                "quietly wrong (undercounts or overcounts), even though app_validate, "
                "app_build, and app_run_healthcheck all report healthy",
                "cause": "pyexasol's {name!s}/{name!d} list-placeholder formatting raises on "
                "an empty Python list, so an empty-selection filter can't pass one through - a "
                "common fix is a hardcoded stand-in list of 'every known value' for the "
                "IN (...) clause. That literal list silently stops covering reality the moment "
                "the underlying column gains a new distinct value - confirmed directly: a "
                "hardcoded 3-region fallback undercounted a table that grew a 4th region by "
                "32%, with every automated check reporting healthy throughout, since none of "
                "them check whether a hardcoded value list still matches live data.",
                "fix": "Query 'SELECT DISTINCT <column>' live instead of hardcoding the "
                "fallback list for an empty-selection filter.",
            },
        ],
        "recommended_workflow": [
            "Use app_create for a starter app or app_create_from_files for source bootstrap.",
            "Use metric-cards for generic dashboard starters and exasol-analytics for profile-bound Exasol scaffolds.",
            "Use app_put_files or app_patch_file to edit the draft workspace.",
            "Run app_validate until validation passes.",
            "Run app_deploy_draft for a one-shot validate/build/promote flow.",
        ],
        # PS26-BUG-013: app_run_healthcheck's sql_smoke and layout/dependencies probes
        # never execute the app's actual @app.callback code - sql_smoke runs each
        # queries/*.sql file directly through an uncached connection, entirely
        # independent of app.py, and the layout/dependencies probes are plain GETs. A
        # "healthy" result carries no guarantee the callback graph has ever run. The
        # only in-band way to force real execution is a raw POST to
        # `_dash-update-component`, which was previously undocumented anywhere
        # MCP-reachable and cost real investigation time to reverse-engineer.
        "triggering_callbacks_for_real": {
            "note": (
                "app_run_healthcheck's probes do not execute callbacks. To actually run "
                "one, POST the Dash internal request Dash's own frontend sends on every "
                "interaction to {mount_path}/_dash-update-component."
            ),
            "request_shape": {
                "output": "the single output id.property string, e.g. 'callback-result.children'",
                "outputs": "{'id': ..., 'property': ...} - or a list of those for a multi-output callback",
                "inputs": "[{'id': ..., 'property': ..., 'value': ...}, ...] - one entry per Input, "
                "with the value you want the callback to see",
                "changedPropIds": "['<id>.<property>', ...] - which of the inputs above actually changed; "
                "getting this list wrong does not 400, it can 500 with an opaque KeyError",
                "state": "[{'id': ..., 'property': ..., 'value': ...}, ...] - one entry per State, or []",
            },
            "example": {
                "method": "POST",
                "path": "{mount_path}/_dash-update-component",
                "json": {
                    "output": "callback-result.children",
                    "outputs": {"id": "callback-result", "property": "children"},
                    "inputs": [{"id": "mode", "property": "value", "value": "safe"}],
                    "changedPropIds": ["mode.value"],
                    "state": [],
                },
            },
            "on_success": "200 with the rendered output value(s) in the response body.",
            "on_failure": (
                "A genuine callback bug returns 500 and is recorded (category "
                "dash_callback_error) at dash://apps/{name}/callback-failures - check "
                "that before assuming the request shape itself was wrong."
            ),
            "related_resources": ["dash://apps/{app}/callback-failures"],
        },
    }


def validate_bundle(bundle: dict[str, Any]) -> tuple[AppManifest, dict[str, Any]]:
    """Validate the constrained bundle payload used by the current scaffold."""

    if not isinstance(bundle, dict):
        raise DashServerError(
            category="bundle_validation_error",
            summary="Bundle must be an object.",
            details={
                "field": "bundle",
                "expected": "object",
                **app_create_schema_help(),
            },
        )

    normalized_bundle = _normalize_bundle_shape(bundle)
    manifest = validate_manifest_payload(normalized_bundle.get("manifest"))
    dashboard = _validate_dashboard(normalized_bundle.get("dashboard"), manifest)
    return manifest, dashboard


def build_dash_wsgi_app(
    manifest: AppManifest,
    dashboard: dict[str, Any],
    mount_prefix: str | None = None,
    *,
    revision_number: int | None = None,
) -> Flask:
    """Create a self-contained Dash WSGI app for a hosted route prefix."""

    server = Flask(f"dash_server.runtime.{manifest.name}")
    resolved_mount_prefix = (mount_prefix or manifest.route).rstrip("/") or "/"
    dash_app = Dash(
        manifest.name,
        server=cast(Any, server),
        routes_pathname_prefix="/",
        requests_pathname_prefix=f"{resolved_mount_prefix}/",
        title=manifest.title,
    )

    metrics = dashboard["metrics"]
    dropdown_options = [
        {"label": metric["label"], "value": metric["label"]}
        for metric in metrics
    ]

    dash_app.layout = html.Div(
        [
            html.H1(dashboard["headline"]),
            html.P(dashboard["summary"]),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(metric["label"], className="metric-label"),
                            html.Strong(metric["value"], className="metric-value"),
                        ],
                        className="metric-card",
                    )
                    for metric in metrics
                ],
                className="metric-grid",
            ),
            html.H2("Highlight"),
            dcc.Dropdown(
                id="metric-selector",
                options=dropdown_options,
                value=dropdown_options[0]["value"],
                clearable=False,
            ),
            html.Div(id="metric-detail"),
        ],
        style={
            "fontFamily": "sans-serif",
            "margin": "2rem auto",
            "maxWidth": "960px",
        },
    )

    lookup = {metric["label"]: metric["value"] for metric in metrics}

    @dash_app.callback(Output("metric-detail", "children"), Input("metric-selector", "value"))
    def show_metric(selected_metric: str) -> str:
        return f"{selected_metric}: {lookup[selected_metric]}"

    apply_hosted_footer(
        dash_app,
        mount_path=resolved_mount_prefix,
        revision_number=revision_number,
        app_name=manifest.name,
        has_consumption_outputs=bool((manifest.consumption or {}).get("outputs")),
        wrap=True,
    )
    return server


def validate_manifest_payload(raw_manifest: Any) -> AppManifest:
    if not isinstance(raw_manifest, dict):
        raise DashServerError(
            category="manifest_validation_error",
            summary="Bundle manifest must be an object.",
            details={
                "field": "manifest",
                **app_create_schema_help(),
            },
        )

    name = _require_non_empty_string(raw_manifest.get("name"), "manifest.name")
    if not _APP_NAME_PATTERN.match(name):
        raise DashServerError(
            category="manifest_validation_error",
            summary="Manifest name must be lowercase letters, numbers, or hyphens.",
            details={
                "field": "manifest.name",
                "value": name,
                "expected_pattern": _APP_NAME_PATTERN.pattern,
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )

    title = _require_non_empty_string(raw_manifest.get("title"), "manifest.title")
    route = raw_manifest.get("route") or f"/apps/{name}"
    if not isinstance(route, str) or not route.startswith("/apps/"):
        raise DashServerError(
            category="manifest_validation_error",
            summary="Manifest route must start with /apps/.",
            details={
                "field": "manifest.route",
                "value": route,
                "expected_prefix": "/apps/",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )

    description = raw_manifest.get("description") or f"{title} hosted by dash-server."
    if not isinstance(description, str):
        raise DashServerError(
            category="manifest_validation_error",
            summary="Manifest description must be a string.",
            details={
                "field": "manifest.description",
                "expected": "string",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )

    template = raw_manifest.get("template") or "metric-cards"
    if template not in _SUPPORTED_TEMPLATES:
        raise DashServerError(
            category="manifest_validation_error",
            summary="Only supported scaffold templates may be used.",
            details={
                "field": "manifest.template",
                "value": template,
                "supported_values": list(_SUPPORTED_TEMPLATES),
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )

    raw_data_sources = raw_manifest.get("data_sources")
    if raw_data_sources is not None and not isinstance(raw_data_sources, dict):
        raise DashServerError(
            category="manifest_validation_error",
            summary="Manifest data_sources must be an object when provided.",
            details={
                "field": "manifest.data_sources",
                "expected": "object",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )
    if isinstance(raw_data_sources, dict):
        primary_source = raw_data_sources.get("primary")
        if isinstance(primary_source, dict) and primary_source.get("kind") == "exasol":
            forbidden_keys = sorted(
                key
                for key in primary_source
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
                raise DashServerError(
                    category="manifest_validation_error",
                    summary="Exasol data_sources must reference a profile and must not embed connection credentials or endpoints.",
                    details={
                        "field": "manifest.data_sources.primary",
                        "forbidden_keys": forbidden_keys,
                        "help_resource": _APP_AUTHORING_GUIDE_RESOURCE,
                    },
                )

    consumption = normalize_consumption_contract(
        raw_manifest.get("consumption"),
        data_sources=raw_data_sources,
    )

    return AppManifest(
        name=name,
        title=title,
        route=route.rstrip("/") or route,
        description=description,
        template=template,
        data_sources=raw_data_sources,
        consumption=consumption,
        consumption_contract_hash=consumption_contract_hash(consumption),
    )


def _validate_dashboard(raw_dashboard: Any, manifest: AppManifest) -> dict[str, Any]:
    if raw_dashboard is None:
        raw_dashboard = {}
    if not isinstance(raw_dashboard, dict):
        raise DashServerError(
            category="bundle_validation_error",
            summary="Bundle dashboard config must be an object.",
            details={
                "field": "dashboard",
                "expected": "object",
                **app_create_schema_help(),
            },
        )

    headline = raw_dashboard.get("headline") or manifest.title
    summary = raw_dashboard.get("summary") or manifest.description
    metrics = raw_dashboard.get("metrics") or [
        {"label": "Status", "value": "Created"},
        {"label": "Route", "value": manifest.route},
    ]

    if not isinstance(headline, str) or not headline.strip():
        raise DashServerError(
            category="bundle_validation_error",
            summary="Dashboard headline must be a non-empty string.",
            details={
                "field": "dashboard.headline",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )
    if not isinstance(summary, str) or not summary.strip():
        raise DashServerError(
            category="bundle_validation_error",
            summary="Dashboard summary must be a non-empty string.",
            details={
                "field": "dashboard.summary",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )
    if not isinstance(metrics, list) or not metrics:
        raise DashServerError(
            category="bundle_validation_error",
            summary="Dashboard metrics must be a non-empty array.",
            details={
                "field": "dashboard.metrics",
                "expected": "non-empty array of {label, value} objects",
                "help_resource": _APP_CREATE_HELP_RESOURCE,
            },
        )

    normalized_metrics: list[dict[str, str]] = []
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            raise DashServerError(
                category="bundle_validation_error",
                summary="Each dashboard metric must be an object.",
                details={
                    "field": f"dashboard.metrics[{index}]",
                    "expected": "object with label and value",
                    "help_resource": _APP_CREATE_HELP_RESOURCE,
                },
            )
        label = _require_non_empty_string(metric.get("label"), f"dashboard.metrics[{index}].label")
        value = _require_non_empty_string(metric.get("value"), f"dashboard.metrics[{index}].value")
        normalized_metrics.append({"label": label, "value": value})

    return {
        "headline": headline.strip(),
        "summary": summary.strip(),
        "metrics": normalized_metrics,
    }


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DashServerError(
        category="manifest_validation_error",
        summary=f"{field_name} must be a non-empty string.",
        details={
            "field": field_name,
            "help_resource": _APP_CREATE_HELP_RESOURCE,
        },
    )


def _normalize_bundle_shape(bundle: dict[str, Any]) -> dict[str, Any]:
    manifest = bundle.get("manifest")
    dashboard = bundle.get("dashboard")
    if isinstance(manifest, dict):
        return {
            "manifest": manifest,
            "dashboard": dashboard,
        }

    root_manifest_fields = {
        key: bundle[key]
        for key in (
            "name",
            "title",
            "route",
            "description",
            "template",
            "data_sources",
            "consumption",
        )
        if key in bundle
    }
    root_dashboard_fields = {
        key: bundle[key]
        for key in ("headline", "summary", "metrics")
        if key in bundle
    }

    if root_manifest_fields:
        name = root_manifest_fields.get("name")
        if isinstance(name, str) and name and "title" not in root_manifest_fields:
            root_manifest_fields["title"] = _humanize_name(name)
        normalized_dashboard = dashboard if dashboard is not None else root_dashboard_fields or None
        return {
            "manifest": root_manifest_fields,
            "dashboard": normalized_dashboard,
        }

    return bundle


def is_files_bundle_shape(bundle: Any) -> bool:
    return isinstance(bundle, dict) and "manifest" not in bundle and "files" in bundle


def canonicalize_files_bundle(bundle: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    files = bundle.get("files")
    if not isinstance(files, list) or not files:
        raise DashServerError(
            category="bundle_validation_error",
            summary="files-based app_create bundles must include a non-empty files array.",
            details={
                "field": "bundle.files",
                "tool": "app_create_from_files",
                "help_resource": _APP_CREATE_FROM_FILES_HELP_RESOURCE,
                "received_bundle_keys": sorted(bundle.keys()),
            },
        )

    name = _require_non_empty_string(bundle.get("name"), "bundle.name")
    title = bundle.get("title") if isinstance(bundle.get("title"), str) else _humanize_name(name)
    route = bundle.get("route") or f"/apps/{name}"
    description = bundle.get("description") or f"{title} hosted by dash-server."
    template = bundle.get("template") or "metric-cards"
    data_sources = bundle.get("data_sources")
    headline = bundle.get("headline") or title
    summary = bundle.get("summary") or description
    metrics = bundle.get("metrics") or [
        {"label": "Status", "value": "Created"},
        {"label": "Route", "value": route},
    ]

    canonical_bundle = {
        "manifest": {
            "name": name,
            "title": title,
            "route": route,
            "description": description,
            "template": template,
            "data_sources": data_sources,
            "consumption": bundle.get("consumption"),
        },
        "dashboard": {
            "headline": headline,
            "summary": summary,
            "metrics": metrics,
        },
    }
    validate_bundle(canonical_bundle)
    return canonical_bundle, files


def _humanize_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))
