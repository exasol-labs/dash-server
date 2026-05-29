<p align="center">
  <img src="docs/logo.svg" alt="dash-server logo" width="140" />
</p>

<h1 align="center">dash-server</h1>

<p align="center">
  Agent-operated Dash hosting for live analytical apps
</p>

<p align="center">
  <a href="docs/mcp-reference.md"><img alt="MCP First" src="https://img.shields.io/badge/MCP-first-0B1220?style=flat-square&logo=protocols.io&logoColor=white"></a>
  <a href="docs/exasol.md"><img alt="Exasol Ready" src="https://img.shields.io/badge/Exasol-ready-1E5EFF?style=flat-square&logo=databricks&logoColor=white"></a>
  <a href="docs/architecture.md"><img alt="GitOps Backed" src="https://img.shields.io/badge/GitOps-backed-0F766E?style=flat-square&logo=git&logoColor=white"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
</p>

`dash-server` is an agent-first host for Dash apps, optimized for turning database questions into working dashboards.

It is built for a workflow like this:

- a user asks for a dashboard
- an agent creates or updates the app through MCP
- the app is validated, built, and deployed with revision tracking
- the user opens a real browser URL and uses the result

The current product direction is especially focused on Exasol-backed dashboards. Instead of making an agent invent connection handling, scaffold structure, and deployment flow from scratch, `dash-server` gives it a purpose-built control plane for creating live Dash apps that query Exasol.

What makes that especially powerful is that the target is not a fixed BI canvas. The target is a real Python data app.

That means an agent is not limited to arranging charts on a dashboard page. It can generate:

- rich Plotly visuals across statistical, financial, scientific, geographic, and 3D use cases
- reactive Dash applications with callbacks, clientside interactions, multi-page routing, and grid-heavy workflows
- Exasol-backed query flows that push large aggregations and analytical logic into a high-performance SQL engine before the UI renders

## Why It Exists

Most app-hosting workflows assume a human operator stitching together code edits, environment setup, deployment steps, and debugging. `dash-server` takes a different approach:

- end users interact with dashboards in the browser
- agents operate the platform through MCP
- Git keeps the durable app and deployment history

That matters because it gives the agent a structured environment instead of a vague shell session:

- app creation and updates happen through typed tools
- revisions are buildable, previewable, promotable, and rollbackable
- diagnostics come back as structured MCP results
- the user gets a stable browser URL instead of a pile of intermediate steps

## Why This Beats Point-and-Click BI

Tools like Power BI and Tableau are strong when the problem fits their model: predefined visuals, predefined interactions, and a mostly human-authored dashboard lifecycle.

`dash-server` is aimed at a different class of problem.

With Dash, Plotly, and Exasol together, you are not composing a report. You are programming an analytical product:

- Dash gives you application behavior, not just layout. A dashboard can react to any combination of filters, clicks, selections, timers, state, URLs, and custom workflow logic.
- Plotly gives you a much broader visual grammar than typical BI builders: mixed trace types, subplots, maps, animations, financial charts, scientific charts, dense statistical views, and interactive events on top of those figures.
- Exasol gives you a very strong execution layer for heavy analytical SQL, large aggregations, high concurrency, and parallel execution, so the expensive work happens in the database rather than in the browser.
- PyExasol gives Python-native access to that engine, including pandas-friendly flows and parallel data streams, so the app can stay in Python without falling back to fragile extracts.

That combination is more flexible than point-and-click BI for a simple reason: the interface, the logic, and the data path are all programmable.

In practice, that means you can build experiences that are awkward or impossible in traditional BI tools, such as:

- drill-through flows that depend on domain logic, not only fixed cross-filter rules
- dashboards that combine forms, charts, tables, health indicators, and custom actions in one app
- views that switch between operational monitoring, exploratory analysis, and workflow execution
- query patterns that deliberately push complex shaping and aggregation into Exasol before rendering
- app behavior that can be generated and iterated on by an agent instead of rebuilt manually in a GUI

The pitch is not "BI, but automated." It is "analytical apps generated at the speed of prompting, with the full expressive power of Python, Plotly, and Exasol behind them."

