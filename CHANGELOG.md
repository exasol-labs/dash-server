# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-31

Initial public release.

`dash-server` is an agent-operated hosting server for Dash apps, purpose-built for turning
database questions into live, query-backed dashboards through MCP. This release includes:

### Platform
- MCP server over Streamable HTTP (`/mcp`), exposing typed tools and resources for the full
  app lifecycle: create, edit, validate, build, preview, promote, roll back, and inspect.
- Git-backed GitOps state model for app source, revision identity, and deployment history —
  the server projection can be rebuilt from the repository rather than depending on opaque
  runtime state.
- Structured diagnostics: logs, tracebacks, health checks, and callback failure reporting
  returned as typed MCP results instead of raw log scraping.
- Per-app runtime isolation modes (dependency environments and process isolation) — see
  [docs/runtime-modes.md](docs/runtime-modes.md).
- A hosted-mode admin surface for operating a shared instance — see
  [docs/hosted-mode.md](docs/hosted-mode.md).
- A browser-facing session channel (`app_session_eval_js` and friends) that lets an agent
  drive a real headless-browser session against a live app: set props, wait for callback
  settlement (including clientside callbacks), read back rendered plots/state, and evaluate
  arbitrary JS in page context.

### Exasol integration
- Local Exasol connection profile management (create, validate, bootstrap from environment
  at startup) with secrets kept outside Git.
- `app_create_exasol_dashboard` and `app_scaffold_from_schema` scaffolds: generate a
  multi-tab analytics hub (system health, query history, business analytics) directly from
  schema introspection.
- A SQL-file authoring convention (`queries/*.sql`) with parameterized placeholders, a
  smoke-test preflight gate, and static lint checks run before every build.
- Governed consumption/export workflow: declare dataset and view outputs on an app and
  export them (CSV, XLSX, and other formats) under server-side policy control.

### Documentation
- Getting-started, MCP client setup, MCP tool/resource reference, architecture, Exasol
  workflow, hosted-mode, and runtime-modes guides under [docs/](docs/).
- Agent-facing skills for building Exasol-backed Dash dashboards and using PyExasol
  directly, under [skills/](skills/).

[Unreleased]: https://github.com/exasol-labs/dash-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/exasol-labs/dash-server/releases/tag/v0.1.0
