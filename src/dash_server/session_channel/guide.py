"""Agent-facing reference for the session channel, served as an MCP resource.

With one wire verb (`app_session_eval_js`) instead of a fixed command enum,
``tools/list`` can no longer tell an agent what it is able to ask for. This guide
carries that weight instead, so it is treated as part of the feature rather than as
documentation: the `ctx` reference below is the API surface, and the recipes are the
things a command enum would have offered as named commands.
"""

from __future__ import annotations

from typing import Any

from .contract import (
    SENTINEL_ITEMS,
    SENTINEL_LENGTH,
    SENTINEL_OMITTED_CHARS,
    SENTINEL_OMITTED_ITEMS,
    SENTINEL_OMITTED_KEYS,
    SENTINEL_TRUNCATED,
    SENTINEL_TYPE,
    TIER_DASH_COMPONENT_API,
    TIER_DOM,
    TIER_REACT_FIBER,
)

_CTX_REFERENCE: tuple[dict[str, str], ...] = (
    {
        "name": "ctx.props(ids?)",
        "returns": "{tier, partial, values: {'id.prop': value}, missing: [id]}",
        "notes": (
            "Component props. Pass an array of ids; omitting it reads every id on the page "
            "up to a cap. Always check `tier` and `partial` before trusting completeness."
        ),
    },
    {
        "name": "ctx.dom(ids?)",
        "returns": "{nodes: {id: {tag, id, classes, text_prefix, rect, visible, in_viewport, child_count}}, missing, viewport}",
        "notes": "What is actually rendered and visible. The right tool for 'is anything there'.",
    },
    {
        "name": "ctx.plots()",
        "returns": "[{id, trace_count, trace_types, points_per_trace, layout, selection, selected_points}]",
        "notes": (
            "Plotly figure state read off the graph div, including zoom/pan ranges and box "
            "or lasso selections that never reach the server."
        ),
    },
    {
        "name": "ctx.stores()",
        "returns": "{tier, partial, stores: {id: {storage_type, data}}, local_storage_keys, session_storage_keys}",
        "notes": (
            "dcc.Store contents. A Store renders nothing, so its data is only reachable on "
            f"the {TIER_DASH_COMPONENT_API} tier; elsewhere `partial` is true and you get "
            "storage keys only."
        ),
    },
    {
        "name": "ctx.page()",
        "returns": "{pathname, search, hash, title, mount_path, revision_number, viewport, navigation_timing}",
        "notes": "Page-level context, including where in a multi-page app the user is.",
    },
    {
        "name": "ctx.setProps(id, props)",
        "returns": "{applied: [prop], id}",
        "notes": (
            "Sets a real prop through dash_clientside.set_props, so Dash reacts exactly as "
            "it would for the user. This changes what the user is looking at — prefer a "
            "preview mount when experimenting."
        ),
    },
    {
        "name": "ctx.waitForIdle(ms?)",
        "returns": "{fired: ['output.prop'], idle_after_ms, timed_out, inflight}",
        "notes": (
            "Await callback quiescence after setProps. Default 3000ms. Pair the two to see "
            "the consequence of an interaction in one call."
        ),
    },
    {
        "name": "ctx.summarize(value, {depth}?)",
        "returns": "bounded, JSON-safe projection of any value",
        "notes": "The same serializer applied to your return value, callable explicitly.",
    },
    {"name": "ctx.byId(id)", "returns": "DOM element or null", "notes": "Escape hatch for raw DOM work."},
    {
        "name": "ctx.dash",
        "returns": "window.dash_clientside",
        "notes": "Unwrapped renderer API for anything the helpers do not cover.",
    },
    {"name": "ctx.out", "returns": "object", "notes": "Fill it to return structured data alongside the value."},
    {
        "name": "ctx.session",
        "returns": "{session_id, mount_path, revision_number}",
        "notes": "Which tab the code is running in.",
    },
)

_RECIPES: tuple[dict[str, str], ...] = (
    {
        "goal": "What has the user selected right now?",
        "code": "ctx.props(['region-filter', 'date-range', 'metric-toggle'])",
    },
    {
        "goal": "Is the chart empty, or just scrolled out of view?",
        "code": "({plots: ctx.plots(), chart: ctx.dom(['revenue-chart']).nodes['revenue-chart']})",
    },
    {
        "goal": "Everything at once, for an opening read of an unfamiliar page.",
        "code": "({page: ctx.page(), props: ctx.props(), plots: ctx.plots()})",
    },
    {
        "goal": "What is in the store that drives the table?",
        "code": "ctx.stores().stores['filter-state']",
    },
    {
        "goal": "What happens if the region changes to APAC?",
        "code": (
            "await ctx.setProps('region-filter', {value: ['APAC']});\n"
            "const fired = await ctx.waitForIdle(3000);\n"
            "({fired, plots: ctx.plots()})"
        ),
    },
    {
        "goal": "Which ids does this page actually have?",
        "code": "Object.keys(ctx.props().values).map(k => k.split('.')[0]).filter((v, i, a) => a.indexOf(v) === i)",
    },
    {
        "goal": "Read one deep value without dumping the whole prop set.",
        "code": "ctx.summarize(ctx.props(['detail-table']).values['detail-table.data'], {depth: 2})",
    },
    {
        "goal": "Confirm the tab is the one the user is looking at.",
        "code": "({session: ctx.session, page: ctx.page(), hidden: document.hidden})",
    },
)


