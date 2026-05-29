# Exasol Workflow

`dash-server` is being optimized to make Exasol-backed dashboard delivery smooth for both users and agents.

The current implementation includes the first usable slice of that path, including:

- Exasol profile creation through MCP
- secret handling that stays outside Git
- connection validation
- generation of a live Exasol-backed dashboard scaffold

## What Exists Today

The current Exasol-specific surface includes:

- `exasol_profiles_list`
- `exasol_profile_create_local`
- `exasol_profile_validate`
- `app_create_exasol_dashboard`
- `app_scaffold_from_schema`
- `dash://exasol/help/connection-modes`
- `dash://exasol/profiles`
- `dash://exasol/profiles/{name}`

## Fastest Setup Path

The simplest way to use `dash-server` for Exasol is:

1. export the Exasol secret into the server environment
2. export one small set of `DASH_SERVER_EXASOL_*` bootstrap variables
3. start `dash-server`
4. let agents use the bootstrapped profile by name

Example:

```bash
export EXA_PASSWORD='your-secret-password'
export DASH_SERVER_EXASOL_PROFILE_NAME='analytics-prod'
export DASH_SERVER_EXASOL_DSN='localhost:8563'
export DASH_SERVER_EXASOL_USER='sys'
. .venv/bin/activate
dash-server
```

Optional bootstrap settings:

```bash
export DASH_SERVER_EXASOL_DESCRIPTION='Primary analytics database'
export DASH_SERVER_EXASOL_SECRET_ENV_VAR='EXA_PASSWORD'
export DASH_SERVER_EXASOL_CREDENTIAL_MODE='password'
export DASH_SERVER_EXASOL_BACKEND='onprem'
export DASH_SERVER_EXASOL_TLS_VERIFY='true'
export DASH_SERVER_EXASOL_STATEMENT_TIMEOUT_SECONDS='30'
export DASH_SERVER_EXASOL_ROW_LIMIT='50000'
```

When these are set, `dash-server` will create the profile automatically on startup if it does not already exist.

That avoids two manual steps:

- creating `profiles/exasol/{name}.json` by hand
- calling `exasol_profile_create_local` before the first dashboard can be generated

The generated Exasol dashboard scaffold includes:

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

The default scaffold is the stronger `exasol-analytics` layout:

- `metric-cards` means a generic Dash starter with static metric cards
- `exasol-analytics` means a profile-bound Exasol scaffold with SQL files, runtime helpers, and a multi-tab app structure

By default, `app_create_exasol_dashboard` now generates an `analytics-hub` app with:

- a `System Health` tab wired to `EXA_MONITOR_LAST_DAY` and `EXA_USAGE_LAST_DAY`
- a `Query History` tab wired to `EXA_SQL_LAST_DAY`
- a `Business Analytics` tab with placeholder SQL the agent can replace

If you already know which schema to target, `app_scaffold_from_schema` will inspect visible Exasol tables and columns, choose a likely analytical table, and seed the business SQL files from that schema.

## The Important Setup Rule

You do not put the Exasol password or token into Git.

`dash-server` splits Exasol connection setup into two parts:

- profile metadata goes into the GitOps repo
- the secret itself goes either into an environment variable or into the local secret store

That means the user needs to provide credentials through one of these two inputs when calling `exasol_profile_create_local`:

- `secret_value`
- `secret_env_var`

You must provide exactly one of them.

## Where Credentials Actually Go

### Profile metadata

When you create a profile, non-secret metadata is stored in the GitOps repo at:

- `instance/gitops-repo/profiles/exasol/{profile-name}.json`

That file contains fields such as:

- `name`
- `backend`
- `credential_mode`
- `dsn`
- `user`
- `tls_verify`
- `secret_ref`

It does not store the actual secret value.

### Secret values

The actual credential is resolved in one of two ways:

1. Environment variable mode
   - you pass `secret_env_var`
   - the server reads the credential from that environment variable at validation/runtime

2. Local secret file mode
   - you pass `secret_value`
   - the server stores it at:
     `instance/exasol-secrets/{profile-name}.json`

By default, the local secret directory is:

- `instance/exasol-secrets`

That path comes from `EXASOL_SECRETS_ROOT`, which defaults to `instance/exasol-secrets`.

## Required Connection Fields

For `exasol_profile_create_local`, you must pass:

- `name`
- `backend`
- `credential_mode`
- `dsn`
- `user`
- exactly one of `secret_value` or `secret_env_var`

Optional fields:

- `description`
- `tls_verify`
- `statement_timeout_seconds`
- `row_limit`

### About `tls_verify`

`tls_verify` controls **certificate verification only**. TLS is always negotiated for `local_direct` profiles.

- `tls_verify: true` (default) — verify the server cert chain. Use this for production or any cluster with a real CA-issued certificate.
- `tls_verify: false` — encrypt but skip cert validation. Use this for local development against a self-signed certificate, e.g. Exasol Community Edition, Exasol Nano, or a corporate dev box that issues its own cert.

Connecting to a self-signed Exasol with `tls_verify: true` fails with `[SSL: CERTIFICATE_VERIFY_FAILED]`. Setting it to `false` does not disable TLS — the websocket is still encrypted.

