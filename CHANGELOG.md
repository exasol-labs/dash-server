# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-09-28

### Added
- Certificate pinning for Exasol profiles. A DSN of the form `host/FINGERPRINT:port` (the
  SHA-256 of the server certificate) now authenticates a self-signed server by its exact
  certificate, whatever `tls_verify` is set to. This gives remote Exasol Personal and other
  self-signed instances a secure alternative to `tls_verify=false`. Previously the explicit
  `CERT_REQUIRED` rejected a pinned self-signed certificate before pyexasol could check the
  pin. A fingerprint mismatch is classified as `tls_fingerprint_mismatch` with a hint.
- `exasol_profile_create_local` and the startup bootstrap now reject DSNs that pyexasol 2.4.1
  cannot parse, with an explanation, instead of failing at connect time. Examples are a
  fingerprint after the port (`host:port/FP`) and IPv6 literals.
- Hosted-app validation warns about plotly APIs removed in plotly 7: `*mapbox` traces and
  `px.*_mapbox`, `layout.mapbox`, the removed `figure_factory` functions, and
  `write_image(engine=...)`. Each warning names the replacement.

### Changed
- Dependencies updated: pyexasol 2.4.1 (stricter DSN parsing), cryptography 50.0.1 (the
  upper bound is raised from `<50` to `<52`; includes the fix for CVE-2026-69247), plotly 7.1.0
  via Dash, which moves hosted apps to plotly.js 4, packaging 26.3, urllib3 2.8.0 (three
  security fixes), setuptools 84, ruff 0.16.8, and mypy 2.3.1. The unused `wheel` build
  requirement is dropped. `uv.lock` had drifted from `pyproject.toml` and is regenerated.
- The 8-digit hex color lint warning now notes that plotly 7 accepts `#RRGGBBAA`. It still
  warns, for apps that may run on an older plotly.
- Exasol profiles created without an explicit `tls_verify` (the `DASH_SERVER_EXASOL_TLS_VERIFY`
  bootstrap variable unset, or `exasol_profile_create_local` called without it) now default to
  `tls_verify=false` when every DSN host is loopback, and `true` otherwise. The README's
  bootstrap block (`localhost:8563`) now connects to Exasol Personal's self-signed certificate
  out of the box. TLS stays on either way, and existing profiles are not rewritten.
  `DASH_SERVER_EXASOL_TLS_VERIFY` and the other bootstrap variables are now documented.
- The Exasol startup bootstrap now reports when an existing profile differs from the
  `DASH_SERVER_EXASOL_*` settings: a startup warning lists each differing field and explains how
  to apply them. Previously the settings were silently ignored, because the "not modifying" notice
  was logged at INFO, which is not shown by default. The new `DASH_SERVER_EXASOL_OVERWRITE=true`
  rewrites the profile from the settings on startup. The default stays create-only.

### Fixed
- Validation no longer runs the code of an app that fails the credential-safety check. The
  import smoke check executes `create_dash_app()`, so an app calling `pyexasol.connect(...)` with
  embedded credentials opened a real database connection from inside the control plane during
  validation. The import check is now skipped (`credential_safety_failed`) once credential safety
  has failed; validation fails either way.
- An app that queries Exasol through `dash_server_exasol` but binds no Exasol data source now
  fails validation and is not created. Before, it was built, preflighted and published: with no
  profile, `sql_smoke` reported `not_applicable`, and the first callback returned HTTP 500 with
  `Hosted app metadata does not declare Exasol data_sources`. The error names the files and the
  `data_sources` form to add. Where `dash-app.json` uses an unrecognised binding key such as
  `"exasol": {...}`, the error names that key. An `exasol-analytics` app that never calls the
  helper only gets a warning.
- `app_create_from_files` no longer silently drops manifest arguments when a `dash-app.json`
  is supplied, which used to overwrite them. An argument now fills a field the file leaves out
  (reported in `notes`), and a field set differently in both is rejected with
  `bundle_validation_error`. The helper module is now also injected when
  `template: exasol-analytics` is set only in the supplied manifest.
- `app_scaffold_from_schema` no longer makes a surrogate key the headline KPI. Before, only
  `*_ID`/`ID` columns were treated as keys, so on a dbt mart `SUPPLIER_KEY` was the first
  numeric column and the summary card showed `SUM(SUPPLIER_KEY)`. Key columns are now
  recognised by name (`*_KEY`, TPC-H-style `*KEY`, `*_ID`, `*_SK`, `*_PK`, `*_FK`) and by
  declared `PRIMARY KEY`/`FOREIGN KEY` constraints, and are excluded from measures. The
  remaining measures are ranked for the SUMmed headline: money first (revenue, sales, amount,
  cost, …), then other additive quantities, with scores, ratios, rates and other
  non-additive values last. `*_KEY` columns now also count as join keys for relationship
  hints.
- The scaffold's derived `REVENUE = SUM(quantity * price)` now pairs a quantity only with a
  per-unit price (`PRICE`, `NET_UNIT_PRICE`, `LIST_PRICE`, `UNIT_COST`). Before, it paired
  any column containing `PRICE`, `AMOUNT`, `RATE`, `FEE`, `REVENUE` or `COST`. That
  double-counted the quantity for totals, e.g. TPC-H's `L_QUANTITY * L_EXTENDEDPRICE`, and
  multiplied quantities by rates such as `TAX_RATE`.
- The built-in `demo` app is now seeded on first start even when the Exasol bootstrap
  profile env vars (`DASH_SERVER_EXASOL_PROFILE_NAME` etc.) are set. Previously the profile
  commit made the fresh GitOps repo look already-initialized, so `/apps/demo` — the README's
  "First check" — returned 404. Affected instances self-heal on the next restart.
- `dash-server` no longer crashes on first start in environments without `pip`, such as the
  README's `uv venv` install path. Requirements the server environment already satisfies
  (including the demo's `dash`) are no longer reinstalled; otherwise the installer falls back
  to `uv pip install --python …` when `pip` is absent, or reports an actionable error.
- The wheel now ships the Flask page templates; a non-editable install no longer returns 500
  (`TemplateNotFound: dashboard_catalog.html`) on `/`.
- `/apps/<name>` for a registered app that is not mounted (e.g. its revision failed to load
  at startup) now returns 503 `dashboard_not_running` with the last error and a pointer to
  `app_get_status`, instead of a bare 404. Unknown dashboards return a JSON 404
  `dashboard_not_found`, as do deliberately disabled apps; hosted-mode denials are
  unchanged.
- Restarting the server no longer adds an identical `app/<name>: record release … on main`
  commit to the GitOps repo for every existing release. Startup backfill re-rendered each
  release manifest with a new `createdAt`; the original timestamp is now kept.

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

[Unreleased]: https://github.com/exasol-labs/dash-server/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/exasol-labs/dash-server/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/exasol-labs/dash-server/releases/tag/v0.1.0
