# Getting Started

This guide covers the shortest path to running `dash-server` locally for its primary use case: Exasol-backed dashboard delivery.

It verifies all three important surfaces:

- the Exasol profile bootstrap path
- the browser-facing Dash runtime
- the MCP control plane at `/mcp`

## Requirements

- Python 3.10+
- macOS, Linux, or Windows

## Install

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

## Start the Server with an Exasol Profile

The simplest operator setup is to let `dash-server` create one reusable Exasol profile at startup.

Set the secret and bootstrap settings before starting the server:

```bash
export EXA_PASSWORD='your-secret-password'
export DASH_SERVER_EXASOL_PROFILE_NAME='analytics-prod'
export DASH_SERVER_EXASOL_DSN='localhost:8563'
export DASH_SERVER_EXASOL_USER='sys'
```

Then start the server:

```bash
. .venv/bin/activate
dash-server
```

Default local address:

- `http://127.0.0.1:5100`

## Port Configuration and macOS AirPlay Receiver

The control-plane HTTP listener defaults to `127.0.0.1:5100`.

You can change it with CLI flags:

```bash
dash-server --host 127.0.0.1 --port 5200
```

Or with environment variables:

```bash
export DASH_SERVER_HOST=127.0.0.1
export DASH_SERVER_PORT=5200
dash-server
```

On macOS Monterey 12 and later, AirPlay Receiver can bind ports `5000` and
`7000` through system ControlCenter / AirPlay helper processes. `dash-server`
defaults to `5100` instead of Flask's classic `5000` to avoid that local macOS
collision. If you explicitly run `dash-server --port 5000` and startup fails,
either disable AirPlay Receiver in macOS sharing settings or choose another
`dash-server` port.

When you change the control-plane port, update all local URLs:

- Browser dashboards: `http://127.0.0.1:5200/apps/demo`
- MCP endpoint: `http://127.0.0.1:5200/mcp`
- MCP client bridge args such as `mcp-remote http://localhost:5200/mcp`

In isolated runtime mode, each hosted app worker also binds a loopback HTTP port
behind the proxy. By default the OS chooses free ephemeral worker ports. To
limit workers to an explicit range, set:

```bash
export DASH_SERVER_APP_RUNTIME_MODE=isolated
export DASH_SERVER_APP_WORKER_PORT_RANGE=5500-5599
```

Do not include `5000` or `7000` in `DASH_SERVER_APP_WORKER_PORT_RANGE` on Macs
where AirPlay Receiver is enabled.

## First Five Minutes

### 1. Check that the Exasol profile was bootstrapped

```bash
curl -s http://127.0.0.1:5100/mcp \
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

You should see `analytics-prod` in the returned profile list.

### 2. Open the demo dashboard

Visit:

- `http://127.0.0.1:5100/apps/demo`

That verifies the hosted-app runtime is working.

### 3. Confirm the MCP endpoint is up

```bash
curl -s http://127.0.0.1:5100/mcp
```

You should get an SSE-ready response body.

### 4. Initialize an MCP session

```bash
curl -s http://127.0.0.1:5100/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "curl", "version": "0"}
    }
  }'
```

### 5. Let an agent create an Exasol dashboard

Once the profile exists, the agent can go straight to dashboard creation with `profile_name: "analytics-prod"`.

### 6. List hosted apps

```bash
curl -s http://127.0.0.1:5100/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "apps_list",
      "arguments": {}
    }
  }'
```

### 7. Read the app inventory resource

```bash
curl -s http://127.0.0.1:5100/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "resources/read",
    "params": {
      "uri": "dash://apps"
    }
  }'
```

### 8. Ask the agent what is on your screen

With the dashboard open in a browser tab, an agent can read its live state directly. This
is the only way to see interaction state — Dash keeps current selections in the browser,
not on the server.

```bash
curl -s http://127.0.0.1:5100/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
      "name": "app_session_eval_js",
      "arguments": {"name": "demo", "code": "({page: ctx.page(), props: ctx.props()})"}
    }
  }'
```

If it reports `session_channel_session_gone`, no tab is open — open the dashboard first.
Read `dash://meta/session-channel-guide` for the full `ctx` helper reference and recipes,
including setting a filter and waiting for the app to settle in one call.

This is a **local-mode feature**: it is unavailable in hosted mode, and it is disabled if
the control plane is not bound to loopback. See
[Hosted Mode](hosted-mode.md#what-hosted-mode-does-not-have-the-browser-session-channel).

At that point you have verified:

- the Exasol bootstrap path
- the browser runtime
- the MCP control plane
- the browser session channel

## Isolated Instances

By default dash-server writes all of its state (SQLite registry, GitOps repo,
artifacts, workspaces, diagnostics, secrets) under `<project_root>/instance`. To
run two servers side-by-side — for parallel tests, side experiments, or staging a
clean state — pass `--instance-path` (or set `DASH_SERVER_INSTANCE_PATH`):

```bash
dash-server --port 5101 --instance-path /tmp/scratch-a/instance
dash-server --port 5102 --instance-path /tmp/scratch-b/instance
```

The two servers will have wholly separate registries, GitOps repos, and apps;
no cross-pollination.

If you need finer-grained control, every sub-root has its own env var that
overrides the per-instance derivation:

| Setting | Env var |
|---------|---------|
| Whole instance dir | `DASH_SERVER_INSTANCE_PATH` |
| SQLite registry | `DASH_SERVER_REGISTRY_DB_PATH` |
| Built artifacts | `DASH_SERVER_ARTIFACTS_ROOT` |
| Workspaces | `DASH_SERVER_WORKSPACES_ROOT` |
| Diagnostics JSONL | `DASH_SERVER_DIAGNOSTICS_ROOT` |
| Dependency state | `DASH_SERVER_DEPENDENCY_STATE_ROOT` |
| GitOps repo | `DASH_SERVER_GITOPS_REPO_PATH` |
| Exasol secrets | `DASH_SERVER_EXASOL_SECRETS_ROOT` |

## Typical User Workflow

1. Start the server locally or deploy it somewhere reachable, with at least one Exasol profile bootstrapped.
2. Connect Claude, ChatGPT, or another MCP client.
3. Ask the agent to create or update a dashboard against that profile.
4. Open the live URL in the browser.
5. Use diagnostics when validation, build, or runtime issues occur.

## Next Docs

- MCP client setup: [mcp-clients.md](mcp-clients.md)
- MCP tools and resources: [mcp-reference.md](mcp-reference.md)
- Exasol workflow: [exasol.md](exasol.md)
- Architecture: [architecture.md](architecture.md)
