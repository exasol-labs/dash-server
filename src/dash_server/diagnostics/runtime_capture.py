"""Record the exceptions a hosted Dash app raises while serving, in-process or in an isolated worker.

A failing callback becomes a ``dash_callback_error`` with its traceback and the callback's inputs and
outputs, which ``app_inspect_traceback`` and ``dash://apps/{name}/callback-failures`` report; any other
exception becomes a runtime error of its traceback's category.
"""

from __future__ import annotations

import traceback
from typing import Any

from flask import Flask, got_request_exception, request


def callback_request_details(payload: Any) -> dict[str, Any]:
    """The parts of a ``_dash-update-component`` request that tell which callback failed."""

    if not isinstance(payload, dict):
        return {"output": None, "outputs": None, "changed_prop_ids": [], "inputs": [], "state": []}
    return {
        "output": payload.get("output"),
        "outputs": payload.get("outputs"),
        "changed_prop_ids": payload.get("changedPropIds") or [],
        "inputs": payload.get("inputs") or [],
        "state": payload.get("state") or [],
    }


def callback_failure_summary(details: dict[str, Any]) -> str:
    output = details.get("output")
    if isinstance(output, str) and output:
        return f"Dash callback failed for output {output}."
    changed = details.get("changed_prop_ids")
    if isinstance(changed, list) and changed:
        return f"Dash callback failed after input change {changed[0]}."
    return "Dash callback failed during callback dispatch."


def record_request_exceptions(
    flask_app: Flask, diagnostics_service: Any, *, app_name: str, revision_number: int
) -> None:
    """Record every exception ``flask_app`` raises while serving a request; once per app."""

    if getattr(flask_app, "_dash_server_diagnostics_instrumented", False):
        return

    def handle_runtime_exception(sender: Any, exception: BaseException, **extra: Any) -> None:
        traceback_text = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        path = request.path
        if path.endswith("/_dash-update-component"):
            details = {
                "path": path,
                "method": request.method,
                **callback_request_details(request.get_json(silent=True)),
            }
            diagnostics_service.record_callback_failure(
                app_name,
                summary=callback_failure_summary(details),
                details=details,
                traceback_text=traceback_text,
                revision_number=revision_number,
            )
            return
        category = diagnostics_service.inspect_traceback(traceback_text)["traceback"]["category"]
        diagnostics_service.record_error(
            app_name,
            source="runtime",
            category=category,
            summary=f"Unhandled runtime exception while serving {path}.",
            details={"path": path, "method": request.method},
            traceback_text=traceback_text,
            revision_number=revision_number,
        )

    got_request_exception.connect(handle_runtime_exception, flask_app, weak=False)
    # Flask doesn't declare arbitrary attributes; this is the documented sentinel pattern.
    flask_app._dash_server_diagnostics_instrumented = True  # type: ignore[attr-defined]
