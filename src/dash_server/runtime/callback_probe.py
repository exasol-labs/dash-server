"""Plan and classify a real Dash callback run for the ``callback_execution`` probe.

Every other probe in :class:`~dash_server.runtime.prober.HealthProber` is a plain
GET or a direct SQL run, so none of them ever executes an ``@app.callback``. A
dashboard whose callbacks all raise therefore used to report ``healthy``: the
shell renders, the layout and dependencies endpoints answer, the SQL files parse,
and the page is still blank in the browser.

This module closes that gap by reconstructing what a browser does on first
render. Dash's frontend reads two endpoints on page load - ``_dash-dependencies``
(the callback graph) and ``_dash-layout`` (the initial property values) - and
then POSTs one ``_dash-update-component`` request per callback it is allowed to
fire. :func:`plan_callbacks` rebuilds exactly those request bodies from those two
payloads, and :func:`classify_response` reads the answers back.

Reading the answer back takes two checks, not one. A callback that *raises* is easy:
Dash answers non-2xx or puts an ``error`` key in the envelope. The harder case is the
one this server creates itself - the injected ``dash_server_exasol.py`` helper never
raises on a data-layer failure, it returns an ``[{"_error": ...}]`` envelope, and the
generated app renders that as an error panel. The callback then returns a completely
ordinary HTTP 200 while the page shows the user nothing but an error. So
:func:`classify_response` also inspects the *rendered output* for that envelope and
for the panel's exact heading, and fails the callback when it finds one.

The scope limit is deliberate: we reproduce the *initial render* burst and
nothing else. A callback Dash itself would not fire on page load
(``prevent_initial_call``), one it would run in the browser
(``clientside_function``), one served over a different transport (``websocket``,
``background``), or one whose inputs do not exist in the initial layout (the
pattern-matching ids another callback renders later) is reported as **skipped
with a reason** instead of being handed an invented input value. Guessing values
is how a probe turns flaky; naming what it did not cover is how it stays honest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_TAG_RE = re.compile(r"<[^>]*>")

# The probe walks a serialized layout tree of unknown depth and runs against a
# live app, so both are bounded. These are ceilings, not budgets: a normal
# dashboard fires a handful of callbacks over a layout of a few hundred nodes.
MAX_LAYOUT_NODES = 20_000
MAX_CALLBACKS = 24
TIME_BUDGET_SECONDS = 20.0
MAX_REPORTED_SKIPS = 20
MAX_REPORTED_FAILURES = 10
MAX_ERROR_TEXT = 600

# The heading `dash_server_exasol.render_error_panel` puts on its panel. The shipped
# helper never raises on a data-layer failure - `load_rows` and friends return an
# `[{"_error": ...}]` envelope, and the generated app renders that envelope as this
# panel. So the *page is broken* while the callback returns a perfectly ordinary
# HTTP 200. Matching the exact heading (never a substring like "error") is what lets
# the probe see that without firing on a dashboard that merely charts an error rate.
ERROR_PANEL_HEADING = "Exasol query failed"

# Dash serializes a dict component id as a JSON object string with sorted keys and
# no spaces - `{"index":["ALL"],"type":"row"}`. A *wildcard* id is one whose values
# hold one of these sentinels; a concrete dict id is an ordinary addressable
# component and the probe treats it like any string id.
_WILDCARD_SENTINELS = frozenset({"ALL", "MATCH", "ALLSMALLER"})


@dataclass(frozen=True)
class CallbackPlan:
    """One ``_dash-update-component`` request body, ready to POST."""

    output: str
    body: dict[str, Any]


@dataclass(frozen=True)
class SkippedCallback:
    """A callback the probe deliberately did not run, and why."""

    output: str
    reason: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"output": self.output, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class CallbackOutcome:
    """What came back from one executed callback."""

    output: str
    status_code: int
    ok: bool
    prevented: bool = False
    error: str | None = None
    empty_figures: list[str] = field(default_factory=list)
    error_outputs: list[str] = field(default_factory=list)

    def as_failure_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "output": self.output,
            "status_code": self.status_code,
            "error": self.error,
        }
        if self.error_outputs:
            payload["error_rendering_outputs"] = self.error_outputs
        return payload


def index_layout_props(layout: Any) -> dict[str, dict[str, Any]]:
    """Map every addressable component id in a serialized layout to its props dict.

    Dash serializes a layout as nested ``{"props": {...}, "type": ..., "namespace":
    ...}`` nodes, with children reachable through ``props.children`` as a dict, a
    list, or a scalar. The walk is iterative (a deep layout must not blow the
    recursion limit) and bounded by :data:`MAX_LAYOUT_NODES`.

    Dict ids are indexed under the same canonical JSON string Dash uses in
    ``_dash-dependencies``, so a callback wired to a concrete dict id resolves here
    instead of being skipped as absent.
    """

    index: dict[str, dict[str, Any]] = {}
    stack: list[Any] = [layout]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_LAYOUT_NODES:
            break
        if isinstance(node, dict):
            props = node.get("props")
            if isinstance(props, dict):
                key = stringify_id(props.get("id"))
                if key is not None and key not in index:
                    index[key] = props
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return index


def split_output_targets(output: str) -> tuple[list[dict[str, Any]], bool] | None:
    """Invert Dash's callback-id encoding into the request's ``outputs`` value.

    Dash encodes a single output as ``"<id>.<property>"`` and a grouped output as
    ``"..<id>.<prop>...<id>.<prop>.."``, escaping any literal dot inside an id as
    ``\\.``. Returns ``(targets, is_grouped)`` - the grouped flag matters because
    Dash's renderer sends a bare object for a single output and a *list* for a
    grouped one, even when the group holds a single target. Returns ``None`` for a
    shape we cannot parse, so the caller can skip that callback rather than POST a
    body Dash would reject.

    A dict id is handed back as a dict, not as its canonical string: Dash validates
    the request's outputs against the callback definition with ``Output.__eq__``,
    which compares dict ids structurally, so the string form is rejected.
    """

    if not output:
        return None
    grouped = output.startswith("..") and output.endswith("..") and len(output) > 4
    parts = output[2:-2].split("...") if grouped else [output]
    targets: list[dict[str, Any]] = []
    for part in parts:
        component_id, separator, prop = part.rpartition(".")
        if not separator or not component_id or not prop:
            return None
        targets.append(
            {"id": parse_component_id(component_id.replace("\\.", ".")), "property": prop}
        )
    return targets, grouped


def parse_component_id(component_id: str) -> Any:
    """Inverse of :func:`stringify_id`: a JSON-object id becomes a dict again."""

    if not component_id.startswith("{"):
        return component_id
    try:
        parsed = json.loads(component_id)
    except ValueError:
        return component_id
    return parsed if isinstance(parsed, dict) else component_id


def plan_callbacks(
    dependencies: Any,
    layout_props: dict[str, dict[str, Any]],
    *,
    max_callbacks: int = MAX_CALLBACKS,
) -> tuple[list[CallbackPlan], list[SkippedCallback]]:
    """Turn a ``_dash-dependencies`` payload into POST bodies plus skip reasons."""

    plans: list[CallbackPlan] = []
    skipped: list[SkippedCallback] = []
    if not isinstance(dependencies, list):
        return plans, skipped

    for spec in dependencies:
        if not isinstance(spec, dict):
            continue
        output = spec.get("output")
        if not isinstance(output, str) or not output:
            continue

        skip = _skip_reason(spec)
        if skip is not None:
            skipped.append(SkippedCallback(output=output, reason=skip[0], detail=skip[1]))
            continue

        outputs_value: Any = []
        if not spec.get("no_output"):
            parsed = split_output_targets(output)
            if parsed is None:
                skipped.append(SkippedCallback(output=output, reason="unparsable_output_id"))
                continue
            targets, grouped = parsed
            outputs_value = targets if grouped else targets[0]

        inputs, missing_input = _bind_values(spec.get("inputs"), layout_props)
        if missing_input is not None:
            skipped.append(
                SkippedCallback(
                    output=output,
                    reason="input_not_in_initial_layout",
                    detail=missing_input,
                )
            )
            continue
        state, missing_state = _bind_values(spec.get("state"), layout_props)
        if missing_state is not None:
            skipped.append(
                SkippedCallback(
                    output=output,
                    reason="state_not_in_initial_layout",
                    detail=missing_state,
                )
            )
            continue

        if len(plans) >= max_callbacks:
            skipped.append(SkippedCallback(output=output, reason="callback_limit_reached"))
            continue

        plans.append(
            CallbackPlan(
                output=output,
                body={
                    # `outputs` mirrors Dash's own renderer: a bare object for a
                    # single output, a list for a grouped (multi-)output callback.
                    "output": output,
                    "outputs": outputs_value,
                    "inputs": inputs,
                    "state": state,
                    # Empty == Dash's initial call. `callback_context.triggered` is
                    # empty on page load, so this is the faithful first-render
                    # request - and it is the one shape that can never mismatch the
                    # inputs list.
                    "changedPropIds": [],
                },
            )
        )
    return plans, skipped


def classify_response(
    output: str,
    status_code: int,
    payload: Any,
    *,
    body_text: str = "",
) -> CallbackOutcome:
    """Read one ``_dash-update-component`` answer as pass / prevented / failure."""

    if status_code == 204:
        # PreventUpdate. The callback ran; it simply declined to update anything.
        return CallbackOutcome(output=output, status_code=status_code, ok=True, prevented=True)
    if status_code < 200 or status_code >= 300:
        return CallbackOutcome(
            output=output,
            status_code=status_code,
            ok=False,
            error=_error_text(status_code, payload, body_text),
        )
    if not isinstance(payload, dict):
        return CallbackOutcome(
            output=output,
            status_code=status_code,
            ok=False,
            error="Callback returned a non-JSON body where Dash always returns JSON.",
        )
    if payload.get("error") is not None:
        return CallbackOutcome(
            output=output,
            status_code=status_code,
            ok=False,
            error=_truncate(_stringify(payload.get("error"))),
        )
    # A 2xx with an error envelope in the rendered output is the failure mode the
    # other probes cannot see: HTTP is fine, Dash is fine, and the page shows the
    # user an error panel. Treat it exactly like a raised callback.
    response = payload.get("response")
    error_outputs, error_message = _rendered_error_outputs(response)
    if error_outputs:
        return CallbackOutcome(
            output=output,
            status_code=status_code,
            ok=False,
            error=_truncate(
                f"Callback succeeded but rendered a data-layer error into "
                f"{', '.join(error_outputs)}: {error_message or 'no message in the envelope'}"
            ),
            error_outputs=error_outputs,
        )
    return CallbackOutcome(
        output=output,
        status_code=status_code,
        ok=True,
        empty_figures=_empty_figure_outputs(response),
    )


def summarize(
    outcomes: list[CallbackOutcome],
    skipped: list[SkippedCallback],
) -> dict[str, Any]:
    """Build the probe's ``details`` payload from the run's outcomes."""

    failures = [outcome for outcome in outcomes if not outcome.ok]
    empty_figures: list[str] = []
    for outcome in outcomes:
        empty_figures.extend(outcome.empty_figures)
    details: dict[str, Any] = {
        "callbacks_executed": len(outcomes),
        "callbacks_failed": len(failures),
        "callbacks_prevented_update": sum(1 for outcome in outcomes if outcome.prevented),
        "callbacks_skipped": len(skipped),
    }
    if skipped:
        details["skipped"] = [item.as_dict() for item in skipped[:MAX_REPORTED_SKIPS]]
    if failures:
        details["first_failed_callback"] = failures[0].output
        details["failed_callbacks"] = [
            failure.as_failure_dict() for failure in failures[:MAX_REPORTED_FAILURES]
        ]
        details["latest_error"] = failures[0].error
    error_outputs: list[str] = []
    for outcome in failures:
        error_outputs.extend(outcome.error_outputs)
    if error_outputs:
        details["error_rendering_outputs"] = sorted(set(error_outputs))
    if empty_figures:
        # Advisory, never a failure: a figure with no traces is usually a broken
        # query and sometimes a genuinely empty reporting period. Report it so an
        # agent can look, but do not turn a quiet Monday into a red healthcheck.
        details["empty_figure_outputs"] = sorted(set(empty_figures))
    return details


# --------------------------------------------------------------------- helpers


def _skip_reason(spec: dict[str, Any]) -> tuple[str, str | None] | None:
    """Return ``(reason, detail)`` when this callback must not be POSTed."""

    if spec.get("clientside_function"):
        return ("clientside_callback", "Runs in the browser, not on the server.")
    if spec.get("prevent_initial_call") is True:
        return ("prevent_initial_call", "Dash does not fire this callback on page load either.")
    if spec.get("background"):
        return ("background_callback", "Needs the long-callback polling protocol.")
    if spec.get("websocket"):
        return ("websocket_callback", "Not served over _dash-update-component.")
    if is_wildcard_id(spec.get("output")):
        return (
            "pattern_matching_id",
            "Wildcard ids resolve only against components another callback renders.",
        )
    for kind in ("inputs", "state"):
        for dependency in spec.get(kind) or []:
            if not isinstance(dependency, dict) or not isinstance(dependency.get("id"), str):
                return (
                    "unresolvable_dependency_id",
                    "The dependency carries no usable component id.",
                )
            if is_wildcard_id(dependency["id"]):
                return (
                    "pattern_matching_id",
                    "Wildcard ids resolve only against components another callback renders.",
                )
    return None


def is_wildcard_id(component_id: Any) -> bool:
    """True when a stringified dict id contains an ALL/MATCH/ALLSMALLER sentinel."""

    if not isinstance(component_id, str) or "{" not in component_id:
        return False
    start = component_id.find("{")
    end = component_id.rfind("}")
    if end <= start:
        return False
    try:
        parsed = json.loads(component_id[start : end + 1])
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    return any(
        isinstance(value, list) and any(item in _WILDCARD_SENTINELS for item in value)
        for value in parsed.values()
    )


def stringify_id(component_id: Any) -> str | None:
    """Dash's canonical id form: a string as-is, a dict as compact sorted JSON."""

    if isinstance(component_id, str):
        return component_id
    if isinstance(component_id, dict):
        try:
            return json.dumps(component_id, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
    return None


def _bind_values(
    dependencies: Any,
    layout_props: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Attach each dependency's current value from the initial layout.

    A property that is absent from the component's serialized props binds to
    ``None`` - that is exactly what Dash's renderer sends for an unset prop such
    as a button's ``n_clicks``. Only an *id* the initial layout does not contain
    is a reason to skip the callback.
    """

    bound: list[dict[str, Any]] = []
    if not isinstance(dependencies, list):
        return bound, None
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        component_id = dependency.get("id")
        prop = dependency.get("property")
        if not isinstance(component_id, str) or not isinstance(prop, str):
            return bound, f"{component_id!r}.{prop!r}"
        props = layout_props.get(component_id)
        if props is None:
            return bound, f"{component_id}.{prop}"
        bound.append(
            {
                "id": parse_component_id(component_id),
                "property": prop,
                "value": props.get(prop),
            }
        )
    return bound, None


def _rendered_error_outputs(response: Any) -> tuple[list[str], str | None]:
    """Find outputs whose rendered value carries a data-layer error.

    Two shapes count, and both come from the helper this server injects into every
    Exasol app:

    * a raw ``{"_error": "..."}`` envelope that reached the output unrendered, and
    * a ``render_error_panel`` component tree, identified by its exact heading.

    Returns the qualified output names plus the first error message found, so the
    probe can say which output broke *and* why.
    """

    if not isinstance(response, dict):
        return [], None
    hits: list[str] = []
    message: str | None = None
    for component_id, props in response.items():
        if not isinstance(props, dict):
            continue
        for prop, value in props.items():
            found = _find_error_message(value)
            if found is None:
                continue
            hits.append(f"{component_id}.{prop}")
            if message is None and found:
                message = found
    return hits, message


def _find_error_message(value: Any) -> str | None:
    """Walk one rendered property value; return the error text, ``""``, or ``None``.

    ``None`` means "no error here". A returned string (possibly empty, when the
    marker is present but carries no message) means this value is an error.
    """

    stack: list[Any] = [value]
    visited = 0
    found_marker = False
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_LAYOUT_NODES:
            break
        if isinstance(node, dict):
            envelope = node.get("_error")
            if envelope is not None:
                return _truncate(_stringify(envelope))
            children = _panel_message(node)
            if children is not None:
                # Prefer the panel's own message, but remember the marker even when
                # the panel body is empty so the caller still reports a failure.
                if children:
                    return children
                found_marker = True
            for item in node.values():
                if isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return "" if found_marker else None


def _panel_message(node: dict[str, Any]) -> str | None:
    """If ``node`` is a ``render_error_panel`` container, return its message text.

    The panel is ``html.Div([html.Strong(HEADING), html.Pre(message)])``, so we look
    for a children list holding the exact heading and read the text out of its
    siblings. Returns ``None`` when this node is not an error panel.
    """

    props = node.get("props")
    if not isinstance(props, dict):
        return None
    children = props.get("children")
    if not isinstance(children, list):
        return None
    if not any(_is_heading(child) for child in children):
        return None
    parts = [
        text
        for child in children
        if not _is_heading(child)
        for text in (_plain_text(child),)
        if text
    ]
    return _truncate(" ".join(parts))


def _is_heading(child: Any) -> bool:
    return _plain_text(child) == ERROR_PANEL_HEADING


def _plain_text(node: Any) -> str:
    """Flatten a small serialized component subtree to its text content."""

    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict):
            return _plain_text(props.get("children"))
        return ""
    if isinstance(node, list):
        return " ".join(filter(None, (_plain_text(item) for item in node))).strip()
    return ""


def _empty_figure_outputs(response: Any) -> list[str]:
    """Names of ``figure`` outputs that came back without a single trace."""

    if not isinstance(response, dict):
        return []
    empty: list[str] = []
    for component_id, props in response.items():
        if not isinstance(props, dict):
            continue
        for prop, value in props.items():
            if prop != "figure":
                continue
            if isinstance(value, dict) and not value.get("data"):
                empty.append(f"{component_id}.{prop}")
    return empty


def _error_text(status_code: int, payload: Any, body_text: str) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if value:
                return _truncate(_stringify(value))
    snippet = _readable_body(body_text)
    if snippet:
        return f"HTTP {status_code}: {snippet}"
    return f"HTTP {status_code} from _dash-update-component."


def _readable_body(body_text: str) -> str:
    """A one-line, tag-free summary of a response body.

    Dash serves a callback traceback as plain text when dev tools are on and a
    generic HTML error page when they are not; both are worth reporting, neither
    is worth reporting as raw markup.
    """

    text = body_text.strip()
    if not text:
        return ""
    if text.startswith("<"):
        text = _TAG_RE.sub(" ", text)
    return _truncate(" ".join(text.split()))


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _truncate(text: str) -> str:
    if len(text) <= MAX_ERROR_TEXT:
        return text
    return text[:MAX_ERROR_TEXT] + "..."


__all__ = [
    "ERROR_PANEL_HEADING",
    "MAX_CALLBACKS",
    "MAX_LAYOUT_NODES",
    "TIME_BUDGET_SECONDS",
    "CallbackOutcome",
    "CallbackPlan",
    "SkippedCallback",
    "classify_response",
    "index_layout_props",
    "is_wildcard_id",
    "parse_component_id",
    "plan_callbacks",
    "split_output_targets",
    "stringify_id",
    "summarize",
]
