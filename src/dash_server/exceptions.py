"""Structured application errors for the MCP control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dash_server.errors import codes_for


@dataclass
class DashServerError(Exception):
    """Structured error returned through the MCP surface.

    ``jsonrpc_code`` and ``http_status`` default to the values registered for
    the category in :mod:`dash_server.errors`; pass them explicitly only when a
    site's semantics genuinely differ from the category default.
    """

    category: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    jsonrpc_code: int | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        default_code, default_status = codes_for(self.category)
        if self.jsonrpc_code is None:
            self.jsonrpc_code = default_code
        if self.http_status is None:
            self.http_status = default_status

    def __str__(self) -> str:
        """Render `category: summary` so tracebacks aren't blank.

        Persona 3's BUG-018 follow-up: when the smoke-check raised a
        ``DashServerError`` with an empty summary, the resulting traceback ended
        with just ``dash_server.exceptions.DashServerError`` and the operator
        had no idea what the failure was about. Returning a non-empty `str` even
        when `summary` is blank fixes that.
        """

        if self.summary:
            return f"{self.category}: {self.summary}"
        return self.category or "DashServerError"

    def to_error_object(self) -> dict[str, Any]:
        return {
            "code": self.jsonrpc_code,
            "message": self.summary,
            "data": {
                "category": self.category,
                **self.details,
            },
        }
