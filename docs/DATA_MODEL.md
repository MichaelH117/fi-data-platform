# Data model

Owner: Director. How the dbt project in `dbt/` turns the five raw sources into tested marts. The binding column
contracts are in [V3_DESIGN.md](V3_DESIGN.md) section 2; the run order and recovery commands are in
[ARCHITECTURE.md](ARCHITECTURE.md). **All data is synthetic.**

```powershell
.venv\Scripts\dbt build --project-dir dbt --profiles-dir dbt                 # seeds, models, snapshots, tests
.venv\Scripts\dbt source freshness --project-dir dbt --profiles-dir dbt
```

dbt always runs from the repo root. `dbt/profiles.yml` reads the DuckDB file from `WAREHOUSE_PATH` (default
`data/warehouse/warehouse.duckdb`); `python -m pipeline.run` sets it for every step. No dbt packages are used, so
`dbt deps` and network access are never needed.

## Layers

| Layer | Schema | Folder | Materialized | Rule |
|---|---|---|---|---|
| raw | `raw_admin_core`, `raw_legacy_admin`, `raw_network_feed`, `raw_crm`, `raw_admin_api` | written by `python -m ingest` | append-only tables | Every row version, untouched, plus the audit columns below |
| staging | `staging` | `dbt/models/staging/<source>/` | view | `stg_<source>__<entity>`: 1:1 with raw; rename, cast, dedupe, hash PII |
| intermediate | `intermediate` | `dbt/models/intermediate/` | view, or table when heavy | `int_<entity>_<verb>`: key bridges, conforming, scope, earning, posting periods |
| snapshots | `snapshots` | `dbt/snapshots/` | snapshot | `snap_<entity>`: daily SCD2 |
| seeds | `seeds` | `dbt/seeds/<area>/` | seed | Counting rules, exclusions, bands, tolerances |
| marts | `core`, `finance`, `claims`, `dealer`, `regulatory` | `dbt/models/marts/<domain>/` | table | `dim_` / `fct_` in `core`; `mart_` per domain. The only layer consumers read |

### Raw audit columns

| Column | Meaning |
|---|---|
| `_loaded_at` | When ingest appended the row |
| `_loaded_by` | `ingest@<run_id>` |
| `_source_system` | `admin_core`, `legacy_admin`, `network_feed`, `crm` or `admin_api` |
| `_source_version` | Admin Core `row_version`; legacy epoch seconds of `updated_at`; feed and intake drop sequence |
| `_is_deleted` | Soft delete (`removed_from_source = 1`) or a legacy tombstone from the key diff |
| `_run_id` | The pipeline run that loaded it |
| `_source_file`, `_file_sha256`, `_row_number`, `_feed_date` | Feed and intake lineage, back to the delivered file and row |

Quarantine tables (`<raw schema>._quarantine`) add `reject_reason` and `raw_json`.

## Staging: what gets cleaned

| Rule | Where |
|---|---|
| Keep only the latest `_source_version` per key; carry `_is_deleted` as `is_deleted` | every `stg_` model (macros in `dbt/macros/staging.sql`) |
| Drop rows that ingest quarantined: anti-join `_quarantine` on (`_source_file`, `_row_number`) | feed and intake staging (macros `feed_accepted`, `intake_accepted`) |
| Hash PII with `sha256` of the normalized value (`customer_key`, `vin_hash`, `chassis_vin_hash`, `phone_hash`) or drop it. **No PII past staging**; the one exception is the pending-contracts mart, flagged `contains_pii: true` | `stg_admin_core__contract`, `stg_legacy_admin__contracts` |
| Flag bad VINs (`is_valid_vin`), trim and upper-case codes | contract and dealer staging |
| Flip the legacy claim sign (stored negative) so paid is always >= 0, flagged `is_amount_sign_fixed`; raw keeps the delivered sign | `stg_legacy_admin__claims` |

## Intermediate: the shared business rules

| Model | Rule |
|---|---|
| `int_contract_key_bridge` | Legacy `legacy_contract_id` or Admin Core `contract_id` to one conformed `contract_key` (`md5` of `<source>\|<id>`), plus `source_system` |
| `int_claim_number_bridge` | Claim numbers normalized with `ltrim(claim_no, '0')`, mapped to `claim_key` and `contract_key`, with `has_contract_key` |
| `int_reportable_dealers`, `int_reportable_scope` | Demo dealers and the test contract range (`seeds/core/demo_exclusions.csv`) are excluded **once**, here (`is_reportable`). Every mart reads through it |
| `int_contracts_conformed`, `int_claims_conformed`, `int_claim_lines_conformed`, `int_cancellations_conformed` | Both systems in one key space and one status vocabulary; every legacy contract gets an obligor (`obligor_basis`) |
| `int_transactions_posted`, `int_posting_days`, `int_posting_periods` | Posting-period key and the period lock (below) |
| `int_disbursements_split`, `int_contract_months`, `int_disbursements_earned` | Dealer cost split per account holder; earned % by month (pro-rata default, rule of 78 via var `earning_method`) |
| `int_remittance_lifecycle` | Remittance batch entered, sent, received; remit age and late fee inputs |
| `int_status_history_backfill` | Contract and claim status history from before the first snapshot |
| `int_call_contract_bridge`, `int_dealer_roster_drift` | CRM calls to contracts; admin-API dealer roster against `dim_dealer` |
| `int_as_of_date` | One reporting date: var `as_of_date`, or else the latest source activity across both contract systems, so a rerun on the same data gives the same numbers |

