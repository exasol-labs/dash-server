# Consumption Outputs

`dash-server` consumption workflows begin with registered outputs. An app declares which datasets
or views may leave the interactive dashboard in `dash-app.json`; callers cannot submit arbitrary
SQL, Python callables, filesystem paths, or external render URLs.

Phase 0 provides validated, revision-pinned discovery. Export execution, retained artifacts,
subscriptions, rendered snapshots, and alerts are intentionally not enabled yet.

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
`pptx`. These formats are discoverable in Phase 0 but will not execute until the rendered-snapshot
phase ships.

## Validation and revision identity

Manifest validation normalizes the contract, rejects unknown or unsafe fields, validates formats,
classification, limits, datasource aliases, parameter schemas, paths, and duplicate IDs. Workspace
validation verifies declared SQL files exist. A canonical SHA-256 contract hash is stored in every
revision manifest and checked during discovery.

The current classifications are `public`, `internal`, `confidential`, and `restricted`.

## Agent discovery

MCP exposes:

- `app_outputs_list {name}`
- `app_output_get {name, output_id}`
- `dash://apps/{name}/outputs`

The response includes the live revision, contract hash, parameter schema, declared and effective
formats, effective limits, server policy, and authorization decision. Phase 0 marks every output as
`executable: false`.

## User discovery

Authenticated users with `dashboard.export` access can open:

```text
/manage/apps/{name}/consumption
```

The page is read-only in Phase 0 and uses the same `ConsumptionService` as MCP.

## Authorization and policy

Viewer, editor, owner, and admin roles receive `dashboard.export`. App ACL scope still applies.
Output discovery is re-authorized against the live app. Public output behavior is disabled by
default even for a publicly visible dashboard.

Relevant configuration:

```text
DASH_SERVER_CONSUMPTION_ENABLED
DASH_SERVER_CONSUMPTION_ALLOWED_FORMATS
DASH_SERVER_CONSUMPTION_MAX_ROWS
DASH_SERVER_CONSUMPTION_MAX_BYTES
DASH_SERVER_CONSUMPTION_PUBLIC_EXPORTS_ENABLED
```

See [the implementation plan](../plans/consumption-workflows-plan.md) for later phases and the RLS,
durable scheduling, storage, and sandboxing gates.