## Which Credential Modes Are Supported

Currently supported combinations are:

- `backend: "onprem"` with:
  - `credential_mode: "password"`
  - `credential_mode: "access_token"`
  - `credential_mode: "refresh_token"`
- `backend: "saas"` with:
  - `credential_mode: "saas_pat"`

At runtime, the connector maps those modes to `pyexasol.connect(...)` arguments like this:

- `password` -> `password=...`
- `access_token` -> `access_token=...`
- `refresh_token` -> `refresh_token=...`
- `saas_pat` -> `password=...`

## Recommended Setup Order

If you want Exasol to feel like part of the platform rather than something each agent or end user must configure manually, the best order is:

1. preconfigure the profile and secret on the server side
2. start `dash-server`
3. let agents use the existing profile by name

The MCP-based profile creation flow is still useful, but it should be treated as the second option, not the primary operator setup path.

## Option 1: Pure Server-Side Setup

This is the best fit for shared or multi-user environments.

In this mode:

- the server operator creates the Exasol profile metadata directly
- the server operator provides the secret directly to the server environment or local secret store
- agents and end users only need to know the profile name, not the actual credentials

### 1A. Startup bootstrap through environment variables

This is now the recommended server-side option because it is the shortest path from zero to a reusable Exasol profile.

Use the example in `Fastest Setup Path` above.

### 1B. Server-side metadata file

Create this file:

- `instance/gitops-repo/profiles/exasol/analytics-prod.json`

If this is a fresh local setup and `instance/gitops-repo` does not exist yet, either:

- start `dash-server` once so it initializes the repo structure
- or create the directory tree yourself before writing the file

Example:

```json
{
  "name": "analytics-prod",
  "backend": "onprem",
  "deployment_mode": "local_direct",
  "credential_mode": "password",
  "user": "sys",
  "dsn": "demodb.exasol.com:8563",
  "description": "Primary analytics database.",
  "tls_verify": true,
  "secret_ref": {
    "provider": "env",
    "key": "EXA_PASSWORD"
  },
  "query_defaults": {
    "statement_timeout_seconds": 30,
    "row_limit": 50000
  }
}
```

### Server-side secret via environment variable

Before starting `dash-server`, export the secret into the server process environment:

```bash
export EXA_PASSWORD='your-secret-password'
. .venv/bin/activate
dash-server
```

This is the cleanest server-side pattern because:

- the secret never goes into Git
- the agent never needs to know the password
- the profile can be reused across many generated dashboards

### Alternative server-side secret via local secret file

If you want the server itself to own the credential outside the environment, create:

- `instance/exasol-secrets/analytics-prod.json`

with contents:

```json
{
  "secret": "your-secret-password"
}
```

and set the profile metadata to:

```json
"secret_ref": {
  "provider": "local_file",
  "key": "analytics-prod"
}
```

### What the agent sees in this setup

The agent does not need the actual credential.

It only needs to know the profile name, for example:

- `analytics-prod`

Then it can generate a dashboard against that profile:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "app_create_exasol_dashboard",
      "arguments": {
        "name": "sales-overview",
        "profile_name": "analytics-prod",
        "title": "Sales Overview"
      }
    }
  }'
```

That is the model to prefer when the server is being used by multiple people or multiple agents.

You can still validate the preconfigured profile after startup with `exasol_profile_validate` or by reading `dash://exasol/profiles`.

## Option 2: Create the Profile Through MCP

This is still useful for local development or early experimentation.

There are two practical ways to give the server credentials through MCP.

### 2A. Use an environment variable

This is the cleaner MCP-based path if you do not want the secret written to the local secret store.

Set the variable before starting `dash-server`:

```bash
export EXA_PASSWORD='your-secret-password'
. .venv/bin/activate
dash-server
```

Then create the profile through MCP:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "exasol_profile_create_local",
      "arguments": {
        "name": "analytics-prod",
        "backend": "onprem",
        "credential_mode": "password",
        "dsn": "demodb.exasol.com:8563",
        "user": "sys",
        "secret_env_var": "EXA_PASSWORD",
        "tls_verify": true
      }
    }
  }'
```

What happens:

- `profiles/exasol/analytics-prod.json` is committed to the GitOps repo
- the profile stores `secret_ref.provider = "env"`
- the secret value stays in `EXA_PASSWORD`

### 2B. Pass the secret directly once

This is useful when you want the server to manage the local secret file for you.

Create the profile through MCP:

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "exasol_profile_create_local",
      "arguments": {
        "name": "analytics-prod",
        "backend": "onprem",
        "credential_mode": "password",
        "dsn": "demodb.exasol.com:8563",
        "user": "sys",
        "secret_value": "your-secret-password",
        "tls_verify": true
      }
    }
  }'
```

What happens:

- `profiles/exasol/analytics-prod.json` is committed to the GitOps repo
- the profile stores `secret_ref.provider = "local_file"`
- the secret is written to:
  `instance/exasol-secrets/analytics-prod.json`

## A Complete Happy-Path Example

### 1. Create the profile

