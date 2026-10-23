# Snowflake layout (written, not executed)

Locally the warehouse is one DuckDB file. This folder holds the same warehouse written for
Snowflake: databases, compute, roles, row-level security, raw loads and environments. **None of it
runs here.** There is no paid account. The SQL is idempotent and each file says which role runs it.

DuckDB schema names map one to one, so dbt models do not change between targets:

| Local DuckDB schema | Snowflake | Written by |
|---|---|---|
| `raw_admin_core`, `raw_legacy_admin`, `raw_network_feed`, `audit` | `RAW.ADMIN_CORE`, `RAW.LEGACY_ADMIN`, `RAW.NETWORK_FEED`, `RAW.AUDIT` | `LOADER` (ingest, CDC connector, COPY) |
| `staging`, `intermediate`, `seeds`, `snapshots` | `ANALYTICS.<same>` | `TRANSFORMER` (dbt) |
| `core`, `finance`, `claims`, `dealer`, `regulatory` | `ANALYTICS.<same>` | `TRANSFORMER`; read by `REPORTER` |

## Files, in run order

| File | Run as | What it does |
|---|---|---|
| `01_databases_schemas.sql` | SYSADMIN | `RAW` (managed-access schemas), `ANALYTICS`, `ANALYTICS.SECURITY`, transient `ANALYTICS_DEV` |
| `02_warehouses_resource_monitors.sql` | SYSADMIN, ACCOUNTADMIN | `LOADING` / `TRANSFORMING` / `REPORTING`, all X-Small with auto-suspend (60 s for ETL, 300 s for BI). One resource monitor per warehouse plus an account monitor: notify at 80%, suspend at 100% |
| `03_roles_grants.sql` | USERADMIN, SECURITYADMIN | `LOADER` / `TRANSFORMER` / `REPORTER`, key-pair service users, current and future grants. `REPORTER` reads marts only, never raw or staging |
| `04_row_access_policy.sql` | SYSADMIN, TRANSFORMER | `SECURITY.USER_DEALER_ACCESS` map and the `RAP_TENANT_DEALER` / `RAP_TENANT` policies |
| `05_load_network_feed.sql` | LOADER | Stage, file formats, raw feed tables with lineage columns, `COPY INTO` by column name, manifest view |
| `06_streams_tasks.sql` | LOADER | Optional: streams + a two-task graph that records feed arrivals and flags a late file |
| `07_environments_clone.sql` | SYSADMIN | Zero-copy clone for UAT, a cutover-rehearsal clone of `RAW`, Time Travel restore recipes |

## How the pieces fit

```
admin systems --CDC connector--> RAW.ADMIN_CORE / RAW.LEGACY_ADMIN --+
network files --PUT + COPY-----> RAW.NETWORK_FEED -------------------+--> dbt (TRANSFORMER, TRANSFORMING wh)
                                                                     |      -> ANALYTICS.STAGING .. marts
                                                                     |      -> post-hook: attach row policies
                                       REPORTER, REPORTING wh  <-----+   Power BI / report site / packets
```

- **Cost.** Three X-Small warehouses that start suspended. Quotas and auto-suspend are set in 02.
  dbt sets `query_tag: warranty_dbt` (see `dbt/profiles.yml`), so spend per workload can be read
  from `ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY`.
- **Security.** Service users use key pairs only (`TYPE = SERVICE`, no passwords). Private keys
  live in the orchestrator's secret store. `*.p8` files are git-ignored.
- **One access map.** `USER_DEALER_ACCESS` has the same shape as the semantic model's RLS table
  and the per-dealer packet filter. One table answers "who can see which tenant and dealer" for
  every reader.
- **Environments.** Prod is `ANALYTICS`. UAT is a zero-copy clone, `ANALYTICS_UAT`, with a
  release-candidate build on top. Dev is the transient `ANALYTICS_DEV`. To switch, set
  `SNOWFLAKE_DATABASE`.

## Running dbt against it

```
pip install dbt-snowflake~=1.10.0
set SNOWFLAKE_ACCOUNT=<account identifier>
set SNOWFLAKE_PRIVATE_KEY_PATH=<path to rsa_key.p8>
set SNOWFLAKE_DATABASE=ANALYTICS_DEV
python -m dbt.cli.main build --project-dir dbt --profiles-dir dbt --target snowflake
```

From Airflow, pass `{"target": "snowflake"}` as the run's params (`pipeline/airflow/dags/vsc_daily.py`).
CI only runs `dbt parse --target snowflake`, which never connects.

## Integration notes for the dbt project (owned by the Data Engineering Manager)

1. **Raw source database.** Raw tables live in database `RAW`, and schema names drop the `raw_`
   prefix. Make `sources.yml` target-aware. For example:

   ```yaml
   - name: raw_admin_core
     database: "{{ 'RAW' if target.type == 'snowflake' else target.database }}"
     schema: "{{ 'ADMIN_CORE' if target.type == 'snowflake' else 'raw_admin_core' }}"
   ```

2. **Row access policies survive rebuilds only if they are re-attached.** `CREATE OR REPLACE`
   drops an attached policy. Add a Snowflake-only post-hook on dealer-grain marts:

   ```sql
   {% if target.type == 'snowflake' %}
   alter table {{ this }} add row access policy ANALYTICS.SECURITY.RAP_TENANT_DEALER on (tenant_key, dealer_key)
   {% endif %}
   ```

   For the Postgres target, use one `on-run-end` hook instead:
   `select security.apply_row_policies()` (see `warehouse/postgres/rls.sql`).
3. **Portable SQL.** Use dbt cross-database macros such as `dbt.dateadd`, `dbt.datediff`,
   `dbt.safe_cast`, `dbt.hash` and `dbt.type_string`, not DuckDB-only functions. The feed's
   `payload` is JSON text in DuckDB and `VARIANT` in Snowflake, so read it through one macro.
   The Admin Core table `transaction` is a reserved word on every target, so quote it.
