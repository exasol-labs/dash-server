"""Round-trip tests for the zero-dependency desired-state YAML subset.

Guards the acceptance criterion from the technical-debt consolidation plan
(Wave 2 item 7): desired-state files containing ``:``-bearing routes and
quoted scalars must round-trip through the hand-rolled reader/writer pair.
"""

from __future__ import annotations

import pytest

from dash_server.gitops.repo_service import (
    parse_yaml_mapping,
    render_yaml_mapping,
)


def _roundtrip(payload: dict) -> dict:
    return parse_yaml_mapping(render_yaml_mapping(payload))


# ---------------------------------------------------------------------------
# Round-trip corpus
# ---------------------------------------------------------------------------

ROUNDTRIP_CASES: list[dict] = [
    # Bare scalars and booleans.
    {"kind": "DashDeployment", "enabled": True, "disabled": False},
    # A representative live desired-state document (nested mappings).
    {
        "apiVersion": "dash-server/v1",
        "kind": "DashDeployment",
        "metadata": {"app": "demo"},
        "spec": {
            "targetRevision": "r000001",
            "commit": "",
            "gitTag": "dash-server/demo/r000001",
            "releaseManifestPath": "releases/demo/r000001.yaml",
            "route": "/apps/demo",
            "visibility": "private",
            "authPolicy": "inherited",
            "enabled": True,
            "permissions": {
                "filesystem": {"mode": "workspace-write"},
                "network": {"mode": "inherit"},
                "env": {"mode": "inherit"},
            },
            "sourcePath": "apps/demo",
        },
    },
    # Route / values bearing colons -- the headline fragility being fixed.
    {"spec": {"route": "/apps/team:beta", "note": "host:8080"}},
    {"spec": {"route": "/apps/foo:"}},  # trailing colon (broke the old reader)
    {"spec": {"route": ":leading-colon"}},
    # Empty scalar must survive as "" (distinct from an empty mapping).
    {"spec": {"commit": "", "gitTag": ""}},
    # Strings that look like reserved tokens must stay strings.
    {"spec": {"a": "true", "b": "false", "c": "null", "d": "~"}},
    # Embedded quotes.
    {"spec": {"quote": 'say "hi"', "path": 'C:\\a\\b'}},
    # Leading indicator characters.
    {"spec": {"anchor": "*ref", "comment": "# not a comment", "dash": "- item"}},
    # Whitespace-significant values.
    {"spec": {"padded": "  spaced  "}},
    # Timestamps / hashes with colons (as written by release manifests).
    {"spec": {"createdAt": "2026-07-22T12:34:56Z", "manifestHash": "sha256:abc123"}},
    # Deeper nesting.
    {"a": {"b": {"c": {"d": "deep"}}, "e": "sibling"}},
]


@pytest.mark.parametrize("payload", ROUNDTRIP_CASES)
def test_roundtrip(payload: dict) -> None:
    assert _roundtrip(payload) == payload


def test_double_roundtrip_is_stable() -> None:
    """Rendering the parsed result reproduces the original text byte-for-byte."""
    for payload in ROUNDTRIP_CASES:
        first = render_yaml_mapping(payload)
        second = render_yaml_mapping(parse_yaml_mapping(first))
        assert first == second


# ---------------------------------------------------------------------------
# Behavior specifics
# ---------------------------------------------------------------------------


def test_colon_value_is_quoted_on_write() -> None:
    text = render_yaml_mapping({"route": "/apps/foo:"})
    assert text == 'route: "/apps/foo:"\n'
    assert parse_yaml_mapping(text) == {"route": "/apps/foo:"}


def test_empty_scalar_and_empty_mapping_are_distinct() -> None:
    assert parse_yaml_mapping('commit: ""\n') == {"commit": ""}
    assert parse_yaml_mapping("commit:\n") == {"commit": {}}


def test_reserved_token_string_is_quoted() -> None:
    assert render_yaml_mapping({"flag": "true"}) == 'flag: "true"\n'
    assert parse_yaml_mapping('flag: "true"\n') == {"flag": "true"}
    # A genuine boolean stays bare and parses back to bool.
    assert render_yaml_mapping({"flag": True}) == "flag: true\n"
    assert parse_yaml_mapping("flag: true\n") == {"flag": True}


def test_unquoted_colon_values_still_parse() -> None:
    """Legacy manifests wrote colon-bearing scalars bare; they must still read."""
    text = "createdAt: 2026-07-22T12:34:56Z\nmanifestHash: sha256:abc123\n"
    assert parse_yaml_mapping(text) == {
        "createdAt": "2026-07-22T12:34:56Z",
        "manifestHash": "sha256:abc123",
    }


def test_comments_and_blank_lines_are_ignored() -> None:
    text = "# leading comment\n\nkind: DashDeployment\n\n# trailing\n"
    assert parse_yaml_mapping(text) == {"kind": "DashDeployment"}


def test_bootstrap_manifest_format_unchanged_for_colon_free_data() -> None:
    """Values without colons keep the plain, human-readable rendering."""
    text = render_yaml_mapping(
        {
            "apiVersion": "dash-server/v1",
            "spec": {"route": "/apps/demo", "gitTag": "dash-server/demo/r000001"},
        }
    )
    assert text == (
        "apiVersion: dash-server/v1\n"
        "spec:\n"
        "  route: /apps/demo\n"
        "  gitTag: dash-server/demo/r000001\n"
    )
