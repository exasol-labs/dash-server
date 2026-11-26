# Semantic Scaffold Notes for SEMANTIC_SALES.SALES

Built on the published semantic model `sales` (readiness: VALID). The queries in `queries/business/` are Semantic SQL: metrics are governed definitions, already aggregated, so they are selected bare and grouped by dimensions.

- Metrics: `total_revenue` (Total Revenue, currency, certified), `gross_margin_pct` (Gross Margin %, percentage, certified)
- Time dimension: `order_month` (Order Month, month, certified)
- Category dimension: `customer_region` (Customer Region, certified)

Rules:

- The profile's sessions must run Semantic SQL: `exasol_profile_validate` reports `preprocessor_active` for the model. If it is false and no preprocessor is set database-wide (`semantic_layer.database_preprocessor` is null), add `session_init_sql` `["ALTER SESSION SET SQL_PREPROCESSOR_SCRIPT = SEMANTIC_ADMIN.SEMANTIC_PREPROCESSOR"]`. With a database-wide preprocessor, never set it in `session_init_sql`; follow the model's `setup` text.
- Only combine a metric with a dimension listed for it in `SEMANTIC_AGENT.VALID_COMBINATIONS_FOR_AGENT`.
- Never paste `COMPILE_SQL` / `COMPILE_REQUEST_JSON` output into `queries/*.sql`: it freezes today's definition.
- `app.py` reads labels, formats (`FORMAT_HINT`, `UNIT_HINT`; a percentage is a fraction), and certification from `SEMANTIC_AGENT.FIELDS_FOR_AGENT` at runtime (`queries/business/fields.sql`), and shows the model's last finished validation (`queries/business/provenance.sql`). Keep query columns unaliased so they match `SQL_COLUMN_NAME`; see `dash://exasol/help/semantic-layer`.
