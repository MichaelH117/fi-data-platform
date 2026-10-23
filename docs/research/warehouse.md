# Warehouse / Storage Design: Research Spike (2026-10-22)

## Options compared

| Option | Pros | Cons | Fit |
|---|---|---|---|
| **A. SQL Server -> Postgres mirror** (mirrors the real setup) | Shows you know the real-world hop; both engines are free in Docker (SQL Server 2022 Developer, `mcr.microsoft.com/mssql/server:2022-latest`) | Two engines to run (SQL Server needs about 2 GB RAM). Real CDC (Debezium + Kafka) adds heavy infrastructure; `tds_fdw` reads SQL Server live and doesn't keep a copy | Good, if the hop is kept **simple** (a batch Python/dbt copy, not Kafka) |
| **B. Postgres-only medallion + dbt** | One engine, free, industry-standard dbt layering, tests, docs, lineage | Doesn't mirror the SQL Server step | Strong core |
| **C. DuckDB** | No server, very fast, great for CI/tests | Single-writer file, so it's awkward as a shared "server" for daily stakeholder refresh | Use for unit tests/CI only |
| **D. Snowflake trial** | Recognizable on a resume | 30 days / $400 credits, then it expires, so the repo stops working | Optional dbt target, not the core |

## RECOMMENDATION

**A hybrid of A and B, all in `docker compose up`:**

1. **Source / "raw data site" (SQL Server 2022 Developer):** a mock TPA admin database (stand-in for a TPA admin system). A Python generator writes contracts, claims and cancellations, with deliberate messiness (dupes, bad VINs, null dealer codes) for the pipeline to clean.
2. **Ingestion (Python + SQLAlchemy/pyodbc):** incremental extract by `updated_at` watermark into **Postgres `bronze`** (raw, append-only, with `_loaded_at` and `_source_file`). Explain in the README that this replaces Debezium for simplicity.
3. **Transform (dbt Core + dbt-postgres, Apache 2.0):** `bronze` (sources) -> `silver` (`stg_`/`int_`: typed, deduped, conformed) -> `gold` (`dim_`/`fct_` marts).
4. **Orchestration:** **Dagster** (asset lineage UI looks great in screenshots), or cron/APScheduler if you want it smaller. Runs daily: extract -> dbt build -> tests -> refresh BI.
5. **BI:** **Metabase** or **Apache Superset** containers (free) pointed at `gold`; Power BI Desktop can connect to the same Postgres for a resume screenshot.
6. **CI:** GitHub Actions running `dbt build` against DuckDB or a Postgres service container.

## Star schema (gold)

**Facts**
- `fct_contract_sale` (grain: one contract): contract_key, dealer_key, agent_key, product_key, vehicle_key, sale_date_key, effective_date_key, term_months, term_miles, retail_price, dealer_cost, reserve_amount, admin_fee
- `fct_claim` (grain: one claim line): claim_key, contract_key, dealer_key, product_key, loss_date_key, paid_date_key, repair_category, claim_amount_requested, claim_amount_paid, status
- `fct_cancellation` (grain: one cancellation): contract_key, cancel_date_key, cancel_reason, refund_amount, pro_rata_pct, chargeback_amount
- `fct_contract_monthly_snapshot` (periodic snapshot: contract x month): earned premium, incurred claims, which gives you the **loss ratio = incurred / earned**, by dealer, agent, product and cohort

**Dimensions:** `dim_dealer`, `dim_agent`, `dim_product` (coverage tier, term, deductible), `dim_vehicle` (make/model/year/mileage band), `dim_date` (role-playing: sale/effective/loss/paid/cancel), `dim_customer` (minimal, PII hashed).

## SCD handling
- **Type 2** for `dim_dealer` (address, status, agent assignment), `dim_agent` (territory) and `dim_product` (pricing), using **dbt snapshots** (`timestamp` strategy on `updated_at`, `check` strategy where there is no timestamp). Facts join on the surrogate key that was valid at the sale date.
- **Type 1** for typo corrections; **Type 0** for `dim_date`.

## Data quality
- **dbt tests (primary):** `unique`/`not_null` on keys, `relationships` (fact -> dim), `accepted_values` (claim status, cancel reason), plus `dbt-expectations`/`dbt_utils` for ranges (claim_paid >= 0, valid VIN length 17, cancel_date >= sale_date).
- **dbt source freshness** on bronze for the daily SLA.
- **Soda Core / Great Expectations:** optional. Soda Core reportedly moved to Elastic License 2.0 with Soda 4.0, and GX moved under Fivetran stewardship in 2026. dbt tests cover the need with fewer moving parts. If you want a "Python DQ" bullet, add GX checks only at the **bronze ingestion** step.
- Write test failures to an `audit.dq_results` table and show a "Data Health" dashboard tab.

## Tradeoffs
- Keeping SQL Server costs RAM and complexity, but it tells the "real enterprise" story. Make it a compose profile so `--profile lite` runs Postgres-only.
- Batch watermark vs CDC: simpler and testable, but it misses hard deletes. Handle those with soft-delete flags in the generator.

## Sources
- [Dagster + dbt + Postgres example (my-dbt-dagster)](https://github.com/jonathanneo/my-dbt-dagster)
- [DAD stack: Dagster-Airbyte-dbt](https://airbyte.com/quickstart/the-dad-stack)
- [SQL Server -> Postgres CDC with Debezium/Kafka](https://ourcodeworld.com/articles/read/3807/real-time-data-migration-from-mssql-to-postgresql-using-cdc-and-kafka)
- [AWS: tds_fdw SQL Server -> PostgreSQL](https://aws.amazon.com/blogs/database/use-the-tds_fdw-extension-to-migrate-data-from-sql-server-to-postgresql/)
- [dbt snapshots / SCD2 (Paradime)](https://docs.paradime.io/guides/dbt-fundamentals/model-materializations/snapshots)
- [dbt SCD Type 2 (Hevo)](https://hevodata.com/data-transformation/dbt-scd-type-2/)
- [Soda vs Great Expectations (Feb 2026)](https://proxy.dataexpert.io/blog/soda-vs-great-expectations-data-quality-tools)
- [Data Contract CLI tool comparison (licensing notes)](https://docs.datacontract.com/comparison)
- [Snowflake trial signup](https://signup.snowflake.com)
- [SQL Server 2022 Developer in Docker (MS Q&A)](https://learn.microsoft.com/en-us/answers/a/630214)
