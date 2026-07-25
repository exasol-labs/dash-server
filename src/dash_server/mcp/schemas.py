"""JSON-schema builders for MCP tool ``inputSchema`` definitions."""

from __future__ import annotations

from typing import Any

from dash_server.dash_apps.factory import (
    app_create_example_bundle,
    app_create_from_files_example,
)


class SchemasMixin:
    """The ``_*_schema()`` builders referenced by ``ToolSpec.input_schema``."""

    def _name_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Hosted app name.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _job_id_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Consumption export job id.",
                }
            },
            "required": ["job_id"],
            "additionalProperties": False,
        }


    def _exasol_profile_create_local_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Profile identifier stored under profiles/exasol/{name}.json.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["onprem", "saas"],
                    "description": "Backend type. onprem supports password/access/refresh token; saas supports saas_pat.",
                },
                "credential_mode": {
                    "type": "string",
                    "enum": ["password", "access_token", "refresh_token", "saas_pat"],
                    "description": "Credential mode for the bound secret.",
                },
                "dsn": {
                    "type": "string",
                    "description": "Exasol DSN or database endpoint used by pyexasol.",
                },
                "user": {
                    "type": "string",
                    "description": "Database username for the profile.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional human-readable description.",
                },
                "tls_verify": {
                    "type": "boolean",
                    "description": "Enable TLS certificate validation. Defaults to true.",
                    "default": True,
                },
                "secret_value": {
                    "type": "string",
                    "description": "Secret to persist in the local secret store. Provide exactly one of secret_value or secret_env_var.",
                },
                "secret_env_var": {
                    "type": "string",
                "description": "Environment variable containing the secret. Provide exactly one of secret_value or secret_env_var.",
                },
                "statement_timeout_seconds": {"type": "integer", "minimum": 1},
                "row_limit": {"type": "integer", "minimum": 1},
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "When false (default), the call fails with `exasol_profile_already_exists` "
                        "if a profile with this name is already on disk. Pass `true` to rewrite "
                        "the metadata (the response will set `was_already_present: true`). This "
                        "matches the persona-1 expectation that the same name isn't silently clobbered."
                    ),
                    "default": False,
                },
            },
            "required": ["name", "backend", "credential_mode", "dsn", "user"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "analytics-prod",
                    "backend": "onprem",
                    "credential_mode": "password",
                    "dsn": "demodb.exasol.com:8563",
                    "user": "sys",
                    "secret_env_var": "EXA_PASSWORD",
                    "tls_verify": True,
                }
            ],
        }


    def _app_create_exasol_dashboard_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "Create a hosted Exasol dashboard from a server-side profile. "
                "Use this instead of writing pyexasol.connect(...) or embedding DSN/user/password/token values in app code."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Hosted app name for the generated Exasol dashboard.",
                },
                "profile_name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Existing Exasol profile to bind into dash-app.json data_sources.primary.profile.",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["analytics-hub", "overview", "kpi-trend", "ops-monitor"],
                    "description": (
                        "Scaffold pattern. analytics-hub is the default exasol-analytics template: "
                        "system health tab, query history tab, and a business analytics placeholder."
                    ),
                    "default": "analytics-hub",
                },
                "title": {"type": "string", "description": "Optional dashboard title."},
                "route": {"type": "string", "description": "Optional route. Defaults to /apps/{name}."},
                "description": {"type": "string", "description": "Optional dashboard description."},
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the generated dashboard immediately.",
                    "default": True,
                },
            },
            "required": ["name", "profile_name"],
            "additionalProperties": False,
            "examples": [
                {"name": "sales-overview", "profile_name": "analytics-prod", "pattern": "analytics-hub", "start_immediately": True}
            ],
        }


    def _app_scaffold_from_schema_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "Create a schema-tailored Exasol scaffold by introspecting visible Exasol tables and columns. "
                "The generated app uses the exasol-analytics template with system tabs plus business SQL seeded from the selected schema."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Hosted app name for the generated schema scaffold.",
                },
                "profile_name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Existing Exasol profile to bind into dash-app.json data_sources.primary.profile.",
                },
                "schema_name": {
                    "type": "string",
                    "description": "Optional schema to prioritize during introspection. When omitted, the tool picks from visible non-system schemas.",
                },
                "table_name": {
                    "type": "string",
                    "description": "Optional specific table inside schema_name to base the scaffold on. When omitted, the highest-scoring table in the schema is picked automatically. schema_blueprint.table_candidates lists alternatives.",
                },
                "title": {"type": "string", "description": "Optional dashboard title."},
                "route": {"type": "string", "description": "Optional route. Defaults to /apps/{name}."},
                "description": {"type": "string", "description": "Optional dashboard description."},
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the generated dashboard immediately.",
                    "default": True,
                },
            },
            "required": ["name", "profile_name"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "sales-orders",
                    "profile_name": "analytics-prod",
                    "schema_name": "SALES",
                    "start_immediately": True,
                }
            ],
        }


    def _app_create_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bundle": self._bundle_schema(),
                "name": {
                    "type": "string",
                    "description": (
                        "Compatibility shorthand. If bundle is omitted, app_create treats "
                        "top-level name/title/route fields as starter-app metadata."
                    ),
                },
                "title": {"type": "string"},
                "route": {"type": "string", "description": "Optional live route for shorthand creation."},
                "description": {"type": "string", "description": "Optional app description for shorthand creation."},
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Optional scaffold template for shorthand creation. metric-cards is the generic starter; exasol-analytics is the profile-bound Exasol scaffold shape.",
                },
                "data_sources": {
                    "type": "object",
                    "description": "Optional datasource bindings for shorthand creation.",
                },
                "consumption": {
                    "type": "object",
                    "description": "Optional registered-output contract for governed consumption.",
                },
                "headline": {"type": "string", "description": "Optional starter dashboard headline for shorthand creation."},
                "summary": {"type": "string", "description": "Optional starter dashboard summary for shorthand creation."},
                "metrics": {
                    "type": "array",
                    "description": "Optional starter metric cards for shorthand creation.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the initial revision immediately at the live route.",
                    "default": True,
                },
            },
            "anyOf": [{"required": ["bundle"]}, {"required": ["name"]}],
            "additionalProperties": False,
            "examples": [
                {
                    "bundle": app_create_example_bundle(),
                    "start_immediately": True,
                },
                {
                    "name": "markets-dashboard",
                    "start_immediately": True,
                }
            ],
        }


    def _app_create_from_files_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Required app identifier.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title. Defaults to a humanized form of name.",
                },
                "route": {
                    "type": "string",
                    "description": "Optional live route. Defaults to /apps/{name}.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional app description.",
                },
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Optional scaffold template used for generated metadata defaults. metric-cards is generic; exasol-analytics means the uploaded files should match the Exasol SQL-helper layout.",
                },
                "data_sources": {
                    "type": "object",
                    "description": (
                        "Optional data-source bindings. For an Exasol-bound app use "
                        "`{\"primary\": {\"kind\": \"exasol\", \"profile\": \"<profile-name>\"}}`. "
                        "Without this, the runtime helper can't resolve a profile and the first "
                        "callback will 500."
                    ),
                    "properties": {
                        "primary": {
                            "type": "object",
                            "description": "Primary data source. For Exasol: `{kind: 'exasol', profile: 'name'}`.",
                            "properties": {
                                "kind": {"type": "string"},
                                "profile": {"type": "string"},
                            },
                            "required": ["kind"],
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": True,
                },
                "consumption": {
                    "type": "object",
                    "description": "Optional registered-output contract for governed consumption.",
                },
                "headline": {"type": "string", "description": "Optional starter dashboard headline used for generated metadata defaults."},
                "summary": {"type": "string", "description": "Optional starter dashboard summary used for generated metadata defaults."},
                "metrics": {
                    "type": "array",
                    "description": "Optional starter metric cards used for generated metadata defaults.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                    "description": "Workspace files to seed into the draft after app creation.",
                },
                "start_immediately": {
                    "type": "boolean",
                    "description": "If true, mount the initial revision immediately at the live route.",
                    "default": True,
                },
            },
            "required": ["name", "files"],
            "additionalProperties": False,
            "examples": [app_create_from_files_example()],
        }


    def _bundle_schema(self) -> dict[str, Any]:
        manifest_props = self._manifest_schema()["properties"]
        dashboard_props = self._dashboard_schema()["properties"]
        return {
            "type": "object",
            "description": (
                "Canonical metadata bundle with top-level manifest and dashboard objects. "
                "app_create only accepts metadata here. Do not include source files in bundle; "
                "use app_create_from_files for name + files bootstrap. Shorthand: manifest "
                "and dashboard fields may also appear directly at the bundle root."
            ),
            "properties": {
                "manifest": self._manifest_schema(),
                "dashboard": self._dashboard_schema(),
                **manifest_props,
                **dashboard_props,
            },
            "anyOf": [
                {"required": ["manifest"]},
                {"required": ["name"]},
            ],
            "additionalProperties": False,
            "examples": [app_create_example_bundle()],
        }


    def _manifest_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": "Metadata for the hosted app.",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9-]*$",
                    "description": "Lowercase app identifier used in routes and registry records.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for the app.",
                },
                "route": {
                    "type": "string",
                    "description": "Live route. Must start with /apps/. Defaults to /apps/{name}.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional app description.",
                },
                "template": {
                    "type": "string",
                    "enum": ["metric-cards", "exasol-analytics"],
                    "description": "Supported starter templates. metric-cards is the generic dashboard starter; exasol-analytics is the Exasol SQL-helper scaffold.",
                },
                "data_sources": {
                    "type": "object",
                    "description": "Optional datasource bindings such as data_sources.primary.profile for Exasol-backed apps.",
                },
                "consumption": {
                    "type": "object",
                    "description": "Optional registered-output contract for governed datasets and views.",
                },
            },
            "required": ["name", "title"],
            "additionalProperties": False,
        }


    def _dashboard_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": "Initial dashboard content for the metric-cards template. Exasol scaffolds primarily use generated SQL files instead of these starter metrics.",
            "properties": {
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                    "description": "At least one metric card to render.",
                },
            },
            "additionalProperties": False,
        }


    def _revision_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "revision_number": {"type": "integer", "description": "Numeric revision to preview or promote."},
            },
            "required": ["name", "revision_number"],
            "additionalProperties": False,
        }


    def _app_diff_draft_vs_artifact_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "revision_number": {
                    "type": "integer",
                    "description": "Optional built revision to compare against. Defaults to the latest built revision.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_build_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "bundle": {
                    **self._bundle_schema(),
                    "description": (
                        "Optional replacement bundle to load into the draft before "
                        "building. Must match the same shape as app_create.bundle."
                    ),
                },
                "force_clean": {
                    "type": "boolean",
                    "description": "Bypass cached dependency-install state before validation/build. Does not change source snapshotting.",
                    "default": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_deploy_draft_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "deployment_target": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "description": "Where to mount the newly built revision. live publishes at /apps/{name}; preview mounts at /preview/{name}/{revision}.",
                    "default": "live",
                },
                "auto_rollback_on_health_failure": {
                    "type": "boolean",
                    "description": "When deploying live, automatically roll back to the previous live revision if post-deploy health checks fail.",
                    "default": False,
                },
                "force_clean": {
                    "type": "boolean",
                    "description": "Bypass cached dependency-install state before validation/build. Does not change source snapshotting.",
                    "default": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_healthcheck_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "target": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "description": "Which mounted route to probe.",
                    "default": "live",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_share_grant_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "principal_type": {
                    "type": "string",
                    "enum": ["user", "group", "domain", "organization", "public"],
                },
                "principal_id": {
                    "type": "string",
                    "description": "Stable principal identifier, such as issuer:subject for users or external_id for groups.",
                },
                "display_name": {
                    "type": "string",
                    "description": "Optional display name when creating a local group grant.",
                },
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer", "editor", "owner"],
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview", "manage", "all"],
                    "default": "live",
                },
            },
            "required": ["name", "principal_type", "principal_id", "role"],
            "additionalProperties": False,
        }


    def _app_share_revoke_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "grant_id": {"type": "integer", "description": "Grant id returned by app_share_get."},
                "principal_type": {
                    "type": "string",
                    "enum": ["user", "group", "domain", "organization", "public"],
                },
                "principal_id": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_share_set_link_scope_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "link_scope": {
                    "type": "string",
                    "enum": ["restricted", "organization", "domain", "anyone_with_link", "public"],
                },
                "allowed_domain": {
                    "type": "string",
                    "description": "Required when link_scope=domain. Only exact email-domain matches can discover the app through this policy.",
                },
                "default_link_role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "allow_preview_link": {"type": "boolean", "default": False},
                "public_catalog_visible": {"type": "boolean"},
                "external_sharing_enabled": {"type": "boolean", "default": False},
            },
            "required": ["name", "link_scope"],
            "additionalProperties": False,
        }


    def _app_share_explain_access_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "target": {"type": "string", "enum": ["live", "preview"], "default": "live"},
                "principal_id": {
                    "type": "string",
                    "description": "Optional principal to explain. Defaults to the current request principal.",
                },
                "email": {"type": "string"},
                "tenant_id": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "roles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_share_create_one_time_link_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "default": "live",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": "How long the link can be redeemed. Defaults to 168 hours.",
                    "default": 168,
                },
                "recipient_email": {
                    "type": "string",
                    "description": "Optional intended recipient email for operator context. It is not a verified identity by itself.",
                },
                "recipient_note": {
                    "type": "string",
                    "description": "Optional note for the owner/admin. Do not place secrets here.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_share_revoke_one_time_link_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "link_id": {"type": "integer", "description": "One-time link id returned by app_share_create_one_time_link."},
            },
            "required": ["name", "link_id"],
            "additionalProperties": False,
        }


    def _app_invite_external_user_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "recipient_email": {
                    "type": "string",
                    "description": "Email address that will own the accepted external user grant.",
                },
                "role": {
                    "type": "string",
                    "enum": ["viewer", "preview_viewer"],
                    "default": "viewer",
                },
                "scope": {
                    "type": "string",
                    "enum": ["live", "preview"],
                    "default": "live",
                },
                "ttl_hours": {
                    "type": "integer",
                    "description": "How long the invitation can be accepted. Defaults to 168 hours.",
                    "default": 168,
                },
                "message": {
                    "type": "string",
                    "description": "Optional owner/admin note for the invitation record. Do not place secrets here.",
                },
            },
            "required": ["name", "recipient_email"],
            "additionalProperties": False,
        }


    def _app_revoke_external_invitation_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "invitation_id": {
                    "type": "integer",
                    "description": "Invitation id returned by app_invite_external_user.",
                },
            },
            "required": ["name", "invitation_id"],
            "additionalProperties": False,
        }


    def _app_tail_logs_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "channel": {
                    "type": "string",
                    "enum": list(self._log_channels),
                    "description": "Log channel. Use build for validation/build workflow logs.",
                },
                "limit": {"type": "integer"},
            },
            "required": ["name"],
            "additionalProperties": False,
        }


    def _app_session_eval_js_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Hosted app name."},
                "code": {
                    "type": "string",
                    "description": (
                        "JavaScript to evaluate in the tab. A trailing expression is returned; "
                        "`await` is allowed. Use the `ctx` helpers (ctx.props, ctx.dom, ctx.plots, "
                        "ctx.stores, ctx.page, ctx.setProps, ctx.waitForIdle) — read "
                        "dash://meta/session-channel-guide first."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Target tab, or 'auto' (default) for the most-recently-polled live "
                        "session of this app. List candidates with app_sessions_list."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Wall-clock deadline for the round trip. Default 10.",
                },
            },
            "required": ["name", "code"],
            "additionalProperties": False,
        }


    def _app_sessions_list_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Restrict to one app. Omit to list every registered tab.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }
