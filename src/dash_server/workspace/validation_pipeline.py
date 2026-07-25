"""Ordered stage pipeline behind ``WorkspaceService.validate_workspace``.

Each validation concern is a small :class:`Check` stage that produces a
:class:`CheckResult`. Stages run in a fixed order and read one another's
results through the shared :class:`ValidationContext`; ``is_valid`` and the
assembled payload derive from the collected results rather than from a
hand-synchronized status ladder plus a separate ``is_valid`` conjunction plus a
result dict.

Adding a validation check is therefore a matter of writing one ``Check`` class,
registering it in :data:`PIPELINE_STAGES`, and listing its key in
:data:`PAYLOAD_KEY_ORDER` — the ``is_valid`` computation picks it up
automatically.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dash_server.consumption import validate_consumption_sources
from dash_server.registry.models import AppManifest

from dash_server.workspace.credential_scan import credential_safety_report
from dash_server.workspace.cross_module import (
    cross_module_symbol_report,
    default_cross_module_symbol_report,
)
from dash_server.workspace.exasol_lint import exasol_validation_report

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dash_server.workspace.service import WorkspaceService


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single validation stage.

    ``key`` is the payload key the report is filed under. ``payload`` is the
    report dict verbatim. ``ok`` records whether the stage passed for the
    purpose of the overall ``is_valid`` verdict. ``blocks_downstream`` marks a
    result that later stages treat as a hard block on the syntax -> cross-module
    -> dependency -> import ladder.
    """

    key: str
    payload: dict[str, Any]
    ok: bool
    blocks_downstream: bool = False


@dataclass
class ValidationContext:
    """Shared inputs and accumulated results threaded through the stages."""

    service: WorkspaceService
    app_name: str
    mount_path: str | None
    force_clean: bool
    files: dict[str, str]
    manifest: AppManifest
    requirements: dict[str, Any]
    python_files: dict[str, str]
    parsed_trees: dict[str, ast.AST]
    syntax_errors: list[dict[str, Any]]
    results: dict[str, CheckResult] = field(default_factory=dict)

    def payload(self, key: str) -> dict[str, Any]:
        """The report dict produced by an already-run stage."""

        return self.results[key].payload


class Check:
    """Base class for a validation stage."""

    key: str

    def run(self, ctx: ValidationContext) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError


class SyntaxCheck(Check):
    key = "syntax"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload = {
            "status": "passed" if not ctx.syntax_errors else "failed",
            "errors": ctx.syntax_errors,
        }
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=not ctx.syntax_errors,
            blocks_downstream=bool(ctx.syntax_errors),
        )


class LintCheck(Check):
    key = "lint"

    def run(self, ctx: ValidationContext) -> CheckResult:
        warnings: list[dict[str, Any]] = []
        for relative_path, tree in ctx.parsed_trees.items():
            warnings.extend(ctx.service._lint_tree(tree, relative_path))
        payload = {
            "status": "passed" if not warnings else "passed_with_warnings",
            "warnings": warnings,
        }
        # Lint never fails validation and never blocks downstream stages.
        return CheckResult(key=self.key, payload=payload, ok=True)


class RequirementsCheck(Check):
    key = "requirements"

    def run(self, ctx: ValidationContext) -> CheckResult:
        return CheckResult(
            key=self.key,
            payload=ctx.requirements,
            ok=not ctx.requirements["invalid"],
        )


class CrossModuleSymbolCheck(Check):
    key = "cross_module_symbols"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload = default_cross_module_symbol_report(syntax_errors=ctx.syntax_errors)
        if not ctx.syntax_errors:
            payload = cross_module_symbol_report(ctx.parsed_trees)
        failed = payload["status"] == "failed"
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=not failed,
            blocks_downstream=failed,
        )


