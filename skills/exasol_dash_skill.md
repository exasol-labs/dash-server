# Skill: Exasol Dash for Claude Code / Codex

Use this skill when you need to design, generate, patch, or review Dash dashboards that query Exasol directly from Python. It is optimized for the `dash-server` workflow, but the patterns also apply to standalone Dash apps.

Companion skill:

- Read [pyexasol_skill.md](pyexasol_skill.md) when you need connector-specific details, placeholder syntax, bulk I/O, metadata helpers, or low-level connection tuning.

## What this skill is for

- Turning Exasol data into polished, responsive Dash dashboards.
- Choosing chart and table patterns that fit Exasol’s strengths.
- Structuring SQL, callbacks, and refresh logic so the app is fast, maintainable, and agent-editable.
- Avoiding common mistakes like import-time queries, oversized result sets, and charts that should have been aggregated in SQL.

## Core design rule

**Let Exasol compute. Let Dash orchestrate and render.**

That means:

- push filters, joins, aggregations, bucketing, ranking, and window logic into SQL
- return relatively small, presentation-ready result sets
- keep Python callbacks focused on:
  - selecting parameters
  - calling queries
  - small formatting / figure assembly
  - coordinating UI state

Do not use Dash callbacks to simulate a database engine.

## Non-negotiable rules

1. Never query Exasol at module import time.
2. Never open Exasol connections inside `create_dash_app()` just to build the layout.
3. In `dash-server`, always use:
   - `routes_pathname_prefix="/"`,
   - `requests_pathname_prefix=url_base_pathname.rstrip("/") + "/"`,
   - `app.callback`, not global `dash.callback`.
4. Keep secrets out of app source, manifests, Git, logs, and visible UI.
5. Use parameterized SQL through `pyexasol`, never string concatenation for values.
6. Return only the columns and rows the component actually needs.
7. Prefer one good query per visual state over shipping a giant raw dataset to Python and slicing it there.

## Default architecture for an Exasol-backed Dash app

For `dash-server`, prefer this shape:

- `dash-app.json`
  - bind `data_sources.primary.profile`
- `app.py`
  - define layout and callbacks
- `dash_server_exasol.py`
  - helper functions for query execution and result shaping
- `queries/`
  - one SQL file per dashboard concern

Recommended callback flow:

1. user changes filters
2. callback derives query params
3. helper executes one SQL file against the bound profile
4. callback converts rows into a figure, grid, KPI cards, or summary text

## Chart selection guidance

Choose charts based on the decision the user needs to make, not on chart novelty.

### KPI cards

Use for:

- totals
- rates
- deltas vs prior period
- SLA / threshold indicators

Exasol pattern:

- pre-compute KPI rows in SQL
- return one row with named metrics
- avoid multiple round-trips for adjacent KPIs

### Time-series line charts

Use for:

- trend over time
- seasonality
- comparing a small number of series

Exasol pattern:

- aggregate in SQL to the displayed grain:
  - minute
  - hour
  - day
  - week
  - month
- do not ship event-level rows when the chart shows daily values
- cap the number of series aggressively

### Bar charts

Use for:

- ranked categories
- small categorical comparisons
- deviations from target

Exasol pattern:

- compute `TOP N` in SQL
- sort in SQL
- optionally add an `OTHER` bucket in SQL instead of flooding the chart with long tails

### Stacked / grouped bars

Use for:

- composition across a modest number of categories
- comparison of parts across periods or segments

Exasol pattern:

- pre-aggregate the stacked grain in SQL
- avoid stacks with too many colors or too many categories

### Scatter plots

Use for:

- relationships between two measures
- clusters
- outlier detection

Exasol pattern:

- sample or aggregate first
- do not send hundreds of thousands of raw points unless the question truly needs it
- if density matters, consider hexbin / heatmap-style aggregation instead

### Heatmaps

Use for:

- hour-of-day by day-of-week
- segment vs metric intensity
- correlation-like operational views

Exasol pattern:

- compute the matrix in SQL
- return a compact grid

### Tables / grids

Use for:

- operational drill-down
- reconciliations
- downloadable detail

Prefer `dash-ag-grid` over `dash_table` for new work.

Reason:

- Dash docs now position AG Grid as the high-performance grid path, while `dash_table` is deprecated in core docs.

Exasol pattern:

- paginate or filter server-side for large result sets
- only use full client-side row payloads for modest result sizes

## Chart anti-patterns

Avoid:

- pie / donut charts with many categories
- dual-axis charts unless the relationship is genuinely worth the cognitive cost
- raw event scatter plots when a trend chart or binned view answers the question better
- dashboards with six charts that all repeat the same dimension at different angles

## SQL design patterns for Dash

### Prefer narrow queries

Each query should have a single clear purpose:

- KPI summary
- trend series
- top-N breakdown
- drill-down table

Do not write one mega-query that tries to feed the entire dashboard unless the dashboard is genuinely tiny.

### Keep SQL files organized by intent

Recommended naming:

- `queries/kpis.sql`
- `queries/trend_daily.sql`
- `queries/top_customers.sql`
- `queries/detail_grid.sql`

### Always aggregate to visual grain

Examples:

- line chart by day: query returns one row per day
- bar chart by region: query returns one row per region
- KPI card set: query returns one row with named metrics