## Core marts (`core` schema)

The old giant contract dimension is split six ways, all at contract grain and keyed by `contract_key`:
`dim_contract` (status, dates, dealer, product, tenant, obligors), `dim_contract_terms`, `dim_contract_finance`,
`dim_contract_vehicle` (hashed VINs only), `dim_contract_actuarial_class` and `dim_contract_claim_summary`. The
other dimensions are `dim_date`, `dim_posting_period`, `dim_tenant` (white-label branding), `dim_dealer`,
`dim_dealer_group`, `dim_agent`, `dim_product`, `dim_account_holder`, `dim_obligor`, `dim_insurer`,
`dim_participation_entity`, `dim_claim` and `dim_user_dealer_access` (the one user-to-dealer-and-tenant map that RLS,
packets and the portal share).

| Fact | Grain |
|---|---|
| `fct_transaction` | one row per transaction (SALE, CANCEL, REINSTATE, AMEND, CASH) |
| `fct_disbursement` | transaction x disbursement type x account holder |
| `fct_earned_disbursement` | contract x disbursement type x posting period |
| `fct_gross_premium` | contract x posting period of sale or amendment |
| `fct_cancellation` / `fct_reinstatement` | cancel / reinstatement |
| `fct_claim_payment_line` | **the only claims fact**: one row per payment line |
| `fct_claim_history` | claim status change |
| `fct_cash_transaction` | cash line, with the remittance lifecycle dates and late fee |
| `fct_commission` | commission |
| `fct_contract_monthly` | contract x month (earned premium, incurred, exposure years), kept for actuarial |
| `fct_call_center_contact` | CRM call |

Every claims roll-up (for example `mart_claim_summary`) is derived from `fct_claim_payment_line`. Nobody builds a
second claims fact.

### Posting-period key

`posting_period_key` is an `INTEGER yyyymm` and a foreign key to `dim_posting_period`. It is the month of the
accounting **post date**, not the entered or effective date (those stay as `*_date_key`, `yyyymmdd`). A period
closes on business day 5 of the next month; after that, a late event posts to the first open period and keeps its
true event date, so a closed month is never restated. Every core fact also carries `posting_period_start_date`.

## Domain marts

| Schema | Marts |
|---|---|
| `finance` | `mart_pnl_by_product`, `mart_profit_share_statement`, `mart_commission_statement`, `mart_cession_experience`, `mart_reserve_rollforward`, `mart_remittance_late_fees`, `mart_disbursement_control_totals` |
| `claims` | `mart_claim_summary`, `mart_claims_kpi`, `mart_claim_components` |
| `dealer` | `mart_dealer_production`, `mart_dealer_pending_contracts`, `mart_remittance_aging`, `mart_dealer_scorecard`, `mart_surcharge_audit`, `mart_specialty_line` |
| `regulatory` | `mart_funded_reserve_summary`, `mart_funded_reserve_contracts`, `mart_funded_reserve_claims`, `mart_market_survey`, `mart_regulatory_line_values`, `mart_regulatory_windows` |
| `core` | `mart_migration_parallel_run` (both systems side by side at cutover), `mart_claim_contact_by_dealer` |

Report counting rules (status set, scope ITD or PERIOD, basis coverage cost or retail) live in seeds such as
`dbt/seeds/regulatory/report_definitions.csv` and `report_status_sets.csv`, never in code comments.

## Snapshots (SCD2, daily)

`snap_dealer`, `snap_product`, `snap_contract_status` and `snap_claim_status` use `strategy: timestamp` on the
source `updated_at` and `hard_deletes: invalidate`, so `dbt_valid_from` is when the change happened in the source and
a soft-deleted row closes its current version. They run in every `dbt build` from the first run. History from before
the first snapshot comes from `contract_status_history` and `claim_history` through `int_status_history_backfill`,
so history never starts late. A source reset (new source epoch) drops and rebuilds the snapshots, because the old
world's history no longer applies.

## Tests and gates

| Kind | Where |
|---|---|
| Generic: `unique` / `not_null` on keys, `relationships`, `accepted_values`, plus custom `expression_is_true` and `unique_combination` | model YAML, `dbt/tests/generic/` |
| Domain singular tests (each mart value checked against its `metrics.yml` definition) | `dbt/tests/<area>/` |
| **Diagnostics gate** (`tag:diagnostics`): product-dimension match rate by entered year >= 99.5%, contract key-bridge coverage >= 99%, claim-to-contract coverage >= 98%, fan-out guard, claim sign, no demo rows, freshness, posting-period integrity | `dbt/tests/diagnostics/`, re-run by `python -m pipeline.diagnostics` into `audit.diagnostics_result` |
| **Reconciliation gate**: row counts and dollar sums per source and entity, source to raw to marts, $0.01 / 0-row tolerance after documented exclusions | `dbt/tests/reconciliation/` and `python -m pipeline.reconcile` into `audit.reconciliation` |
| Source freshness: `_loaded_at` warns after 24 h, errors after 48 h | `dbt/models/sources.yml` |

The full build excludes `tag:gate`; the two gate steps run those tests and record the result. A failed gate stops
actuarial, semantic, publish and reports. What to do for each failing check is in the
[RUNBOOK](operating-model/RUNBOOK.md).

## Deprecated V1 shapes

`dbt/models/marts/core/v1_compat/` holds V1-shaped views rebuilt on the V3 core, tagged `deprecated`, so older
pages keep running for one release. Build nothing new on them; they are removed once every consumer has moved
([RELEASE](operating-model/RELEASE.md)).
