"""Local secret storage and resolution for Exasol profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dash_server.exceptions import DashServerError
from dash_server.exasol.models import ExasolSecretRef


class ExasolSecretStore:
    """Resolve and persist non-Git secret values for local Exasol use."""

    def __init__(self, secrets_root: str) -> None:
        self.secrets_root = Path(secrets_root)
        self.secrets_root.mkdir(parents=True, exist_ok=True)

    def store_local_secret(self, profile_name: str, secret_value: str) -> ExasolSecretRef:
        path = self._secret_path(profile_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"secret": secret_value}))
        return ExasolSecretRef(provider="local_file", key=profile_name)

    def env_secret_ref(self, env_var: str) -> ExasolSecretRef:
        return ExasolSecretRef(provider="env", key=env_var)

    def resolve(self, secret_ref: ExasolSecretRef) -> str:
        if secret_ref.provider == "env":
            value = os.environ.get(secret_ref.key)
            if isinstance(value, str) and value:
                return value
            raise DashServerError(
                category="exasol_secret_error",
                summary=f"Environment variable {secret_ref.key} is not set.",
                details={"provider": "env", "key": secret_ref.key},
                jsonrpc_code=-32011,
                http_status=404,
            )
        if secret_ref.provider == "local_file":
            payload_path = self._secret_path(secret_ref.key)
            if not payload_path.exists():
                raise DashServerError(
                    category="exasol_secret_error",
                    summary=f"Local secret for profile {secret_ref.key} was not found.",
                    details={"provider": "local_file", "key": secret_ref.key},
                    jsonrpc_code=-32011,
                    http_status=404,
                )
            payload = json.loads(payload_path.read_text())
            value = payload.get("secret")
            if isinstance(value, str) and value:
                return value
            raise DashServerError(
                category="exasol_secret_error",
                summary=f"Local secret for profile {secret_ref.key} is invalid.",
                details={"provider": "local_file", "key": secret_ref.key},
                jsonrpc_code=-32011,
                http_status=400,
            )
        raise DashServerError(
            category="exasol_secret_error",
            summary=f"Unsupported secret provider {secret_ref.provider}.",
            details={"provider": secret_ref.provider},
            jsonrpc_code=-32011,
            http_status=400,
        )

    def _secret_path(self, profile_name: str) -> Path:
        return self.secrets_root / f"{profile_name}.json"