### Use SQL for ranking, bucketing, and windows

Push down:

- `ROW_NUMBER()`, `RANK()`, `DENSE_RANK()`
- moving averages
- percent-of-total
- period-over-period deltas
- cohort bucketing
- date truncation / time bucketing

### Minimize type friction

Exasol docs emphasize appropriate and consistent data types.

So:

- avoid joining unlike types
- prefer exact numeric types for business metrics where practical
- cast deliberately in SQL, not ad hoc in Python

### Parameterize all user-driven filters

Examples:

- date range
- region
- product family
- customer segment
- threshold values

Never concatenate values into SQL strings.

## Exasol-first server-side aggregation strategies

### Strategy 1: summary row + small supporting dimensions

Good default for dashboards:

- one KPI summary query
- one trend query
- one ranked categorical query
- one detail query

This keeps each callback understandable and patchable.

### Strategy 2: pre-bucket for time-series

For high-volume facts:

- bucket in SQL
- compute only the displayed period
- push comparisons into SQL if they depend on the same grain

### Strategy 3: top-N with long-tail suppression

When categories explode:

- rank categories in SQL
- keep top N
- optionally fold the rest into `OTHER`

### Strategy 4: drill-down only when requested

Do not load detailed rows on first render unless the dashboard is explicitly detail-first.

Pattern:

- first callback loads summary views
- second callback loads drill-down grid from selected chart point / row / filter

## Refresh and caching patterns

### Default rule

If the query is cheap and user-driven, run it synchronously in the callback.

If the query is slow or expensive:

- cache by filter arguments
- or use Dash background callbacks when the UX truly benefits

### Good refresh patterns

- explicit `Refresh` button for operational dashboards
- `dcc.Interval` for near-real-time monitoring
- time-bounded cache for repeated views

### Bad refresh patterns

- re-running every query on every tiny UI change
- polling at aggressive intervals without operational need
- storing giant result sets in the browser

### Browser state

Use `dcc.Store` for:

- active filters
- selected entity ids
- compact derived metadata

Do not use `dcc.Store` as a place to dump large Exasol query results.

## Dash interaction patterns that work well with Exasol

### Pattern: filter panel -> summary callbacks

Best for:

- executive and operational dashboards

Structure:

- top filter controls
- KPI row
- 2-4 summary visuals
- optional drill-down grid below

### Pattern: tabs for separate query families

Use tabs when each view has a meaningfully different query shape.

Example:

- Overview
- Geography
- Customers
- Operations

Do not use tabs just to hide an overstuffed dashboard.

### Pattern: click-to-drill

Use chart click or grid row selection to load a focused detail view.

Keep the detail callback separate from the summary callback.

### Pattern: loading feedback

Wrap slow outputs with `dcc.Loading`.

For long-running work, Dash docs support background callbacks. Use them selectively, not by default.

### Pattern: partial updates

If only a small part of the UI changes, consider Dash `Patch` / partial property updates rather than re-sending whole structures.

Good use cases:

- adding dynamic filters
- appending small UI elements
- minor figure/layout adjustments

## Grid guidance

For new dashboards, prefer `dash-ag-grid`.

Default AG Grid choices:

- size the grid explicitly
- use pagination or constrained scrolling
- enable filtering/sorting where it helps the workflow
- keep row payloads modest

If the result can become large:

- page server-side
- or constrain the query by default date / entity filters

## Figure-building guidance

### Prefer clear over ornate

- direct labels
- consistent colors
- stable category ordering
- obvious units
- readable tick formats

### Avoid over-formatting in Python

If a label, bucket, or ordering belongs to the data model, do it in SQL.

Use Python for:

- figure assembly
- annotation placement
- light display formatting
- interaction wiring

## Review checklist for agent-generated Exasol dashboards

Before shipping, check:

1. Does every query run only inside callbacks or explicit refresh helpers?
2. Are all user-driven values parameterized?
3. Is Exasol doing the aggregation instead of Python?
4. Are returned result sets small enough for the target component?
5. Does each visual answer a distinct question?
6. Is the default page fast enough on first load?
7. Are slow outputs wrapped with loading feedback?
8. Are secrets absent from source, manifests, logs, and visible text?
9. If using `dash-server`, does `create_dash_app()` follow the required hosted pattern?
10. Would a human be able to patch the SQL and callback flow without reverse-engineering magic?

## Default build recipe

When generating an Exasol dashboard from scratch, prefer this sequence:

1. Define the dashboard questions first:
   - what decision should the user make?
   - what filters matter?
   - what grain should each visual show?
2. Pick 3-5 visuals maximum for the first version.
3. Write one SQL file per visual concern.
4. Ensure every SQL file returns presentation-ready rows.
5. Build simple, explicit callbacks.
6. Add drill-down only after the summary view works.
7. Add caching / background execution only when query latency justifies it.

## What “amazing” means here

An amazing Exasol dashboard is not the one with the most charts. It is the one that:

- opens quickly
- answers a real business question
- uses Exasol for the heavy lifting
- gives the user clean interactions
- is easy for an agent to extend safely
- still looks deliberate and polished

Optimize for clarity, speed, and maintainability first. The polish should reinforce the data story, not distract from it.
