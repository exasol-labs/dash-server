# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Exasol profiles can declare session setup statements, `session_init_sql`: run on every new
  connection the profile opens (dashboards, SQL smoke, exports, and `exasol_profile_validate`),
  like a BI tool's "initial SQL". This makes Exasol Semantic Views usable from hosted dashboards:
  set `ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR` and
  Semantic SQL works in `queries/*.sql`. Set it through `exasol_profile_create_local` or the
  `DASH_SERVER_EXASOL_SESSION_INIT_SQL` bootstrap variable. Only `ALTER SESSION`,
  `EXECUTE SCRIPT`, and `OPEN SCHEMA` are accepted. A failing statement closes the connection
  and reports `exasol_session_init_failed`; `exasol_profile_validate` reports the outcome under
  `session_init`. See `docs/exasol.md`.
- `app_run_healthcheck` has a `semantic_models` probe that reports the Exasol Semantic Views
  models an app's queries read. It fails when a model is `INVALID`, `PRECONDITION`, or
  `NOT_PUBLISHED`, and lists models with validation warnings. The `sql_smoke` probe details
  include the same models.
- `dash://exasol/help/semantic-layer`: agent guidance for dashboards on an Exasol Semantic
  Views model. It covers checking for a published model before writing metric SQL, profile
  session setup and grants, query style, never saving compiled SQL into `queries/*.sql`,
  governed labels from `FIELDS_FOR_AGENT`, what the checks catch, and prototyping. A
  `create_exasol_dashboard_on_semantic_model` workflow joins `dash://meta/workflows`;
  `dash://exasol/help/agent-workflow` now starts with "check for a governed semantic model
  first", and `dash://exasol/help/sql-placeholders` warns about summing an order-level amount
  over order lines.
- `exasol_profile_validate` reports the published Exasol Semantic Views models the profile can
  see (`semantic_layer`), including whether each model's preprocessor is active in the profile's
  sessions. Its text names the models, and its next step says to build on them, or first to add
  the `session_init_sql` they need.
- `app_scaffold_from_schema` scaffolds a dashboard on a published semantic model: pass the
  model's published schema as `schema_name` (and an object as `table_name`). Its Semantic SQL
  selects certified metrics, a trend over a time dimension, and a breakdown by a category
  dimension, all from valid metric/dimension combinations. The generated app reads labels,
  formats (`FORMAT_HINT`, `UNIT_HINT`), and certification from the model at runtime, so changes
  made there reach the dashboard, and shows a provenance strip: model version, last finished
  validation, and how many metrics are certified. It has no Exasol system tabs (pattern
  `semantic-model`). Without `schema_name`, the scaffold prefers a usable published model over
  raw tables.
- SQL smoke (build preflight and `app_run_healthcheck`) warns when a query file reads a table that
  backs a published semantic model, naming the published schema to query instead. The warnings
  are in the `sql_smoke` probe's `details.warnings`; they don't fail the check.
- `app_validate` warns when a query file looks like saved `COMPILE_SQL` / `COMPILE_REQUEST_JSON`
  output, which freezes today's metric definitions.
- Builds record which Exasol Semantic Views fields each query file uses, with each field's
  definition hash from the semantic layer's compiler (`COMPILE_SQL` plan). `app_run_healthcheck`'s
  `semantic_models` probe lists fields whose definition changed since the revision was built
  (`changed_since_build`, including fields derived from a changed one), for review. Each entry
  says how it changed: `definition`, `renamed` (with `renamed_to`, found through the model's
  synonyms), `missing`, or `not_compiled` for a file that no longer compiles. The dashboard
  already shows the new definition, so this doesn't fail the probe. Needs a Semantic Views build
  with definition hashes.
- When Semantic Views refuses a query only inside SQL smoke's nested `WHERE 1=0` probe (a renamed
  metric's old name in an ungrouped select, in 0.5.0), smoke compiles the query with `COMPILE_SQL`
  and binds the compiled SQL instead, passing with a warning. Before, the check failed a query
  that works for viewers and suggested the wrong metric.
- For fused Exasol Semantic Views models, the `semantic_models` probe reports partition coverage
  and how each reconciled attribute is answered (`fusion`, from the read-only
  `CHECK_FUSION_HEALTH`), and what changed since the build (`fusion_changed_since_build`): a moved
  partition boundary, or more keys filled or unresolved. A definition hash that moved with the
  fusion layout is marked `fusion_layout_changed`.
- SQL smoke runs query files that call `SEMANTIC_ADMIN.CHECK_FUSION_HEALTH` as written, and skips
  other `EXECUTE SCRIPT` files with the reason instead of failing them inside its `WHERE 1=0`
  wrapper.
- `app_run_healthcheck`'s text states the health status and failed probes, and lists
  semantic-model findings that pass but need review.
