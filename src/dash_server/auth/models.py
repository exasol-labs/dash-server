"""Small identity model used before full hosted authentication is implemented."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    """Request principal known to the control plane."""

    principal_id: str
    principal_type: str
    display_name: str
    email: str | None
    roles: tuple[str, ...]
    groups: tuple[str, ...] = ()
    is_authenticated: bool = False
    issuer: str | None = None
    subject: str | None = None
    email_verified: bool = False
    tenant_id: str | None = None

    @classmethod
    def local_admin(cls) -> Principal:
        return cls(
            principal_id="local-admin",
            principal_type="local_admin",
            display_name="Local Admin",
            email=None,
            roles=("admin", "owner", "editor", "viewer"),
            is_authenticated=True,
            issuer="dash-server:local",
            subject="local-admin",
            email_verified=False,
        )

    @classmethod
    def anonymous(cls) -> Principal:
        return cls(
            principal_id="anonymous",
            principal_type="anonymous",
            display_name="Anonymous",
            email=None,
            roles=(),
            is_authenticated=False,
            issuer=None,
            subject=None,
            email_verified=False,
        )

    @classmethod
    def authenticated_user(
        cls,
        *,
        issuer: str,
        subject: str,
        email: str | None,
        display_name: str | None = None,
        groups: tuple[str, ...] = (),
        roles: tuple[str, ...] = ("viewer",),
        email_verified: bool = False,
        tenant_id: str | None = None,
    ) -> Principal:
        return cls(
            principal_id=f"{issuer}:{subject}",
            principal_type="user",
            display_name=display_name or email or subject,
            email=email,
            roles=roles,
            groups=groups,
            is_authenticated=True,
            issuer=issuer,
            subject=subject,
            email_verified=email_verified,
            tenant_id=tenant_id,
        )

    @classmethod
    def link_access(
        cls,
        *,
        link_id: int,
        app_name: str,
        role: str,
        scope: str,
        email: str | None = None,
    ) -> Principal:
        principal_id = f"share_link:{link_id}"
        return cls(
            principal_id=principal_id,
            principal_type="link",
            display_name=f"Shared link for {app_name}",
            email=email,
            roles=(role,),
            groups=(),
            is_authenticated=True,
            issuer="dash-server:share-link",
            subject=str(link_id),
            email_verified=email is not None,
            tenant_id=None,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Principal:
        return cls(
            principal_id=str(payload.get("principal_id") or "anonymous"),
            principal_type=str(payload.get("principal_type") or "anonymous"),
            display_name=str(payload.get("display_name") or "Anonymous"),
            email=payload.get("email") if isinstance(payload.get("email"), str) else None,
            roles=tuple(str(item) for item in payload.get("roles", ()) if isinstance(item, str)),
            groups=tuple(str(item) for item in payload.get("groups", ()) if isinstance(item, str)),
            is_authenticated=bool(payload.get("is_authenticated", False)),
            issuer=payload.get("issuer") if isinstance(payload.get("issuer"), str) else None,
            subject=payload.get("subject") if isinstance(payload.get("subject"), str) else None,
            email_verified=bool(payload.get("email_verified", False)),
            tenant_id=payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AuthContext:
    """Current authentication context for the server process."""

    mode: str
    auth_enabled: bool
    principal: Principal
    provider: str = "disabled"

    @classmethod
    def for_mode(cls, mode: str, *, auth_enabled: bool, provider: str = "disabled") -> AuthContext:
        if mode == "local":
            return cls(
                mode=mode,
                auth_enabled=auth_enabled,
                provider=provider,
                principal=Principal.local_admin(),
            )
        return cls(
            mode=mode,
            auth_enabled=auth_enabled,
            provider=provider,
            principal=Principal.anonymous(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "auth_enabled": self.auth_enabled,
            "provider": self.provider,
            "principal": self.principal.to_dict(),
        }
