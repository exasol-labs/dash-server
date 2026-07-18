"""Shared authorization-aware consumption discovery service."""

from __future__ import annotations

from typing import Any

from dash_server.auth import AuthContext, AuthorizationService
from dash_server.exceptions import DashServerError
from dash_server.registry.sqlite_registry import SQLiteAppRegistry

from .contract import consumption_contract_hash, normalize_consumption_contract
from .models import ConsumptionExecutionContext, ConsumptionPolicy


class ConsumptionService:
    """Serve one normalized output catalog to MCP and web adapters."""

    def __init__(
        self,
        registry: SQLiteAppRegistry,
        authorization_service: AuthorizationService,
        config: dict[str, Any],
    ) -> None:
        self.registry = registry
        self.authorization_service = authorization_service
        self.policy = ConsumptionPolicy.from_config(config)
        self.policy_version = self.policy.version

    def list_outputs(self, name: str, auth_context: AuthContext) -> dict[str, Any]:
        app, revision, decision = self._authorized_revision(name, auth_context)
        consumption = normalize_consumption_contract(
            revision.manifest.get("consumption"),
            data_sources=revision.manifest.get("data_sources"),
        )
        contract_hash = consumption_contract_hash(consumption)
        stored_hash = revision.manifest.get("consumption_contract_hash")
        if isinstance(stored_hash, str) and stored_hash and stored_hash != contract_hash:
            raise DashServerError(
                category="consumption_contract_hash_mismatch",
                summary=f"Stored consumption contract hash does not match app {name} revision content.",
                details={
                    "app": name,
                    "revision_number": revision.revision_number,
                    "stored_hash": stored_hash,
                    "computed_hash": contract_hash,
                },
                jsonrpc_code=-32012,
                http_status=500,
            )
        outputs = [
            self._output_payload(
                output,
                contract_hash=contract_hash,
            )
            for output in (consumption or {}).get("outputs", [])
        ]
        return {
            "app": {"name": app.name, "title": app.title, "route": app.route},
            "revision": {
                "revision_number": revision.revision_number,
                "commit_sha": revision.commit_sha,
                "git_tag": revision.git_tag,
            },
            "contract_hash": contract_hash,
            "outputs": outputs,
            "output_count": len(outputs),
            "policy": {**self.policy.to_dict(), "version": self.policy_version},
            "authorization": decision.to_dict(),
        }

    def get_output(
        self,
        name: str,
        output_id: str,
        auth_context: AuthContext,
    ) -> dict[str, Any]:
        payload = self.list_outputs(name, auth_context)
        for output in payload["outputs"]:
            if output["id"] == output_id:
                return {**payload, "output": output}
        raise DashServerError(
            category="consumption_output_not_found",
            summary=f"Consumption output {output_id} was not found for app {name}.",
            details={"app": name, "output_id": output_id},
            jsonrpc_code=-32004,
            http_status=404,
        )

    def execution_context(
        self,
        name: str,
        output_id: str,
        auth_context: AuthContext,
    ) -> ConsumptionExecutionContext:
        payload = self.get_output(name, output_id, auth_context)
        return ConsumptionExecutionContext(
            principal_id=auth_context.principal.principal_id,
            principal_type=auth_context.principal.principal_type,
            groups=auth_context.principal.groups,
            app_name=name,
            revision_number=int(payload["revision"]["revision_number"]),
            output_contract_hash=str(payload["contract_hash"]),
            policy_version=self.policy_version,
        )

    def _authorized_revision(self, name: str, auth_context: AuthContext):
        app = self.registry.get_app(name)
        if app is None:
            raise DashServerError(
                category="app_not_found",
                summary=f"App {name} was not found.",
                details={"app": name},
                jsonrpc_code=-32004,
                http_status=404,
            )
        decision = self.authorization_service.authorize_app(
            auth_context,
            app,
            "dashboard.export",
        )
        if not decision.allowed:
            raise DashServerError(
                category="consumption_authorization_denied",
                summary=f"Principal cannot discover consumption outputs for app {name}.",
                details=decision.to_dict(),
                jsonrpc_code=-32030,
                http_status=decision.status_code,
            )
        if (
            not auth_context.principal.is_authenticated
            and not self.policy.public_exports_enabled
        ):
            raise DashServerError(
                category="consumption_authorization_denied",
                summary="Public consumption output discovery is disabled by server policy.",
                details={
                    **decision.to_dict(),
                    "reason": "public_exports_disabled",
                },
                jsonrpc_code=-32030,
                http_status=403,
            )
        revision = self.registry.get_current_revision(name)
        if revision is None:
            raise DashServerError(
                category="app_revision_not_found",
                summary=f"App {name} has no current revision.",
                details={"app": name},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return app, revision, decision

    def _output_payload(
        self,
        output: dict[str, Any],
        *,
        contract_hash: str,
    ) -> dict[str, Any]:
        declared_formats = list(output["formats"])
        effective_formats = [
            item for item in declared_formats if item in self.policy.allowed_formats
        ] if self.policy.enabled else []
        declared_limits = output.get("limits", {})
        effective_limits = {
            "max_rows": min(int(declared_limits.get("max_rows", self.policy.max_rows)), self.policy.max_rows),
            "max_bytes": min(int(declared_limits.get("max_bytes", self.policy.max_bytes)), self.policy.max_bytes),
        }
        return {
            **output,
            "contract_hash": contract_hash,
            "policy": {
                "enabled": self.policy.enabled and bool(effective_formats),
                "effective_formats": effective_formats,
                "blocked_formats": sorted(set(declared_formats) - set(effective_formats)),
                "effective_limits": effective_limits,
                "phase": "discovery_only",
                "executable": False,
                "reason": "phase_0_discovery_only",
            },
        }
