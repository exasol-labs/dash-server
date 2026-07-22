"""Git-backed metadata storage for Exasol connection profiles."""

from __future__ import annotations

import json
import re
from typing import Any

from dash_server.exceptions import DashServerError
from dash_server.exasol.models import ExasolProfile, ExasolSecretRef
from dash_server.gitops import GitRepoService

_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ExasolProfileStore:
    """Persist non-secret Exasol profile metadata in the GitOps repository."""

    def __init__(self, git_repo_service: GitRepoService) -> None:
        self.git_repo_service = git_repo_service

    def list_profiles(self) -> list[ExasolProfile]:
        profiles_dir = self.git_repo_service.repo_root / "profiles" / "exasol"
        if not profiles_dir.exists():
            return []
        profiles: list[ExasolProfile] = []
        for path in sorted(profiles_dir.glob("*.json")):
            profiles.append(self._profile_from_payload(json.loads(path.read_text())))
        return profiles

    def get_profile(self, name: str) -> ExasolProfile:
        path = self._profile_path(name)
        if not path.exists():
            raise DashServerError(
                category="exasol_profile_not_found",
                summary=f"Exasol profile {name} was not found.",
                details={"profile": name},
            )
        return self._profile_from_payload(json.loads(path.read_text()))

    def save_profile(self, profile: ExasolProfile) -> ExasolProfile:
        self._validate_profile_name(profile.name)
        self.git_repo_service.commit_managed_update(
            managed_files={
                self.profile_path(profile.name): json.dumps(profile.to_dict(), indent=2)
                + "\n"
            },
            removed_paths=[],
            commit_message=f"exasol/{profile.name}: record local profile metadata",
        )
        return self.get_profile(profile.name)

    def profile_exists(self, name: str) -> bool:
        """Check whether a profile is already on disk without raising on absence."""

        return self._profile_path(name).exists()

    def profile_path(self, name: str) -> str:
        return f"profiles/exasol/{name}.json"

    def _profile_path(self, name: str):
        return self.git_repo_service.repo_root / self.profile_path(name)

    def _validate_profile_name(self, name: str) -> None:
        if not _PROFILE_NAME_RE.match(name):
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary="Exasol profile names must use lowercase letters, numbers, and hyphens.",
                details={"field": "name", "value": name},
            )

    def _profile_from_payload(self, payload: dict[str, Any]) -> ExasolProfile:
        secret_ref = payload.get("secret_ref")
        if not isinstance(secret_ref, dict):
            raise DashServerError(
                category="exasol_profile_validation_error",
                summary="Exasol profile secret_ref must be an object.",
                details={"field": "secret_ref"},
            )
        return ExasolProfile(
            name=str(payload.get("name")),
            backend=str(payload.get("backend")),
            deployment_mode=str(payload.get("deployment_mode")),
            credential_mode=str(payload.get("credential_mode")),
            user=str(payload.get("user")),
            dsn=str(payload.get("dsn")),
            description=str(payload.get("description")),
            tls_verify=bool(payload.get("tls_verify", True)),
            secret_ref=ExasolSecretRef(
                provider=str(secret_ref.get("provider")),
                key=str(secret_ref.get("key")),
            ),
            query_defaults=payload.get("query_defaults") if isinstance(payload.get("query_defaults"), dict) else None,
        )
