"""Consumption web-form parsing and shared-template regression tests.

Covers the two Wave 3 template/form changes:

* parameter *types* are resolved server-side from the output contract, so a
  tampered or absent client ``type__<name>`` field can no longer influence how a
  submitted value is coerced; and
* the three consumption pages share one base template holding the common shell
  CSS while keeping their page-specific rules.
"""

from __future__ import annotations

from types import SimpleNamespace

from werkzeug.datastructures import MultiDict

from dash_server.consumption.blueprint import _form_parameters


def test_form_parameters_coerce_by_server_declared_types() -> None:
    form = MultiDict(
        [
            ("param__count", "5"),
            ("param__ratio", "1.5"),
            ("param__flag", "true"),
            ("param__label", "hello"),
            ("format", "csv"),  # non-param field is ignored
        ]
    )
    types = {
        "count": "integer",
        "ratio": "number",
        "flag": "boolean",
        "label": "string",
    }

    parsed = _form_parameters(form, types)

    assert parsed == {"count": 5, "ratio": 1.5, "flag": True, "label": "hello"}


def test_tampered_type_field_cannot_change_coercion() -> None:
    """A client-supplied ``type__*`` field must have no effect on coercion."""

    server_types = {"count": "integer"}

    honest = _form_parameters(MultiDict([("param__count", "42")]), server_types)
    tampered = _form_parameters(
        MultiDict([("param__count", "42"), ("type__count", "string")]),
        server_types,
    )
    lying = _form_parameters(
        MultiDict([("param__count", "42"), ("type__count", "boolean")]),
        server_types,
    )

    # The declared server-side type wins; the value is coerced to int regardless
    # of what the client claims (or omits).
    assert honest == tampered == lying == {"count": 42}


def test_undeclared_parameter_is_left_uncoerced() -> None:
    # A parameter with no server-side declared type is passed through as a raw
    # string; the service's schema validation rejects it downstream.
    parsed = _form_parameters(MultiDict([("param__stray", "7")]), {})

    assert parsed == {"stray": "7"}


def test_blank_and_bad_values_match_prior_behavior() -> None:
    form = MultiDict(
        [
            ("param__count", ""),  # blank -> skipped entirely
            ("param__ratio", "not-a-number"),  # bad -> falls back to raw string
        ]
    )
    types = {"count": "integer", "ratio": "number"}

    parsed = _form_parameters(form, types)

    assert parsed == {"ratio": "not-a-number"}


def test_admin_jobs_page_extends_shared_base(app) -> None:
    from flask import render_template

    admin = SimpleNamespace(
        app="finance-outputs",
        job_count=0,
        jobs=[],
        coordinator=SimpleNamespace(mode="local-single-process", pid=123),
    )
    with app.app_context():
        html = render_template("consumption_admin_jobs.html", admin=admin)

    # Exactly one style block, and the shared shell rules come from the base.
    assert html.count("<style>") == 1
    assert "--main-width: 960px" in html
    assert "min(var(--main-width), calc(100vw - 2rem))" in html
    assert 'font-family: "Avenir Next", "Segoe UI", sans-serif' in html
    # Page-specific rule still rendered from the child.
    assert "border-collapse: collapse" in html
    assert "finance-outputs export jobs" in html


def test_export_page_renders_head_and_width_blocks(app) -> None:
    from flask import render_template

    export = SimpleNamespace(
        job=SimpleNamespace(
            id="job-123",
            status="running",
            app_name="finance-outputs",
            output_id="monthly-close-detail",
            revision_number=1,
            requested_format="csv",
            progress=SimpleNamespace(rows=0, bytes=0),
            error=None,
        )
    )
    with app.app_context():
        html = render_template(
            "consumption_export.html",
            export=export,
            cancel_token="tok",
            download=None,
        )

    # head_extra block emits the auto-refresh meta for in-flight jobs.
    assert 'http-equiv="refresh"' in html
    # main_width block sets this page's column width via the shared variable.
    assert "--main-width: 760px" in html
    assert "min(var(--main-width), calc(100vw - 2rem))" in html
    # Child-specific crimson cancel button survives the extraction.
    assert "background: #9f1239" in html


def test_outputs_form_omits_client_type_hint(app) -> None:
    """The export form no longer emits client-controlled type__ hidden fields."""

    from flask import render_template

    catalog = SimpleNamespace(
        app=SimpleNamespace(name="finance-outputs", title="Finance", route="/apps/finance-outputs"),
        revision=SimpleNamespace(revision_number=1),
        contract_hash="abcdef0123456789",
        output_count=1,
        policy=SimpleNamespace(exports_enabled=True),
        outputs=[
            {
                "id": "monthly-close-detail",
                "title": "Monthly close detail",
                "description": "",
                "kind": "dataset",
                "classification": "confidential",
                "formats": ["csv"],
                "parameters": {
                    "type": "object",
                    "properties": {"period": {"type": "string"}},
                    "required": ["period"],
                },
                "policy": {
                    "effective_formats": ["csv"],
                    "effective_limits": {"max_rows": 5000, "max_bytes": 1000000},
                    "format_availability": {"csv": {"executable": True, "reason": "available"}},
                },
            }
        ],
    )
    with app.app_context():
        html = render_template(
            "consumption_outputs.html",
            catalog=catalog,
            exports={"jobs": [], "job_count": 0},
            csrf_token="tok",
            idempotency_key="idem",
            can_export=True,
            can_manage=False,
        )

    assert 'name="param__period"' in html
    assert "type__" not in html