- `dash://exasol/semantic-usage` lists which live dashboards use which model fields, with the
  definition hash each was built against: impact analysis before changing or dropping a metric.

### Changed
- `exasol_profile_create_local` with `overwrite: true` on an existing profile keeps every setting
  the call leaves out, including the secret: `{name, overwrite: true, session_init_sql}` adds
  session setup without restating the connection. A new DSN gets its own TLS default unless
  `tls_verify` is given. Creating a profile still needs the connection settings and a secret.
- Agent guidance routes semantic-first. The profile tools, `dash://meta/workflows`, and
  `dash://exasol/help/connection-modes` suggest `app_create_from_files` and point to
  `dash://exasol/help/semantic-layer` when `exasol_profile_validate` lists a published model,
  instead of `app_create_exasol_dashboard`; the semantic workflow is listed first, and
  `dashboard-patterns` and `docs/getting-started.md` say the prebuilt patterns don't read a
  semantic model. `exasol_profile_validate`'s `name` is described as the profile name.
- `dash://exasol/help/semantic-layer` covers both session setup statements
  (`ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT` and `ENABLE_SEMANTIC_SQL()`), says to run Semantic
  SQL through the Exasol MCP server one call at a time and what `not a valid GROUP BY expression`
  means there, drops the outdated "don't pass `row_limit`" advice, and says the per-request
  compile lane isn't supported. `dash://exasol/help/sql-placeholders` says to write literal
  braces as `{{ }}`, and a query that fails on one now says so instead of reporting a bare
  `KeyError`.
- `app_validate`'s "does not declare LIMIT or obvious aggregation" warning no longer fires for
  Semantic SQL against a `SEMANTIC_*` schema (its metrics aggregate implicitly), and "Prefer one
  statement per Exasol SQL file" ignores semicolons inside comments and string literals.
- `app_scaffold_from_schema` no longer treats every `SEMANTIC_*` schema as platform-internal. Only
  Semantic Views' own schemas (`SEMANTIC_ADMIN`, `SEMANTIC_AGENT`, `SEMANTIC_CATALOG`,
  `SEMANTIC_SOURCE`, `SYS_SEMANTIC`) are skipped. Published model schemas are scaffolded through the
  semantic path, and a user schema that merely starts with `SEMANTIC_` is ordinary data again.
- The Exasol skill (`skills/exasol_dash_skill.md`) has a semantic-layer section, and its creation
  path advice now matches `dash://exasol/help/dashboard-patterns`: `app_create_from_files` is the
  default once the dashboard is designed. It used to rank it last.
- The SQL smoke check (build preflight and `app_run_healthcheck`) understands Exasol Semantic
  Views. A query file that reads a published semantic model on a profile whose sessions don't
  activate the model's preprocessor now fails with the `session_init_sql` line to add. Before,
  the `WHERE 1=0` probe could pass such a query, and it then failed for the first viewer.
- A `data_layer` healthcheck error stops counting once its SQL file runs successfully again in
  the same revision, so a repaired data source no longer needs `app_acknowledge_data_layer_errors`.
  A repeat of the same error after a success is recorded even within the 60-second
  deduplication window.
- A query file whose statement returns no result set (such as `ALTER SESSION`) now returns no
  rows instead of failing with "Attempt to fetch from statement without result set". The
  cached connection is still discarded afterwards, so a session change can't leak into other
  apps sharing the profile.
- An app whose first revision fails artifact preflight (from `app_create`,
  `app_create_from_files`, `app_create_exasol_dashboard`, or `app_scaffold_from_schema`) is no
  longer started: there is no earlier revision to fall back on, so the failing one would serve
  viewers. The `artifact_preflight_failed` error says so; fix the draft and run
  `app_deploy_draft`, which builds, checks, and starts the app.
- `app_promote_revision` adds a `warnings` entry when the revision has a recorded build-preflight
  or data-layer failure (`require_healthy: true` still refuses it). A preview `app_deploy_draft`
  whose preflight failed says so in its result text, and preflight errors name the failing check.
- The `semantic_models` healthcheck probe takes a model's readiness from its newest *finished*
  validation run. A run left unfinished (Exasol Semantic Views 0.5.0's `CHECK_PARTITION_COVERAGE`
  leaves one behind on every call) made an `INVALID` model read as `VALID`. Without read access to
  `SEMANTIC_SOURCE`, such a model is reported as `UNKNOWN`. Each model that isn't `VALID` now
  carries the rule codes and messages behind its status (`issues`), and a failure names the first.
- The `semantic_models` probe covers query files that bypass a semantic model. Saved compiled
  Semantic SQL fails the probe (`frozen_files`), so the app reports `degraded` instead of
  `healthy` while it serves frozen definitions. Hand-written SQL over a model's source tables is
  listed under `raw_governed_reads`. `dash://exasol/semantic-usage` lists both under `bypasses`.