## Why GitOps Matters Here

`dash-server` stores its permanent state in Git because agents need a control plane that is durable, inspectable, and easy to recover.

That includes:

- app source
- revision identity
- release metadata
- live and preview deployment intent
- canonical deployment history

This matters for three reasons.

First, Git gives you an audit trail that is easy for both humans and agents to understand. You can see what changed, when it changed, and what revision became live.

Second, Git makes the platform recoverable. The server can rebuild its local projection from the repository instead of depending on fragile runtime state or a hidden database as the only source of truth.

Third, Git makes agent operation safer. If an agent creates, edits, validates, promotes, or rolls back a dashboard, those actions land in a durable history instead of disappearing into an opaque deployment system.

In other words, the GitOps model is not there for fashion. It is there because once agents become operators, you need versioned state, reproducibility, rollback, and a clear operational record by default.

## What It Enables

Today, `dash-server` lets an agent:

- create new Dash apps from scaffolds or uploaded files
- edit draft source files
- validate hosted-app structure before deployment
- build immutable revisions
- start previews and promote them live
- inspect logs, tracebacks, health, and callback failures
- return full browser URLs for deployed dashboards
- work against a Git-backed hosting model instead of opaque runtime state

For Exasol-specific workflows, it also lets an agent:

- create local Exasol connection profiles
- validate those profiles
- generate Exasol dashboard scaffolds with SQL files and runtime helpers
- introspect schemas and generate a tailored Exasol scaffold from visible tables and columns
- keep Exasol secrets outside Git while storing profile metadata in the GitOps repo

That is the core specialization of this project: making it easy for an agent to move from "show me the shape of this business problem" to "here is a live, query-backed data application" without dropping into an unstructured devops workflow.

## Exasol-Focused Dashboard Delivery

The Exasol path is the most important specialization in the current codebase.

The intended flow is:

1. Start `dash-server` with an Exasol profile already bootstrapped on the server
2. Let the agent validate or reuse that profile
3. Generate a scaffold with `app_create_exasol_dashboard` or `app_scaffold_from_schema`
4. Let the agent refine the SQL and Dash code
5. Deploy to `/preview/{name}/{revision}` for review, then promote to `/apps/{name}`

The generated scaffold includes:

- `dash-app.json`
- `app.py`
- `dash_server_exasol.py`
- `queries/system/meta.sql`
- `queries/system/monitor.sql`
- `queries/system/usage.sql`
- `queries/system/sql_hist.sql`
- `queries/business/summary.sql`
- `queries/business/trend.sql`
- `queries/business/detail.sql`
- `requirements.txt`

The default `exasol-analytics` scaffold is now a multi-tab analytics hub with system health, query history, and a business analytics area the agent can specialize from schema discovery.

More detail: [docs/exasol.md](docs/exasol.md)

## How People Typically Use It

The normal experience looks like this:

1. Start `dash-server`
2. Connect Claude, ChatGPT, or another MCP-capable agent
3. Ask the agent to create or update a dashboard
4. Open the returned live URL, such as `/apps/sales`
5. If something fails, let the agent use diagnostics to fix and redeploy it

There is no human admin UI. The browser-facing product is the hosted dashboards themselves.

## Quick Start

### Install

Using `uv`:

```bash
uv venv
. .venv/bin/activate
uv pip install -e ".[dev]"
```

Using standard `venv` + `pip`:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e ".[dev]"
```

### Start the server with Exasol

The fastest path is to let the server bootstrap one Exasol profile at startup.

Export the Exasol secret and the minimal profile settings before starting `dash-server`:

```bash
export EXA_PASSWORD='your-secret-password'
export DASH_SERVER_EXASOL_PROFILE_NAME='analytics-prod'
export DASH_SERVER_EXASOL_DSN='localhost:8563'
export DASH_SERVER_EXASOL_USER='sys'
```

Optional startup settings:

```bash
export DASH_SERVER_EXASOL_DESCRIPTION='Primary analytics database'
export DASH_SERVER_EXASOL_SECRET_ENV_VAR='EXA_PASSWORD'
export DASH_SERVER_EXASOL_CREDENTIAL_MODE='password'
```

Then start the server:

```bash
. .venv/bin/activate
dash-server
```

Default local URL:

- `http://127.0.0.1:5000`

