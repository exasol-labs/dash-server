"""Type-checking view of ``MCPServer`` shared by its concern mixins.

The mixins in this package call each other's helpers and read the services that
``MCPServer.__init__`` sets, but none of them defines those on its own. Each mixin
inherits ``MCPServerHost`` so mypy sees that shared surface. At runtime it is plain
``object``: a declaration here can never shadow (or stand in for) a real method.
Keep these signatures in sync with their implementations; mypy checks every
mixin override against them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dash_server.auth import AuthContext
    from dash_server.consumption import ConsumptionService
    from dash_server.exasol import ExasolDashboardService
    from dash_server.exceptions import DashServerError
    from dash_server.gitops import GitRepoService
    from dash_server.mailer import InvitationEmailSender
    from dash_server.runtime.service import AppRuntimeService
    from dash_server.session_channel import SessionChannelService

    class MCPServerHost:
        # MCPServer class attributes and constructor state.
        protocol_version: str
        _log_channels: tuple[str, ...]
        runtime_service: AppRuntimeService
        git_repo_service: GitRepoService
        exasol_dashboard_service: ExasolDashboardService | None
        email_sender: InvitationEmailSender | None
        consumption_service: ConsumptionService | None
        session_channel_service: SessionChannelService | None
        _tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]

        # MCPServer
        def _tool_definitions(self) -> list[dict[str, Any]]: ...

        # DispatchMixin
        def _absolute_url(self, path: str) -> str: ...
        def _attach_absolute_urls(self, payload: Any) -> Any: ...
        def _render_visible_tool_text(self, summary_text: str, structured_content: dict[str, Any]) -> str: ...
        def _validation_summary_lines(self, validation: dict[str, Any]) -> list[str]: ...
        def _diagnostic_lines_for_error(self, exc: DashServerError) -> list[str]: ...

        # ResourcesMixin
        def _resource_definitions(self) -> list[dict[str, Any]]: ...
        def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]: ...
        def _workers_payload(self) -> dict[str, Any]: ...

        # GuidanceMixin
        def _append_guidance_to_text(self, text: str, guidance: dict[str, Any] | None) -> str: ...
        def _attach_guidance(
            self,
            tool_name: str,
            payload: dict[str, Any],
            *,
            is_error: bool,
            exc: DashServerError | None = None,
        ) -> dict[str, Any]: ...

        # HandlersMixin
        def _require_name(self, arguments: dict[str, Any]) -> str: ...
        def _require_existing_app(self, name: str, *, tool_name: str) -> Any: ...
        def _worker_manager_or_error(self) -> Any: ...
        def _dep_env_service_or_error(self) -> Any: ...
        def _consumption_service(self) -> ConsumptionService: ...
        @staticmethod
        def _consumption_auth_context() -> AuthContext: ...

else:
    MCPServerHost = object
