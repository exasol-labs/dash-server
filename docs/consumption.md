# Consumption Outputs

`dash-server` consumption workflows begin with registered outputs. An app declares which datasets
or views may leave the interactive dashboard in `dash-app.json`; callers cannot submit arbitrary
SQL, Python callables, filesystem paths, or external render URLs.

Phase 1 provides validated, revision-pinned discovery plus asynchronous, on-demand CSV exports.
XLSX, subscriptions, rendered snapshots, alerts, broadcast delivery, and durable multi-node workers
remain disabled.

## Declaring a dataset output

```json
{
  "data_sources": {
    "primary": {"kind": "exasol", "profile": "analytics-prod"}
  },
  "consumption": {
    "outputs": [
      {
        "id": "monthly-close-detail",
        "title": "Monthly close detail",
        "kind": "dataset",
        "source": {
          "type": "exasol_sql",
          "data_source": "primary",
          "path": "queries/monthly_close_export.sql"
        },
        "parameters": {
          "type": "object",
          "properties": {
            "period": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}
          },
          "required": ["period"],
          "additionalProperties": false
        },
        "formats": ["csv", "xlsx"],
        "classification": "confidential",
        "limits": {"max_rows": 100000, "max_bytes": 52428800},
        "allow_subscriptions": true,
        "allow_alerts": false
      }
    ]
  }
}
```

Dataset outputs currently support only an `exasol_sql` source. The source must reference a declared
Exasol datasource alias and an existing workspace-relative `queries/*.sql` file. Parameters use a
restricted scalar JSON Schema subset and are intended for bound values only.

View declarations use `kind: "view"`, a same-app `dash_route`, and one or more of `pdf`, `png`, or
`pptx`. These formats remain discoverable but will not execute until the rendered-snapshot phase.

## Validation and revision identity

Manifest validation normalizes the contract, rejects unknown or unsafe fields, validates formats,
classification, limits, datasource aliases, parameter schemas, paths, and duplicate IDs. Workspace
validation verifies declared SQL files exist. A canonical SHA-256 contract hash is stored in every
revision manifest and checked during discovery.

Before the first export for an output/revision, executable preflight resolves the Exasol profile and
runs the existing safe SQL smoke check. Parameterized export SQL must provide non-secret smoke values
in `queries/sql_smoke.json`, for example:

```json
{"queries/monthly_close_export.sql": {"period": "2026-07"}}
```

The current classifications are `public`, `internal`, `confidential`, and `restricted`.

## Agent discovery

MCP exposes:

- `app_outputs_list {name}`
- `app_output_get {name, output_id}`
- `dash://apps/{name}/outputs`
- `app_export_create {name, output_id, format: "csv", parameters, idempotency_key?}`
- `app_exports_list {name}`
- `export_get {job_id}`
- `export_cancel {job_id}`
- `export_download_link_create {job_id}`
- `dash://exports/{job_id}`

The response includes the live revision, contract hash, parameter schema, declared and effective
formats, per-format execution availability, effective limits, server policy, and authorization
decision. MCP returns bounded artifact metadata and authenticated download URLs, never artifact
bytes.

## User discovery

Authenticated users with `dashboard.export` access can open:

```text
/manage/apps/{name}/consumption
```

The page generates parameter fields from the registered schema, creates CSV jobs with CSRF and
idempotency protection, shows the caller's recent exports, and provides progress, cancellation, and
download pages. Hosted dashboard chrome links to this page when the live revision declares outputs.
The UI uses the same `ConsumptionService` as MCP.

## Execution and artifact behavior

- Every job pins the live revision number, normalized output declaration, contract hash, policy
  version, effective limits, principal, and canonical parameter hash before enqueueing.
- Parameters are schema-validated, encrypted at rest, redacted from API/audit output, and bound
  through pyexasol. Arbitrary SQL and undeclared parameters are rejected.
- Export queries use a dedicated uncached connection and bounded `fetchmany`/`fetchone` batches;
  the interactive `fetchall()` path is not used.
- CSV neutralizes spreadsheet-formula prefixes, enforces row/byte/runtime limits, and publishes with
  an atomic rename only after completion. Failed and cancelled jobs publish no partial artifact.
- Local artifacts expire after the configured TTL. Cleanup runs during export/list/download
  activity and marks the corresponding job expired.
- Download tokens are short-lived, purpose-bound, and principal-bound. Download re-checks app
  authorization and returns `Cache-Control: private, no-store`.

The Phase 1 coordinator is deliberately single-process and local. Do not run multiple control-plane
processes against it; shared queues, object storage, and HA coordination are Phase 6 work.

## Authorization and policy

Viewer, editor, owner, and admin roles receive `dashboard.export`. App ACL scope still applies.
Output discovery is re-authorized against the live app. Public output behavior is disabled by
default even for a publicly visible dashboard.

Relevant configuration:

```text
DASH_SERVER_CONSUMPTION_ENABLED
DASH_SERVER_CONSUMPTION_EXPORTS_ENABLED
DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS
DASH_SERVER_CONSUMPTION_MAX_ROWS
DASH_SERVER_CONSUMPTION_MAX_BYTES
DASH_SERVER_CONSUMPTION_MAX_RUNTIME_SECONDS
DASH_SERVER_CONSUMPTION_ARTIFACT_TTL_SECONDS
DASH_SERVER_CONSUMPTION_DOWNLOAD_TOKEN_TTL_SECONDS
DASH_SERVER_CONSUMPTION_FETCH_BATCH_SIZE
DASH_SERVER_CONSUMPTION_MAX_CONCURRENT_JOBS
DASH_SERVER_CONSUMPTION_PARAMETER_KEY
DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED
```

Discovery defaults on. Export execution defaults off and must be enabled explicitly with
`DASH_SERVER_CONSUMPTION_EXPORTS_ENABLED=true`. Anonymous export creation and download are not
supported in Phase 1, even when public output discovery is enabled.

See [the implementation plan](../plans/consumption-workflows-plan.md) for later phases and the RLS,
durable scheduling, storage, and sandboxing gates.
