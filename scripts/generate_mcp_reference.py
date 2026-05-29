"""Regenerate the tool/resource sections of `docs/mcp-reference.md` from the live MCP registry.

Invocation: ``python scripts/generate_mcp_reference.py``.

The script boots `create_app` against a throwaway instance, asks the in-process
``MCPServer`` for its `tools/list` and `resources/list`, formats them as markdown,
and writes the result between the `<!-- BEGIN: auto-* -->` / `<!-- END: auto-* -->`
markers in `docs/mcp-reference.md`. The surrounding curated prose is untouched.

`tests/test_mcp_reference_doc.py` runs the same generation against the committed
file and fails the build on any drift, so adding a new tool or renaming an
existing one is a one-line operator action (`python scripts/generate_mcp_reference.py`
and commit the change).
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE_DOC = _PROJECT_ROOT / "docs" / "mcp-reference.md"

_TOOLS_BEGIN = "<!-- BEGIN: auto-tools -->"
_TOOLS_END = "<!-- END: auto-tools -->"
_RESOURCES_BEGIN = "<!-- BEGIN: auto-resources -->"
_RESOURCES_END = "<!-- END: auto-resources -->"


def build_reference_sections() -> tuple[str, str]:
    """Return (tools_markdown, resources_markdown) for the current MCP surface."""

    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
    from dash_server.app_factory import create_app

    with tempfile.TemporaryDirectory() as scratch:
        app = create_app({"INSTANCE_PATH": scratch, "TESTING": True})
        server = app.extensions["mcp_server"]
        tools = server._tool_definitions()
        resources = server._resource_definitions()

    return _format_tools(tools), _format_resources(resources)


def _format_tools(tools: list[dict[str, Any]]) -> str:
    """Markdown for the tools section: a bulleted list with one-line descriptions."""

    lines = [
        f"_{len(tools)} tools registered. Each `tools/call` request must pass the tool name and the arguments defined by its `inputSchema`._",
        "",
    ]
    for tool in sorted(tools, key=lambda t: t["name"]):
        name = tool["name"]
        description = tool.get("description", "").splitlines()[0].strip() or "(no description)"
        lines.append(f"- **`{name}`** — {description}")
    return "\n".join(lines) + "\n"


def _format_resources(resources: list[dict[str, Any]]) -> str:
    """Markdown for the resources section.

    Per-app URIs (`dash://apps/{app}/...`) are collapsed to a single template entry
    so the doc isn't dominated by the demo app's resources.
    """

    seen_templates: set[str] = set()
    server_wide: list[dict[str, Any]] = []
    per_app: list[dict[str, Any]] = []

    for res in resources:
        uri = res["uri"]
        templated = _template_uri(uri)
        if templated.startswith("dash://apps/{app}"):
            if templated in seen_templates:
                continue
            seen_templates.add(templated)
            per_app.append({**res, "uri": templated})
        else:
            server_wide.append(res)

    lines = [
        f"_{len(server_wide)} server-wide resources plus the per-app pattern below "
        "({{app}} matches any registered app name)._",
        "",
        "### Server-wide",
        "",
    ]
    for res in sorted(server_wide, key=lambda r: r["uri"]):
        desc = (res.get("description") or "").splitlines()[0].strip() or "(no description)"
        lines.append(f"- **`{res['uri']}`** — {desc}")
    lines += [
        "",
        "### Per-app (`dash://apps/{app}/…`)",
        "",
    ]
    for res in sorted(per_app, key=lambda r: r["uri"]):
        desc = (res.get("description") or "").splitlines()[0].strip() or "(no description)"
        lines.append(f"- **`{res['uri']}`** — {desc}")
    return "\n".join(lines) + "\n"


_APP_URI_RE = re.compile(r"^dash://apps/[a-z0-9][a-z0-9-]*(/.*)?$")


def _template_uri(uri: str) -> str:
    """Collapse `dash://apps/<concrete>/...` → `dash://apps/{app}/...`.

    `dash://apps` itself stays unchanged. `dash://apps/{app}` (no trailing path)
    becomes the bare per-app overview entry.
    """

    if uri == "dash://apps":
        return uri
    match = _APP_URI_RE.match(uri)
    if not match:
        return uri
    suffix = match.group(1) or ""
    return f"dash://apps/{{app}}{suffix}"


def render_to_doc(doc_path: Path = _REFERENCE_DOC) -> str:
    """Rewrite the marker sections of ``doc_path`` and return the new contents."""

    if not doc_path.exists():
        raise SystemExit(f"reference doc not found at {doc_path}")
    tools_md, resources_md = build_reference_sections()
    current = doc_path.read_text(encoding="utf-8")
    updated = _replace_between(current, _TOOLS_BEGIN, _TOOLS_END, tools_md)
    updated = _replace_between(updated, _RESOURCES_BEGIN, _RESOURCES_END, resources_md)
    doc_path.write_text(updated, encoding="utf-8")
    return updated


def _replace_between(source: str, begin: str, end: str, payload: str) -> str:
    """Replace whatever lives between `begin` and `end` in `source` with `payload`."""

    if begin not in source or end not in source:
        raise SystemExit(
            f"missing markers {begin!r}/{end!r} in target doc — add them so the "
            "generator can find the section to update."
        )
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    replacement = f"{begin}\n{payload}{end}"
    return pattern.sub(replacement, source)


if __name__ == "__main__":
    render_to_doc()
    print(f"updated {_REFERENCE_DOC.relative_to(_PROJECT_ROOT)}")
