"""Drift guard for `docs/mcp-reference.md`.

The tool list and resource list in the reference doc are auto-generated from the
live MCP registry. This test re-runs the generator and asserts the committed file
matches. CI failure means a tool was added/renamed/redescribed; run
``python scripts/generate_mcp_reference.py`` and commit the change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE_DOC = _PROJECT_ROOT / "docs" / "mcp-reference.md"


@pytest.fixture(autouse=True)
def _add_scripts_to_path() -> None:
    """Let us import the standalone script as a module."""

    scripts_dir = str(_PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def test_mcp_reference_doc_is_in_sync_with_registry() -> None:
    """The committed `docs/mcp-reference.md` matches what the generator produces.

    Reruns the generator's section-formatting against the in-process MCP server
    and compares to the marker regions in the committed doc. If this fails, run:

        python scripts/generate_mcp_reference.py

    then commit the updated doc.
    """

    from generate_mcp_reference import (  # type: ignore[import-not-found]
        _RESOURCES_BEGIN,
        _RESOURCES_END,
        _TOOLS_BEGIN,
        _TOOLS_END,
        build_reference_sections,
    )

    committed = _REFERENCE_DOC.read_text(encoding="utf-8")
    fresh_tools, fresh_resources = build_reference_sections()

    committed_tools = _extract_between(committed, _TOOLS_BEGIN, _TOOLS_END)
    committed_resources = _extract_between(committed, _RESOURCES_BEGIN, _RESOURCES_END)

    assert committed_tools == fresh_tools, (
        "docs/mcp-reference.md tool list is stale. Run "
        "`python scripts/generate_mcp_reference.py` and commit."
    )
    assert committed_resources == fresh_resources, (
        "docs/mcp-reference.md resource list is stale. Run "
        "`python scripts/generate_mcp_reference.py` and commit."
    )


def _extract_between(source: str, begin: str, end: str) -> str:
    """Return the substring strictly between `begin` and `end`, with the bracketing
    newlines stripped to match the generator's output shape."""

    start_idx = source.find(begin)
    end_idx = source.find(end)
    assert start_idx >= 0 and end_idx >= 0, f"markers {begin!r}/{end!r} missing"
    body = source[start_idx + len(begin) : end_idx]
    return body.lstrip("\n")
