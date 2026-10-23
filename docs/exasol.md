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
- `app_create_from_files` (with `template=exasol-analytics` — the recommended creation path once the dashboard is designed; not Exasol-only, but it injects `dash_server_exasol.py`)
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
export DASH_SERVER_EXASOL_TLS_VERIFY='false'   # unset: off for loopback DSNs, on otherwise
export DASH_SERVER_EXASOL_STATEMENT_TIMEOUT_SECONDS='30'
export DASH_SERVER_EXASOL_ROW_LIMIT='50000'
export DASH_SERVER_EXASOL_SECRETS_ROOT='/path/to/exasol-secrets'   # default: <instance>/exasol-secrets
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `DASH_SERVER_EXASOL_PROFILE_NAME` | unset (no bootstrap) | Profile to create at startup. Setting it enables the bootstrap. |
| `DASH_SERVER_EXASOL_DSN` | required | pyexasol DSN, e.g. `localhost:8563`. |
| `DASH_SERVER_EXASOL_USER` | required | Database user. |
| `DASH_SERVER_EXASOL_SECRET_ENV_VAR` | `EXA_PASSWORD` | Name of the env var holding the secret. The secret itself is never written to disk. |
| `DASH_SERVER_EXASOL_CREDENTIAL_MODE` | `password` | Credential type for the secret. |
| `DASH_SERVER_EXASOL_BACKEND` | `onprem` | Exasol backend kind. |
| `DASH_SERVER_EXASOL_TLS_VERIFY` | derived from DSN | Certificate verification; see [About `tls_verify`](#about-tls_verify). |
| `DASH_SERVER_EXASOL_DESCRIPTION` | `Local Exasol profile <name>.` | Human-readable description. |
| `DASH_SERVER_EXASOL_STATEMENT_TIMEOUT_SECONDS` | `30` | Per-statement timeout. |
| `DASH_SERVER_EXASOL_ROW_LIMIT` | `50000` | Maximum rows returned per query. |
| `DASH_SERVER_EXASOL_SESSION_INIT_SQL` | unset | `;`-separated session statements run on every new connection; see [Session setup statements](#session-setup-statements). |
| `DASH_SERVER_EXASOL_SECRETS_ROOT` | `<instance>/exasol-secrets` | Where secrets passed as `secret_value` are stored. |
| `DASH_SERVER_EXASOL_OVERWRITE` | `false` | Rewrite an existing profile when it differs from these settings (see below). |

When these are set, `dash-server` will create the profile automatically on startup if it does not already exist.

If the profile **already exists**, it is left unchanged by default, so a restart can't revert
changes an agent made with `exasol_profile_create_local`. When the stored profile differs from
the `DASH_SERVER_EXASOL_*` settings, startup logs a warning listing each differing field. To
apply the settings, either:

- set `DASH_SERVER_EXASOL_OVERWRITE=true` and restart, or
- call `exasol_profile_create_local` with `overwrite=true` and the changed values; settings you
  leave out, including the secret, keep their current values.

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

When the schema is unfamiliar, `app_scaffold_from_schema` will inspect visible Exasol tables and columns, choose a likely analytical table, and seed the business SQL files from that schema. Treat it as an exploration tool: it emits 12 files, and once you know what the dashboard should be, `app_create_from_files` with `template=exasol-analytics` is the shorter path — it takes your own `app.py` and `queries/*.sql` and still injects `dash_server_exasol.py`.

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
- `session_init_sql` (see [Session setup statements](#session-setup-statements))

### About `tls_verify`

`tls_verify` controls **certificate verification only**. TLS is always negotiated for `local_direct` profiles.

- `tls_verify: true` — verify the server cert chain. Use this for production or any cluster with a real CA-issued certificate.
- `tls_verify: false` — encrypt but skip cert validation. Use this for a self-signed certificate, e.g. Exasol Personal, Exasol Community Edition, Exasol Nano, or a corporate dev box that issues its own cert.

When `tls_verify` is not given (the `DASH_SERVER_EXASOL_TLS_VERIFY` bootstrap variable is unset,
or `exasol_profile_create_local` is called without it), the default depends on the DSN:

- every host is loopback (`localhost` or `127.x.x.x`) → `false`. Loopback traffic never leaves the machine, so verification protects nothing, and local installs ship self-signed certificates.
- any other host → `true`. A remote instance with a self-signed certificate needs an explicit `false`.

An explicit value always wins. Changing the default does not rewrite existing profiles.

### Pinning a self-signed certificate

For a remote instance with a self-signed certificate, pinning is safer than `tls_verify: false`:
the connection is accepted only if the server presents exactly that certificate. Put the
certificate's SHA-256 fingerprint in the DSN, **before** the port:

```bash
# fingerprint of the certificate the server presents
echo | openssl s_client -connect db.example.com:8563 2>/dev/null \
  | openssl x509 -outform DER | shasum -a 256 | cut -d' ' -f1 | tr a-f A-F
```

```bash
export DASH_SERVER_EXASOL_DSN='db.example.com/<FINGERPRINT>:8563'
```

With a pinned DSN, pyexasol checks the pin instead of the CA chain, and `tls_verify` has no
effect. If the certificate is rotated, validation fails with `tls_fingerprint_mismatch`.
The error reports the server's new fingerprint, which you should check before updating the DSN.
Put the fingerprint before the port: pyexasol 2.4.1 and later reject `host:port/FINGERPRINT`,
and `exasol_profile_create_local` rejects it too, with an explanation. IPv6 literals are not
supported in DSNs.

Connecting to a self-signed Exasol with `tls_verify: true` fails with `[SSL: CERTIFICATE_VERIFY_FAILED]`. Setting it to `false` does not disable TLS — the websocket is still encrypted.

### Session setup statements

Some Exasol features are switched on per session rather than per query. `session_init_sql` lists
statements that run on **every** connection the profile opens, before it is used: cached
dashboard connections, SQL smoke checks during builds, exports, and `exasol_profile_validate`.
It works like a BI tool's "initial SQL".

The main use is [Exasol Semantic Views](https://github.com/exasol-labs/exasol-semantic-views):
Semantic SQL against a published model only works once the session has its preprocessor
activated. The `dash://exasol/help/semantic-layer` resource has the full recipe for dashboards on
a semantic model: finding published models, query style, grants for a non-SYS profile user,
governed labels from `FIELDS_FOR_AGENT`, and why compiled SQL must never be saved into
`queries/*.sql`.

```json
"session_init_sql": [
  "ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"
]
```

or, for the startup bootstrap:

```bash
export DASH_SERVER_EXASOL_SESSION_INIT_SQL='ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR'
```

Rules:

- One statement per entry, at most 10. Only `ALTER SESSION`, `EXECUTE SCRIPT`, and
  `OPEN SCHEMA` are accepted, since the statements run on every connection.
- If a statement fails, the connection is closed and the query (or build check) fails with
  `exasol_session_init_failed`, naming the statement. `exasol_profile_validate` reports the
  outcome under `session_init` (`not_configured`, `applied`, or `failed`).
- Changing a profile's settings (for example with `exasol_profile_create_local` and
  `overwrite: true`) replaces cached connections on their next use.
- Don't put session setup in a hosted app's `queries/*.sql`. A statement without a result set
  returns no rows, and dash-server then discards the connection so one app's session change
  can't leak into other apps sharing the profile.

With the preprocessor active, the SQL smoke check (during builds and in `app_run_healthcheck`)
validates Semantic SQL exactly as it runs, so a dropped metric fails the next healthcheck
instead of waiting for a viewer. (A query Semantic Views refuses only inside the check's nested
`WHERE 1=0` probe, such as a renamed metric's old name in an ungrouped select, is checked through
its `COMPILE_SQL` output instead and passes with a warning.) A query file that reads a published semantic model
on a profile *without* the preprocessor fails the smoke check with the `session_init_sql` line
to add. `app_run_healthcheck` also reports each model the app reads in a `semantic_models`
probe: it fails when a model is `INVALID`, `PRECONDITION`, or `NOT_PUBLISHED`, and lists models
with validation warnings. Each model that isn't `VALID` carries the rule codes and messages behind
its status (`issues`). Readiness comes from the model's newest *finished* validation run, read
from `SEMANTIC_SOURCE` (granted with `SEMANTIC_USER`), so a run left unfinished doesn't make an
invalid model read as `VALID`. Without that access such a model is reported as `UNKNOWN`.

The probe also covers query files that bypass the model:
- **Saved compiled SQL** (`COMPILE_SQL` / `COMPILE_REQUEST_JSON` output) fails the probe
  (`details.frozen_files`), which degrades the app's health: it keeps the definitions of the day
  it was compiled.
- **Hand-written SQL over a model's source tables** is listed under
  `details.raw_governed_reads` without failing.

`dash://exasol/semantic-usage` lists both kinds under `bypasses`, so a metric owner sees the
dashboards a definition change won't reach.

For a **fused** model (partitions, alternate sources, reconciled attributes), the probe reports
the partitions' coverage windows and how each reconciled attribute is answered
(`details.fusion`, from Semantic Views' read-only `CHECK_FUSION_HEALTH`). It also compares both
with the build: a moved partition boundary, or more keys filled from another source or left
unresolved, is listed under `details.fusion_changed_since_build`. These change the numbers
without changing any definition, so they don't fail the probe; `app_run_healthcheck`'s text lists
them for review. A definition hash that moved together with the fusion layout is marked
`fusion_layout_changed`: a boundary move changes the hash of every field on the entity.

A query file may call `EXECUTE SCRIPT SEMANTIC_ADMIN.CHECK_FUSION_HEALTH(...)` to show those counts
to viewers; SQL smoke runs it as written. SQL smoke skips other `EXECUTE SCRIPT` files, which may
change data, including `CHECK_PARTITION_COVERAGE`: on Semantic Views 0.5.0 it records an
unfinished validation run on every call, so don't poll it from a dashboard.

## pyexasol Telemetry

pyexasol 3 sends an anonymized usage event to Exasol on every connection. dash-server
disables this by setting `EXASOL_TELEMETRY_DISABLE=1` before it loads pyexasol, in every
process that opens a connection. If the variable is already set, dash-server leaves it
alone. pyexasol treats any value as "disabled", so telemetry cannot be turned back on through
this variable.

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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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

The message is written for viewers: the database's own error message plus an
`(error ref …)` suffix. The DSN, database user, session id and SQL text are
left out; the full error is recorded on `dash://apps/{name}/errors`, where
`details.error_ref` matches the reference.

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

Row values are Python-typed:

| Exasol column | Python value |
|---|---|
| `DECIMAL(p,0)` | `int` |
| `DECIMAL(p,s)` | `decimal.Decimal` |
| `DATE` | `datetime.date` |
| `TIMESTAMP`, `TIMESTAMP WITH LOCAL TIME ZONE` | `datetime.datetime` (naive, in the session time zone) |
| `DOUBLE` | `float` |
| `BOOLEAN` | `bool` |
| `VARCHAR`, `CHAR`, `INTERVAL`, `GEOMETRY`, `HASHTYPE` | `str` |

Numbers with more than 15 significant digits stay `str`, because a browser stores numbers as
doubles and would round them on screen. Values in a non-ISO session date format also stay `str`.
Dash and plotly serialize `Decimal` and dates natively, so rows can go straight into figures and
tables. Format them with `strftime()` / format specs rather than string slicing, and convert a
`Decimal` with `float()` before mixing it into float arithmetic.

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
curl -s http://127.0.0.1:5100/mcp \
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
curl -s http://127.0.0.1:5100/mcp \
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
