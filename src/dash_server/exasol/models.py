"""Domain models for Exasol dashboard profiles and secret references."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ExasolSecretRef:
    """Reference to non-Git secret material for an Exasol profile."""

    provider: str
    key: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ExasolProfile:
    """Git-tracked Exasol connection profile metadata."""

    name: str
    backend: str
    deployment_mode: str
    credential_mode: str
    user: str
    dsn: str
    description: str
    tls_verify: bool
    secret_ref: ExasolSecretRef
    query_defaults: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["secret_ref"] = self.secret_ref.to_dict()
        return payload