- Data-layer errors shown in dashboards (the `_error` envelope that `render_error_panel`
  displays) carry only the database's own message plus an `(error ref …)` suffix. The DSN,
  database user, session id and SQL text no longer reach viewers; the full error stays on
  `dash://apps/{name}/errors`, whose `details.error_ref` matches the reference.
- A cached Exasol connection is replaced when its profile's settings change (for example a
  rewrite with `overwrite: true`). Before, a busy thread kept using a connection opened from the
  old DSN, user, or session setup until it went idle.
- **Breaking for hosted apps:** dashboard queries (`query_rows`, `query_one`, `query_scalar`
  and the generated `load_*` wrappers) now return `DECIMAL` values as `int` or
  `decimal.Decimal`, `DATE` as `datetime.date`, and `TIMESTAMP` as `datetime.datetime`.
  Before, pyexasol delivered most of these as strings, so charts and arithmetic needed manual
  conversion. App code that treats these values as strings, such as `row["DAY"][:7]` or
  `date.fromisoformat(row["DAY"])`, must be updated. Numbers with more than 15 significant
  digits stay strings so the browser doesn't round them, including 16–18 digit
  `DECIMAL(p,0)` values that used to arrive as `int`. The generated scaffolds already work
  with typed values, and the agent guidance (`dash://exasol/help/agent-workflow`, the Exasol
  dashboard skill) documents the new types.
- Dependencies updated: pyexasol 3.0.1 (the upper bound is raised from `<3.0` to `<4.0`; 3.0.1 fixes
  parsing of `INTERVAL DAY TO SECOND` values below 9-digit day precision, exasol/pyexasol#441),
  cryptography 50.0.2, Werkzeug 3.1.9, pydantic 2.14.1, setuptools 84.2.1, ruff 0.17.1, and
  mypy 2.4.0, plus transitive updates. pyexasol 3's breaking changes are in its DBAPI module
  (type conversion defaults, removal of `pyexasol.db2`). dash-server uses `pyexasol.connect`,
  whose results are unchanged.
- pyexasol 3 sends anonymized usage telemetry to Exasol on every connection. dash-server turns
  this off by setting `EXASOL_TELEMETRY_DISABLE=1` before it loads pyexasol, unless the
  variable is already set.

### Fixed
- With shared dependencies (the default), an app's `requirements.txt` could change the versions
  of packages in the server's own environment, including dash-server's dependencies, which broke
  the server for every app. Installs now only add packages: a requirement that excludes an
  installed version fails with `conflicts` naming it, and installs run with every installed version
  pinned as a constraint.
- Scaffolded Exasol apps (`app_create_exasol_dashboard`, `app_scaffold_from_schema`) still
  declared `pyexasol>=2.2.2,<3.0` in `requirements.txt` after the server moved to pyexasol 3.
  With shared dependencies, building such an app installed pyexasol 2 into the server's own
  environment. They now declare the server's ranges (`dash>=4.3,<5.0`, `pyexasol>=3.0.1,<4.0`),
  and a test keeps them in step.
- Isolated-mode app workers ignored `SIGTERM`. The worker's handler called `httpd.shutdown()` on
  the thread running `serve_forever()`, which deadlocks, so only the manager's `SIGKILL` fallback
  stopped them. Workers forked from the forkserver baseline also inherited the baseline's signal
  handlers and listening socket: on `SIGTERM` they unlinked the baseline's socket file.
- The forkserver baseline outlived its server: nothing stopped it when the server process ended,
  and it was never adopted by the next one. It now exits when its parent process does.
- The test suite left worker and baseline processes running after every run of the isolated-runtime
  tests. Tests now stop every worker manager they create.
- XLSX exports now write `DECIMAL`, `DATE`, and `TIMESTAMP` columns as number and date cells.
  Before, pyexasol delivered those values as strings, so they landed in the workbook as text
  that couldn't be summed or sorted, despite what the docs said. The XLSX export connection
  now uses a typed `fetch_mapper` built on pyexasol 3's `pyexasol.data_types`. CSV exports
  are unchanged. Numbers with more than 15 significant digits are now written
  as text, so Excel no longer rounds them (this includes `DECIMAL(16..18,0)` values that were
  written as numbers before). Dates before 1900 and `INTERVAL` values are also written as text.
- Reading Exasol scaffold templates no longer passes two segments to `joinpath`. On Python
  3.10 this fails for packages that are not installed as plain files on disk (for example
  zip imports).
- `mypy` now passes cleanly. It previously reported 129 errors, mostly from the MCP server
  mixins calling helpers defined on their siblings. `types-openpyxl` is added to the dev
  extras.
- CI now fails on mypy errors (before, it only reported them), as it already did on
  `ruff check`. CI installs from `uv.lock` with `uv sync --locked`. This pins mypy and ruff to
  the locked versions and fails the build if the lock has drifted from `pyproject.toml`.

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
