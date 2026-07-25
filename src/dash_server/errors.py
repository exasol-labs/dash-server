"""Central registry mapping error categories to wire codes.

The category string is the semantic identity of a ``DashServerError``; the
JSON-RPC code and HTTP status are a function of it. This table is the single
place that function is defined — raise sites supply only the category (plus an
explicit override for the rare site whose semantics genuinely differ, such as
an unauthenticated 401 under an authorization-denied category).
"""

from __future__ import annotations

# JSON-RPC 2.0 framing codes (spec-defined, not category-driven).
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# Fallback for categories not yet registered.
DEFAULT_CODES = (-32000, 400)

# category -> (jsonrpc_code, http_status)
CATEGORY_CODES: dict[str, tuple[int, int]] = {
    "app_authorization_denied": (-32030, 403),
    "app_conflict": (-32001, 409),
    "app_delete_confirmation_error": (-32602, 400),
    "app_not_found": (-32004, 404),
    "app_revision_not_found": (-32004, 404),
    "artifact_error": (-32008, 500),
    "auth_provider_error": (-32020, 400),
    "artifact_preflight_failed": (-32010, 409),
    "build_validation_error": (-32602, 400),
    "bundle_validation_error": (-32602, 400),
    "consumption_artifact_expired": (-32004, 410),
    "consumption_artifact_not_found": (-32004, 404),
    "consumption_artifact_not_ready": (-32038, 409),
    "consumption_authorization_denied": (-32030, 403),
    "consumption_contract_hash_mismatch": (-32012, 500),
    "consumption_contract_validation_error": (-32602, 400),
    "consumption_csrf_invalid": (-32030, 403),
    "consumption_executor_unavailable": (-32012, 500),
    "consumption_export_limit_exceeded": (-32033, 413),
    "consumption_exports_disabled": (-32030, 403),
    "consumption_format_unavailable": (-32602, 400),
    "consumption_idempotency_conflict": (-32037, 409),
    "consumption_idempotency_key_invalid": (-32602, 400),
    "consumption_job_cancelled": (-32032, 409),
    "consumption_job_not_found": (-32004, 404),
    "consumption_not_configured": (-32012, 500),
    "consumption_output_not_found": (-32004, 404),
    "consumption_output_preflight_failed": (-32034, 422),
    "consumption_parameter_decode_error": (-32012, 500),
    "consumption_parameter_validation_error": (-32602, 400),
    "consumption_profile_not_found": (-32034, 422),
    "consumption_query_failed": (-32035, 502),
    "consumption_query_timeout": (-32036, 408),
    "consumption_quota_exceeded": (-32039, 429),
    "consumption_source_not_found": (-32004, 404),
    "consumption_streaming_unsupported": (-32012, 500),
    "consumption_token_expired": (-32031, 410),
    "consumption_token_invalid": (-32030, 403),
    "deployment_healthcheck_failed": (-32010, 409),
    "diagnostics_not_found": (-32004, 404),
    "exasol_not_configured": (-32012, 500),
    "exasol_profile_already_exists": (-32013, 409),
    "exasol_profile_not_found": (-32004, 404),
    "exasol_profile_validation_error": (-32602, 400),
    "exasol_runtime_error": (-32012, 500),
    "exasol_schema_scaffold_error": (-32012, 400),
    "exasol_secret_error": (-32011, 400),
    "exposure_validation_error": (-32602, 400),
    "gitops_reconcile_error": (-32010, 400),
    "invalid_resource_uri": (-32602, 400),
    "invitation_not_found": (-32602, 404),
    "manifest_validation_error": (-32602, 400),
    "mcp_authorization_denied": (-32030, 403),
    "oidc_callback_error": (-32020, 400),
    "oidc_claim_error": (-32020, 400),
    "oidc_config_error": (-32020, 400),
    "oidc_nonce_error": (-32020, 400),
    "oidc_state_error": (-32020, 400),
    "oidc_test_token_error": (-32020, 400),
    "patch_error": (-32006, 409),
    "preview_unavailable": (-32005, 409),
    "resource_not_found": (-32602, 400),
    "revision_not_found": (-32004, 404),
    "rollback_unavailable": (-32005, 409),
    "route_conflict": (-32009, 409),
    "runtime_mode_error": (-32603, 400),
    "runtime_mount_error": (-32008, 500),
    "runtime_state_error": (-32603, 400),
    "session_channel_busy": (-32040, 409),
    "session_channel_protocol_error": (-32602, 400),
    "session_channel_session_gone": (-32041, 409),
    "session_channel_timeout": (-32042, 408),
    "session_channel_unavailable": (-32043, 403),
    "share_link_not_found": (-32602, 404),
    "tool_not_found": (-32602, 400),
    "tool_validation_error": (-32602, 400),
    "workspace_constraint_error": (-32006, 409),
    "workspace_file_not_found": (-32004, 404),
    "workspace_not_found": (-32004, 404),
    "workspace_validation_error": (-32007, 409),
}


def codes_for(category: str) -> tuple[int, int]:
    return CATEGORY_CODES.get(category, DEFAULT_CODES)


__all__ = [
    "CATEGORY_CODES",
    "DEFAULT_CODES",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "codes_for",
]
