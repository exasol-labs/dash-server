"""Parameter protection and signed purpose-bound consumption tokens."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from dash_server.exceptions import DashServerError


class ParameterCodec:
    """Encrypt normalized job parameters at rest with a stable local key."""

    def __init__(self, secret: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    @classmethod
    def from_config(cls, config: dict[str, Any], artifacts_root: str | Path) -> ParameterCodec:
        configured = config.get("DASH_SERVER_CONSUMPTION_PARAMETER_KEY") or config.get("SECRET_KEY")
        if isinstance(configured, str) and configured:
            return cls(configured)
        key_path = Path(artifacts_root) / "consumption" / ".parameter-key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            secret = key_path.read_text(encoding="utf-8").strip()
        else:
            secret = secrets.token_urlsafe(48)
            key_path.write_text(secret, encoding="utf-8")
            key_path.chmod(0o600)
        return cls(secret)

    def encode(self, parameters: dict[str, Any]) -> str:
        raw = json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decode(self, value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(self._fernet.decrypt(value.encode("ascii")))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise DashServerError(
                category="consumption_parameter_decode_error",
                summary="Stored export parameters could not be decoded.",
                details={},
                jsonrpc_code=-32012,
                http_status=500,
            ) from exc
        if not isinstance(decoded, dict):
            raise DashServerError(
                category="consumption_parameter_decode_error",
                summary="Stored export parameters have an invalid shape.",
                details={},
                jsonrpc_code=-32012,
                http_status=500,
            )
        return decoded


class ConsumptionTokenSigner:
    """Sign CSRF and download tokens with explicit purposes."""

    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="dash-server-consumption-v1")

    def issue(self, purpose: str, payload: dict[str, Any]) -> str:
        return self._serializer.dumps({"purpose": purpose, **payload})

    def verify(self, token: str, purpose: str, *, max_age: int) -> dict[str, Any]:
        try:
            payload = self._serializer.loads(token, max_age=max_age)
        except SignatureExpired as exc:
            raise DashServerError(
                category="consumption_token_expired",
                summary="The consumption token has expired.",
                details={"purpose": purpose},
                jsonrpc_code=-32031,
                http_status=410,
            ) from exc
        except BadSignature as exc:
            raise DashServerError(
                category="consumption_token_invalid",
                summary="The consumption token is invalid.",
                details={"purpose": purpose},
                jsonrpc_code=-32030,
                http_status=403,
            ) from exc
        if not isinstance(payload, dict) or payload.get("purpose") != purpose:
            raise DashServerError(
                category="consumption_token_invalid",
                summary="The consumption token has the wrong purpose.",
                details={"purpose": purpose},
                jsonrpc_code=-32030,
                http_status=403,
            )
        return payload


def parameter_hash(parameters: dict[str, Any]) -> str:
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    return {key: "<provided>" for key in parameters}


__all__ = [
    "ConsumptionTokenSigner",
    "ParameterCodec",
    "parameter_hash",
    "redact_parameters",
]