```bash
export EXA_PASSWORD='your-secret-password'
```

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 10,
    "method": "tools/call",
    "params": {
      "name": "exasol_profile_create_local",
      "arguments": {
        "name": "analytics-prod",
        "backend": "onprem",
        "credential_mode": "password",
        "dsn": "demodb.exasol.com:8563",
        "user": "sys",
        "secret_env_var": "EXA_PASSWORD",
        "description": "Primary analytics database.",
        "tls_verify": true,
        "statement_timeout_seconds": 30,
        "row_limit": 50000
      }
    }
  }'
```

### 2. Validate the connection

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 11,
    "method": "tools/call",
    "params": {
      "name": "exasol_profile_validate",
      "arguments": {
        "name": "analytics-prod"
      }
    }
  }'
```

If this passes, the server was able to:

- resolve the secret
- import `pyexasol`
- connect using your `dsn`, `user`, and credential

### 3. Generate a dashboard scaffold

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 12,
    "method": "tools/call",
    "params": {
      "name": "app_create_exasol_dashboard",
      "arguments": {
        "name": "sales-overview",
        "profile_name": "analytics-prod",
        "title": "Sales Overview"
      }
    }
  }'
```

This creates a hosted Dash app that uses the saved Exasol profile.

## How the Generated Dashboard Uses the Profile

The scaffold writes the profile name into `dash-app.json`:

```json
{
  "data_sources": {
    "primary": {
      "kind": "exasol",
      "profile": "analytics-prod",
      "auth_mode": "local_direct"
    }
  }
}
```

At runtime:

1. the app reads the profile name from metadata
2. the helper calls the server-side Exasol runtime
3. the runtime resolves the secret using `secret_ref`
4. the query is executed with `pyexasol`

The generated app does not hardcode the password or token into source files.

## Helper Return Contract

The generated `dash_server_exasol.py` helper functions (`query_rows`,
`query_one`, `query_scalar`, and the per-app `load_rows` / `load_row`
wrappers) **never raise on data-layer failure**. They always return:

- on success: a list of row dicts (or a single dict for `query_one`, or a value
  for `query_scalar`).
- on failure: a single-row envelope `[{"_error": "<message>"}]` (or the dict
  `{"_error": "..."}` from `query_one`).

This shape is intentional — Dash callbacks shouldn't surface raw 500s for
upstream-data failures; they should render a friendly error panel. But the
shape also means **`row["AGENT_ID"]` will `KeyError` on the error envelope** if
you forget to check first.

The helper exposes `has_error(rows)` to make the check cheap and explicit:

```python
from dash_server_exasol import load_rows, has_error, render_error_panel

@app.callback(Output("agents", "children"), Input("refresh", "n_clicks"))
def render_agents(_):
    rows = load_rows(server, metadata, __file__, "queries/agents.sql")
    if has_error(rows):
        return render_error_panel(rows[0]["_error"])
    return [html.Tr([html.Td(r["AGENT_ID"]), html.Td(r["LATENCY_MS"])]) for r in rows]
```

The same envelope is what the `_record_data_layer_error` recorder fires on, so
treating it explicitly is what lights up `dash://apps/{name}/errors`, the
`data_layer` healthcheck probe, and `app_collect_diagnostics`. Skipping the
check (the way the persona-2 ML-engineer flow did) ends in a `KeyError` 500
that masquerades as a control-plane bug.

## Multi-User Recommendation

For a shared environment, the recommended pattern is:

1. an operator creates `profiles/exasol/{name}.json`
2. the operator provides the secret via environment variable or `instance/exasol-secrets/{name}.json`
3. agents only receive the profile name
4. agents use `app_create_exasol_dashboard` and normal app-editing tools

That keeps:

- credentials out of user prompts
- credentials out of Git
- Exasol setup out of the end-user workflow

## How to Inspect What Was Stored

### Read all profiles through MCP

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 20,
    "method": "resources/read",
    "params": {
      "uri": "dash://exasol/profiles"
    }
  }'
```

### Read one profile through MCP

```bash
curl -s http://127.0.0.1:5000/mcp \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 21,
    "method": "resources/read",
    "params": {
      "uri": "dash://exasol/profiles/analytics-prod"
    }
  }'
```

You will see the `secret_ref`, but not the secret itself.

## Common Mistakes

### Passing both `secret_value` and `secret_env_var`

This fails. You must provide exactly one.

### Passing neither `secret_value` nor `secret_env_var`

This also fails. The profile needs one secret source.

### Setting `secret_env_var` but not exporting the variable

Profile creation will work, but validation/runtime will fail because the server cannot resolve the variable.

### Expecting the secret to appear in the GitOps repo

It will not. Only the `secret_ref` is stored there.

### Forgetting that the server process needs the env var

If you use `secret_env_var`, the environment variable must be visible to the `dash-server` process itself, not just to your shell history or editor.

## Important Current Limits

The current implementation is intentionally narrow:

- local single-user profile modes only
- no hosted multi-user passthrough or impersonation flow yet
- no OS keychain integration yet
- no advanced schema/query discovery flow yet

The longer plan lives in [plans/exasol-dashboard-optimization-spec.md](../plans/exasol-dashboard-optimization-spec.md).