class DependencyInstallCheck(Check):
    key = "dependency_install"

    def run(self, ctx: ValidationContext) -> CheckResult:
        entries = ctx.requirements["entries"]
        invalid = ctx.requirements["invalid"]
        payload = ctx.service._default_dependency_install_status(
            syntax_errors=ctx.syntax_errors,
            invalid_requirements=invalid,
            requirements=entries,
        )
        if ctx.payload("cross_module_symbols")["status"] == "failed":
            payload = {
                "status": "skipped",
                "requirements": entries,
                "notes": "Skipped dependency install because local cross-module symbol validation failed.",
            }
        elif not ctx.syntax_errors and not invalid:
            payload = ctx.service._ensure_dependencies(
                ctx.app_name,
                entries,
                force_clean=ctx.force_clean,
            )
        failed = payload["status"] == "failed"
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=not failed,
            blocks_downstream=failed,
        )


class ImportSmokeCheck(Check):
    key = "imports"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload: dict[str, Any] = {
            "status": "skipped",
            "error": None,
            "traceback": None,
        }
        if not ctx.syntax_errors:
            if ctx.payload("cross_module_symbols")["status"] == "failed":
                payload = {
                    "status": "skipped",
                    "category": "cross_module_symbols_failed",
                    "error": "Skipped import smoke check because local cross-module symbol validation failed.",
                    "traceback": None,
                }
            elif ctx.payload("dependency_install")["status"] == "failed":
                payload = {
                    "status": "skipped",
                    "category": "environment_missing_dependency",
                    "error": "Dependency install failed before import smoke check.",
                    "traceback": None,
                }
            else:
                payload = ctx.service._import_smoke_check(
                    ctx.app_name,
                    ctx.manifest,
                    declared_packages=ctx.requirements["packages"],
                    dependency_install=ctx.payload("dependency_install"),
                    mount_path=ctx.mount_path,
                )
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=payload.get("status") == "passed",
        )


class CallbacksCheck(Check):
    key = "callbacks"

    def run(self, ctx: ValidationContext) -> CheckResult:
        imported = ctx.payload("imports").get("callbacks")
        if isinstance(imported, dict):
            payload = imported
        else:
            payload = ctx.service._default_callback_report()
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=payload["status"] != "failed",
        )


class CredentialSafetyCheck(Check):
    key = "credential_safety"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload = credential_safety_report(
            ctx.files,
            ctx.manifest,
            python_files=ctx.python_files,
        )
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=payload["status"] != "failed",
        )


class ExasolCheck(Check):
    key = "exasol"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload = exasol_validation_report(
            ctx.files,
            ctx.manifest,
            python_files=ctx.python_files,
        )
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=payload["status"] != "failed",
        )


class ConsumptionCheck(Check):
    key = "consumption"

    def run(self, ctx: ValidationContext) -> CheckResult:
        payload = validate_consumption_sources(
            ctx.manifest.consumption,
            files=ctx.files,
        )
        return CheckResult(
            key=self.key,
            payload=payload,
            ok=payload["status"] != "failed",
        )


# Evaluation order: each stage may read the results of every stage before it.
PIPELINE_STAGES: tuple[Check, ...] = (
    SyntaxCheck(),
    LintCheck(),
    RequirementsCheck(),
    CrossModuleSymbolCheck(),
    DependencyInstallCheck(),
    ImportSmokeCheck(),
    CallbacksCheck(),
    CredentialSafetyCheck(),
    ExasolCheck(),
    ConsumptionCheck(),
)

# Emission order of the check reports within the validate_workspace payload.
# (Distinct from the evaluation order above: e.g. dependency_install is computed
# before imports but reported last.)
PAYLOAD_KEY_ORDER: tuple[str, ...] = (
    "requirements",
    "lint",
    "syntax",
    "cross_module_symbols",
    "imports",
    "callbacks",
    "credential_safety",
    "exasol",
    "consumption",
    "dependency_install",
)


def run_pipeline(ctx: ValidationContext) -> tuple[dict[str, dict[str, Any]], bool]:
    """Run every stage in order and derive the payload fragment plus ``is_valid``.

    Returns the check reports keyed by payload key (in
    :data:`PAYLOAD_KEY_ORDER`) and the overall ``is_valid`` verdict, which is the
    conjunction of every stage's ``ok`` flag.
    """

    for stage in PIPELINE_STAGES:
        ctx.results[stage.key] = stage.run(ctx)
    is_valid = all(result.ok for result in ctx.results.values())
    reports = {key: ctx.results[key].payload for key in PAYLOAD_KEY_ORDER}
    return reports, is_valid
