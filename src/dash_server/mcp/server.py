"""MCP server for the hosted Dash control plane.

``MCPServer`` composes the concern mixins that used to be one 4.6k-line module:
JSON-RPC dispatch (:mod:`~dash_server.mcp.dispatch`), resource routing
(:mod:`~dash_server.mcp.resources`), schema builders (:mod:`~dash_server.mcp.schemas`),
agent guidance (:mod:`~dash_server.mcp.guidance`), and the tool handlers
(:mod:`~dash_server.mcp.handlers`). The tool/guidance declarations live on the
``ToolSpec`` table (:mod:`~dash_server.mcp.tool_specs`), from which ``tools/list``
is derived below. The public constructor and attributes are unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
from typing import Any

from dash_server.consumption import ConsumptionService
from dash_server.exasol import ExasolDashboardService
from dash_server.gitops import GitRepoService
from dash_server.mailer import InvitationEmailSender
from dash_server.mcp.dispatch import DispatchMixin
from dash_server.mcp.guidance import GuidanceMixin
from dash_server.mcp.handlers import HandlersMixin
from dash_server.mcp.resources import ResourcesMixin
from dash_server.mcp.schemas import SchemasMixin
from dash_server.mcp.tool_specs import TOOL_SPECS
from dash_server.runtime.service import AppRuntimeService
from dash_server.session_channel import SessionChannelService


class MCPServer(DispatchMixin, ResourcesMixin, SchemasMixin, GuidanceMixin, HandlersMixin):
    """MCP implementation for hosted Dash control-plane tools and resources."""

    protocol_version = "2025-06-18"
    # Phase 3.5d added "worker" + "worker.events" for isolated-mode workers;
    # "session.commands" is the browser session channel's audit trail.
    _log_channels = (
        "latest",
        "build",
        "runtime",
        "health",
        "worker",
        "worker.events",
        "session.commands",
    )

    def __init__(
        self,
        runtime_service: AppRuntimeService,
        git_repo_service: GitRepoService,
        exasol_dashboard_service: ExasolDashboardService | None = None,
        email_sender: InvitationEmailSender | None = None,
        consumption_service: ConsumptionService | None = None,
        session_channel_service: SessionChannelService | None = None,
    ) -> None:
        self.runtime_service = runtime_service
        self.git_repo_service = git_repo_service
        self.exasol_dashboard_service = exasol_dashboard_service
        self.email_sender = email_sender
        self.consumption_service = consumption_service
        self.session_channel_service = session_channel_service
        # Handler dict is derived from the single ToolSpec table so a tool cannot
        # exist in one structure but not the others.
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            spec.name: getattr(self, spec.handler) for spec in TOOL_SPECS
        }

    def _tool_definitions(self) -> list[dict[str, Any]]:
        """Derive the ``tools/list`` payload from the ToolSpec table (P2.2).

        ``ToolSpec.input_schema`` is either an inline schema dict or the name of a
        builder method (``mcp/schemas.py``); everything else is copied verbatim from
        the spec's folded ``title``/``description``/``meta``.
        """

        definitions: list[dict[str, Any]] = []
        for spec in TOOL_SPECS:
            schema = spec.input_schema
            if isinstance(schema, str):
                schema = getattr(self, schema)()
            elif isinstance(schema, dict):
                schema = copy.deepcopy(schema)
            definition: dict[str, Any] = {
                "name": spec.name,
                "title": spec.title,
                "description": spec.description,
                "inputSchema": schema,
            }
            if spec.meta is not None:
                definition["_meta"] = copy.deepcopy(spec.meta)
            definitions.append(definition)
        return definitions