def session_channel_guide(channel_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the ``dash://meta/session-channel-guide`` payload."""

    return {
        "resource": "dash://meta/session-channel-guide",
        "summary": (
            "Run ephemeral JavaScript inside a live dashboard tab with app_session_eval_js. "
            "This is the only way to observe Dash interaction state, which lives in the "
            "browser rather than on the server."
        ),
        "availability": (
            "Local mode only. The channel is disabled in hosted mode and when the control "
            "plane is not bound to loopback; app_session_eval_js then returns "
            "session_channel_unavailable with the reason."
        ),
        "channel": channel_status or {},
        "how_it_works": [
            "Every hosted page carries a poll loop injected by the hosted chrome.",
            "The control plane queues one command per session; the page picks it up on its "
            "next poll, evaluates it, and posts a bounded result back.",
            "Polling accelerates while a command is outstanding and decays back afterwards, "
            "so latency is roughly one poll interval rather than a fixed 2s.",
            "One command in flight per session: a second concurrent dispatch is refused "
            "rather than queued behind the first.",
        ],
        "eval_semantics": {
            "modes": {
                "expression": "The whole body is one expression — `ctx.props(['a'])`.",
                "last_line": "Multi-statement body whose final line is a standalone expression.",
                "statements": "Plain body; use an explicit `return` to produce a value.",
            },
            "notes": [
                "The mode used is reported as `eval_mode`, so you can tell why a value did "
                "or did not come back.",
                "`await` is allowed anywhere; the body runs as an async function.",
                "A fresh scope per command — nothing you define survives into the next call. "
                "Use ctx.out or return a value.",
                "console.log/warn/error during the command are captured on `console`.",
                "A thrown error comes back as ok=false with name, message, stack, and `line` "
                "relative to the code you submitted.",
                "Mode detection is newline-based, not statement-based: "
                "\"console.log('a'); console.warn('b'); 42\" as one physical line is "
                "`statements` mode and returns undefined with no error, while the identical "
                "logic split across newlines is `last_line` mode and returns 42. Always "
                "check `eval_mode` in the result, or just use an explicit `return` — it "
                "works in every mode and removes the ambiguity entirely.",
            ],
        },
        "ctx": list(_CTX_REFERENCE),
        "prop_tiers": {
            "order": [TIER_DASH_COMPONENT_API, TIER_REACT_FIBER, TIER_DOM],
            TIER_DASH_COMPONENT_API: "Supported renderer API. Full prop tree, including dcc.Store data.",
            TIER_REACT_FIBER: (
                "Unsupported React-internals traversal. Works in practice but is fragile "
                "across Dash versions."
            ),
            TIER_DOM: "Rendered DOM only — values of inputs and text content. Always `partial: true`.",
            "guidance": (
                "Every result states the tier it used. Treat `partial: true` or a lower tier "
                "than expected as a real limitation and say so, rather than presenting a "
                "partial prop set as complete."
            ),
        },
        "truncation": {
            "summary": (
                "Results are bounded. Every clip is explicit — nothing is silently shortened, "
                "because a clipped array that looks complete would produce confident wrong "
                "answers."
            ),
            "sentinels": {
                SENTINEL_TYPE: "Marks a tagged value: undefined, NaN, Infinity, circular, date, depth-limit, …",
                SENTINEL_TRUNCATED: "Present and true wherever a cap fired.",
                SENTINEL_LENGTH: "Original array length when items were dropped.",
                SENTINEL_ITEMS: "The items or characters that were kept.",
                SENTINEL_OMITTED_ITEMS: "Array items dropped.",
                SENTINEL_OMITTED_KEYS: "Object keys dropped.",
                SENTINEL_OMITTED_CHARS: "String characters dropped.",
            },
            "envelope_flag": "`truncated: true` at the top level if anything was clipped anywhere.",
        },
        "failure_modes": {
            "session_channel_unavailable": "Not local mode, disabled, or a non-loopback bind. Nothing to retry.",
            "session_channel_session_gone": (
                "No live tab, an unknown id, a stale tab, or an id belonging to another app. "
                "`live_sessions` in the details lists what is available. A stale tab's last "
                "known state is not reported — do not present it as current."
            ),
            "session_channel_busy": "That session already has a command in flight.",
            "session_channel_timeout": (
                "The tab did not answer in time. It may be closed, backgrounded, or still "
                "running your code — JavaScript cannot be cancelled from the server."
            ),
            "ok_false": "The page ran your code and it threw. Read `error.line` and retry.",
        },
        "recipes": list(_RECIPES),
        "related_tools": ["app_sessions_list", "app_session_eval_js", "app_tail_logs"],
        "audit": (
            "Every command is appended to the app's session.commands diagnostics channel; "
            "read it with app_tail_logs(channel='session.commands')."
        ),
    }


__all__ = ["session_channel_guide"]