### First check

Open:

- `http://127.0.0.1:5000/apps/demo`

That verifies the hosted Dash runtime is up.

### Confirm the Exasol profile is ready

If you used the startup bootstrap env vars above, `analytics-prod` should already exist without any MCP profile-creation step:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "resources/read",
    "params": {
      "uri": "dash://exasol/profiles"
    }
  }'
```

From there, your agent can immediately create dashboards against `analytics-prod`.

## Connect an Agent

`dash-server` exposes MCP over Streamable HTTP at:

- `http://127.0.0.1:5000/mcp`

For local Claude Desktop, you can bridge that HTTP endpoint with `mcp-remote`:

```json
{
  "mcpServers": {
    "dash-server": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:5000/mcp"]
    }
  }
}
```

More setup detail:

- [docs/getting-started.md](docs/getting-started.md)
- [docs/mcp-clients.md](docs/mcp-clients.md)
- [docs/exasol.md](docs/exasol.md)

## Design Summary

The current implementation combines:

- a Flask control-plane host
- an MCP-first operating surface
- a multi-app Dash runtime
- Git-backed draft workspaces, revision metadata, desired state, and deployment history
- a rebuildable SQLite projection for fast local reads

For the deeper design and code-level walkthrough, see [docs/architecture.md](docs/architecture.md).

## Documentation

- Getting started: [docs/getting-started.md](docs/getting-started.md)
- MCP client setup: [docs/mcp-clients.md](docs/mcp-clients.md)
- MCP tools and resources: [docs/mcp-reference.md](docs/mcp-reference.md)
- Exasol workflow: [docs/exasol.md](docs/exasol.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Runtime modes (dependency + process isolation): [docs/runtime-modes.md](docs/runtime-modes.md)
- Product plan: [plans/dash-server-spec.md](plans/dash-server-spec.md)
- GitOps plan: [plans/gitops-storage-and-revision-spec.md](plans/gitops-storage-and-revision-spec.md)
- Claude interactive connectors plan: [plans/claude-interactive-connectors-spec.md](plans/claude-interactive-connectors-spec.md)
- Exasol optimization plan: [plans/exasol-dashboard-optimization-spec.md](plans/exasol-dashboard-optimization-spec.md)

## Current Status

The repository currently includes:

- the staged Dash hosting/control-plane implementation through the Stage 4 line plus later usability and GitOps work
- GitOps storage and revision tracking through Phase 4A
- the Exasol dashboard optimization work through Phase 0
- the **runtime-isolation work** through Phase 5: per-app dependency environments, out-of-process workers with forkserver baselines, idle-stop with hot restart, environment + wheel-cache GC, and the hosted-mode safety gate. See [docs/runtime-modes.md](docs/runtime-modes.md) for the operator-facing overview and [plans/app-runtime-isolation-and-dependency-environments-plan.md](plans/app-runtime-isolation-and-dependency-environments-plan.md) for the design history.

What it already does well:

- agent-operated Dash hosting
- structured validation and diagnostics
- revisioned deploy, preview, promote, and rollback
- Git-backed app and deployment history
- Exasol profile creation and dashboard scaffolding
- per-app dependency environments + out-of-process workers (opt-in via `DASH_SERVER_APP_DEPENDENCY_ISOLATION=per_app` and `DASH_SERVER_APP_RUNTIME_MODE=isolated`)

What it does not yet provide:

- a real security sandbox (the runtime isolation is **operational**, not **security**; see [plans/runtime-sandboxing-adapter-plan.md](plans/runtime-sandboxing-adapter-plan.md))
- remote Git sync workflows
- a human admin console
- the later Exasol phases around richer discovery, SaaS modes, and advanced dashboard generation

## Running Tests

```bash
. .venv/bin/activate
pytest
```
