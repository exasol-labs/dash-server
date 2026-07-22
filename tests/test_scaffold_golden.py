"""Golden byte-identity net for the Exasol scaffold generator.

This is the safety net for the P8 template-extraction refactor (Wave 2 item 8): it
pins the exact bytes of every scaffold variant's generated output. Run it before and
after the refactor; the generated files must be byte-identical.

To (re)generate the fixtures on purpose, run:

    DASH_SCAFFOLD_UPDATE_GOLDEN=1 python -m pytest tests/test_scaffold_golden.py -q

Regeneration is only ever done deliberately against known-good code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dash_server.exasol import scaffold

GOLDEN_ROOT = Path(__file__).parent / "golden" / "scaffold"

# Blueprints exercising the branches in the schema-tailored SQL builders:
#  - full: time column + measures + derived (qty x price) revenue + dimension
#  - rel:  no local time column, join into a related table for the trend axis
#  - min:  no measure/time/dimension (placeholder fallbacks)
_SCHEMA_BLUEPRINTS = {
    "schema_full": {
        "schema_name": "SALES",
        "table_name": "ORDERS",
        "business_caption": "Sales orders analytics tailored from the SALES.ORDERS table.",
        "summary_heading": "Orders KPI Snapshot",
        "chart_heading": "Orders Trend",
        "table_heading": "Orders Detail",
        "time_column": "ORDER_DATE",
        "dimension_column": "REGION",
        "primary_measure": "NET_AMOUNT",
        "measure_columns": ["QUANTITY", "NET_UNIT_PRICE", "NET_AMOUNT"],
        "relationship_hints": [],
    },
    "schema_rel": {
        "schema_name": "MART",
        "table_name": "ORDER_LINES",
        "business_caption": "Order-line analytics joined to ORDERS for the date axis.",
        "summary_heading": "Line KPI Snapshot",
        "chart_heading": "Line Trend",
        "table_heading": "Line Detail",
        "time_column": None,
        "dimension_column": "ORDER_ID",
        "primary_measure": "QUANTITY",
        "measure_columns": ["QUANTITY", "NET_UNIT_PRICE", "UNIT_COST"],
        "relationship_hints": [
            {
                "column_name": "ORDER_ID",
                "other_schema": "MART",
                "other_table": "ORDERS",
                "other_time_column": "ORDER_DATE",
                "other_key_column": "ORDER_ID",
            }
        ],
    },
    "schema_min": {
        "schema_name": "RAW",
        "table_name": "EVENTS",
        "business_caption": "Minimal scaffold; no measures/time/dimension discovered.",
        "summary_heading": "Events KPI Snapshot",
        "chart_heading": "Events Trend",
        "table_heading": "Events Detail",
        "time_column": None,
        "dimension_column": None,
        "primary_measure": None,
        "measure_columns": [],
        "relationship_hints": [],
    },
}


def _generate_outputs() -> dict[str, str]:
    """Return a flat {relative_golden_path: content} map of every scaffold output."""

    outputs: dict[str, str] = {}

    # 1. Files-based dashboard bundles, one per built-in pattern.
    for pattern in scaffold.EXASOL_DASHBOARD_PATTERNS:
        bundle = scaffold.build_exasol_dashboard_bundle(
            app_name=f"golden-{pattern}",
            title=f"Golden {pattern.title()}",
            route=f"/golden/{pattern}",
            description=f"Golden fixture for the {pattern} pattern.",
            profile_name="golden-profile",
            pattern=pattern,
        )
        for entry in bundle["files"]:
            outputs[f"dashboard__{pattern}/{entry['path']}"] = entry["content"]

    # 2. Schema-tailored bundles across the branch-covering blueprints.
    for key, blueprint in _SCHEMA_BLUEPRINTS.items():
        bundle = scaffold.build_schema_scaffold_bundle(
            app_name=f"golden-{key}",
            title=f"Golden {key}",
            route=f"/golden/{key}",
            description=f"Golden fixture for {key}.",
            profile_name="golden-profile",
            blueprint=blueprint,
        )
        for entry in bundle["files"]:
            outputs[f"{key}/{entry['path']}"] = entry["content"]

    # 3. The shipped runtime helper text (also embedded in every bundle above).
    outputs["helper/dash_server_exasol.py"] = scaffold.render_exasol_helper_py()

    # 4. The static guidance dicts, serialized deterministically.
    help_payloads = {
        "help/connection_modes.json": scaffold.exasol_connection_modes_help(),
        "help/sql_placeholders.json": scaffold.exasol_sql_placeholders_help(),
        "help/dashboard_patterns.json": scaffold.exasol_dashboard_patterns_help(),
        "help/agent_workflow.json": scaffold.exasol_agent_workflow_help(),
    }
    for path, payload in help_payloads.items():
        outputs[path] = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    return outputs


def _write_goldens() -> None:
    outputs = _generate_outputs()
    for rel_path, content in outputs.items():
        target = GOLDEN_ROOT / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


if os.environ.get("DASH_SCAFFOLD_UPDATE_GOLDEN"):
    _write_goldens()


_OUTPUTS = _generate_outputs()


@pytest.mark.parametrize("rel_path", sorted(_OUTPUTS))
def test_scaffold_output_is_byte_identical_to_golden(rel_path: str) -> None:
    golden_path = GOLDEN_ROOT / rel_path
    assert golden_path.exists(), (
        f"Missing golden fixture {rel_path}. Regenerate with "
        f"DASH_SCAFFOLD_UPDATE_GOLDEN=1 python -m pytest tests/test_scaffold_golden.py"
    )
    expected = golden_path.read_text(encoding="utf-8")
    assert _OUTPUTS[rel_path] == expected, f"Scaffold output drifted for {rel_path}"


def test_golden_set_has_no_orphans() -> None:
    """Every golden fixture on disk must correspond to a produced output."""

    on_disk = {
        str(path.relative_to(GOLDEN_ROOT))
        for path in GOLDEN_ROOT.rglob("*")
        if path.is_file()
    }
    produced = set(_OUTPUTS)
    orphans = on_disk - produced
    assert not orphans, f"Golden fixtures with no matching output: {sorted(orphans)}"


# --- Runtime helper module unit tests (P8 acceptance: "the shipped helper module
# --- passes its own unit tests"). These import the extracted helper module directly.


def _import_helper_module():
    return pytest.importorskip("dash_server.exasol.scaffold_helper")


def test_helper_has_error_detects_error_envelope() -> None:
    helper = _import_helper_module()
    assert helper.has_error([{"_error": "boom"}]) is True
    assert helper.has_error([{"value": 1}]) is False
    assert helper.has_error([]) is False


def test_helper_render_error_panel_includes_message() -> None:
    helper = _import_helper_module()
    panel = helper.render_error_panel("connection refused")
    text = str(panel)
    assert "connection refused" in text


def test_helper_render_table_handles_empty_and_error_and_rows() -> None:
    helper = _import_helper_module()
    assert "No rows returned." in str(helper.render_table([]))
    assert "boom" in str(helper.render_table([{"_error": "boom"}]))
    table = helper.render_table([{"A": 1, "B": 2}])
    rendered = str(table)
    assert "Table" in rendered


def test_helper_module_text_matches_scaffold_output() -> None:
    """The importable helper module's source is what scaffold ships verbatim."""

    helper = _import_helper_module()
    import inspect

    module_path = Path(inspect.getfile(helper))
    assert module_path.read_text(encoding="utf-8") == scaffold.render_exasol_helper_py()
