# Runbook: what to do when a check fails

Start here whenever `python -m pipeline.run` exits non-zero. Publishing is already blocked, so there
is no rush to "get something out". The job is to find the cause, fix it where it starts, and re-run
from the gate. Never lower a threshold to make a run pass. That is a release (see
[RELEASE.md](RELEASE.md)).

## First five minutes (every failure)

1. **Find the failed step.** Read the last lines of `data/runs/<run_id>.log`, or:
   ```sql
   select run_id, status, error, steps from audit.pipeline_run order by started_at desc limit 1;
   ```
2. **Open the Operations page** (`site/ops.html`). It still shows yesterday's site; the gate tables
   show the last *recorded* results.
3. **Read the check's headline number:**
   ```sql
   select check_name, status, measured_value, threshold, message
   from audit.diagnostics_result
   where run_id = (select max(run_id) from audit.diagnostics_result) and status <> 'pass';
   ```
4. **Decide the severity** ([SLA.md](SLA.md)). If 07:00 is at risk, send the stakeholder notice
   (template at the bottom of this page) by 07:30 at the latest.
5. **Open an incident** from [INCIDENT_TEMPLATE.md](INCIDENT_TEMPLATE.md) for Sev 1 and Sev 2.

To see the failing rows of any diagnostic, run it alone. Each one returns one row per failure:

```powershell
.venv\Scripts\dbt test --project-dir dbt --profiles-dir dbt --select <check_name>
```

After the fix, resume from the first gate. Earlier steps only need to run again if the fix was
upstream of them:

```powershell
.venv\Scripts\python -m pipeline.run --from dbt          # model or seed fix
.venv\Scripts\python -m pipeline.run --from ingest       # raw data had to be reloaded
.venv\Scripts\python -m pipeline.run --from diagnostics  # threshold release already merged
```

`--only` cannot skip a failed gate: `pipeline/run.py` reads `data/runs/_gates.json` and refuses.

---

## Diagnostics checks (`dbt/tests/diagnostics/`, gate step 5)

### `diag_product_dim_match_rate`
- **Checks:** in every entered year, at least 99.5% of contracts find their product in
  `core.dim_product` (`product_key <> '-1'`). Var `min_product_match_rate`.
- **Usually means:** the product catalog is stale. A new plan year or program went on sale before
  it was added to the source catalog, so new contracts point at a product the dimension does not
  have. An inner join would silently drop those contracts. Here they land on the unknown member and
  the gate stops the run. The failing rows show which entered years dropped.
- **Do:**
  1. List the unmatched contracts for the failing year:
     `select entered_year, count(*) from core.dim_contract where product_key = '-1' and contract_key <> '-1' group by 1 order by 1;`
  2. Check `snapshots.snap_product` for when the plan was last seen.
  3. If the source catalog is missing the plan, the Data Engineer fixes the catalog or its
     replication. Do not hand-add rows to the dimension.
  4. Re-run `--from ingest`.
- **Worked example:** [postmortems/2026-03-stale-product-dimension.md](postmortems/2026-03-stale-product-dimension.md).

### `diag_contract_key_bridge_coverage`
- **Checks:** at least 99% of contract references (claims, cancellations, transactions, amendments,
  cash) resolve through `intermediate.int_contract_key_bridge`. Var `min_contract_bridge_coverage`.
- **Usually means:** a source started sending ids in a new keyspace or format (for example, a
  converted legacy reference without its prefix), or the bridge lost rows after a soft delete.
- **Do:** group the unresolved references by `reference_type` and `source_system`, and compare a
  sample with `raw_*` to see the id format. The fix goes in the bridge normalization (Data
  Engineering Manager) or in the extract (Data Engineer). Re-run `--from dbt`.

### `diag_claim_contract_coverage`
- **Checks:** at least 98% of claims, per source and overall, resolve to a `contract_key`. Var
  `min_claim_contract_coverage`. The coverage number is reported on every run, even when it passes.
- **Usually means:** claim numbers changed format, or claims arrived for contracts that have not
  been replicated yet. Zero padding is the classic format change: the bridge normalizes with
  `ltrim(claim_no, '0')`.
- **Do:** look at `intermediate.int_claim_number_bridge` where `not has_contract_key`.
  - If the contracts exist upstream but have not landed yet, it is a timing issue: re-run ingest
    after the next CDC window.
  - If the number format changed, fix the normalization and add the new pattern to the bridge tests.

### `diag_fan_out_guard`
- **Checks:** every contract-grain model has `count(*) = count(distinct contract_key)`.
- **Usually means:** a join to a finer-grained table (transactions, options, surcharges, status
  history) multiplied contracts. Every dollar total downstream would be overstated.
- **Do:** the failing row names the model. Find the most recently added join (`git log -p` on the
  model) and aggregate the finer table to contract grain *before* joining. Never "fix" it with
  `select distinct`, which hides the cause.

### `diag_claim_sign`
- **Checks:** after conforming, `paid_amount >= 0` everywhere, and the raw legacy feed is still
  stored negative, as its documented contract says.
- **Usually means:** one of two things:
  - The staging sign flip was skipped for a new column.
  - The legacy system changed its convention upstream. If nobody notices, the flip then turns
    already-correct amounts negative, so this case fails too.
- **Do:** if conformed rows are negative, fix the staging model. If raw legacy amounts are now
  positive, stop and confirm the change with the source owner. Then update the staging rule and this
  check in one release.

### `diag_no_demo_rows`
- **Checks:** no demo dealer or test-range contract reaches any mart. The exclusion happens once,
  in `intermediate.int_reportable_scope`.
- **Usually means:** a new model read staging or intermediate directly instead of going through the
  reportable scope, or a new demo dealer was created without `is_demo = 1`.
- **Do:** the failing row names the model. Route it through `int_reportable_scope`. For a new demo
  dealer, add it to `dbt/seeds/core/demo_exclusions.csv` and ask the source owner to flag it.

### `diag_freshness`
- **Checks:**
  - The latest `_loaded_at` for each raw source is within 48 h of run time (var `max_raw_age_hours`).
  - Every landing file that failed its manifest checksum or row count was quarantined.
  - Every raw landing file is listed in a manifest.
- **Usually means:** the feed did not arrive, ingest ran against an old copy, or a bad file was
  promoted.
- **Do:**
  1. Check `data/landing/` for today's folder and its `_manifest.json`, and check
     `audit.ingest_watermark` for the source.
  2. If the feed is late, wait for it rather than publish stale numbers. The SLA allows a late day.
  3. If a bad file was promoted, quarantine it in `raw_network_feed._quarantine` with a
     `reject_reason`, then re-run `--from ingest --full-refresh`.

### `diag_posting_period_integrity`
- **Checks:** every financial fact row has a `posting_period_key` that exists in
  `core.dim_posting_period`.
- **Usually means:** an event posted to a month the period spine does not cover yet (an as-of date
  past the spine), or a new fact skipped the period-lock macro.
- **Do:** the failing row names the fact. Check that `dim_posting_period` covers the as-of month,
  and that the fact gets its key through `int_posting_periods`. Closed periods roll late events
  forward to the first open period.

---

## Reconciliation (`pipeline/reconcile.py`, gate step 6)

A break is any difference above $0.01 or 0 rows between source, raw and marts, after the documented
exclusions (quarantine, demo/test scope, converted legacy claims).

```sql
select source_system, entity, measure, comparison, row_diff, amount_diff
from audit.reconciliation
where run_id = (select max(run_id) from audit.reconciliation) and status <> 'pass';
```

| Break at | Usually means | Do |
|---|---|---|
| `source -> raw` | Ingest missed or duplicated a CDC window, or a watermark moved past rows | Compare `audit.ingest_watermark` with the source's max version, then re-run `--from ingest --full-refresh` for that source |
| `raw -> marts` | A model filter dropped rows beyond the documented exclusions, or a join duplicated them | Count by key between the raw table and the mart, check the fan-out guard, and check new filters in the diff |
| Calibration tests (`dbt/tests/reconciliation/`) | The dealer-cost split no longer ties to transactions, roll-ups no longer tie to the one claims fact, or earned exceeds written | Run the named test alone. The failing rows are the contracts that do not tie |

A new legitimate exclusion is a release. Document it in `pipeline/reconcile.py` and in the model,
with a test, before the run is allowed to pass.

## Other failures

| Step | Symptom | Do |
|---|---|---|
| generate / feed / ingest | Python traceback | Data Engineer on call. Check `LANDING_DIR`, `ADMIN_CORE_PATH` and `LEGACY_DB_PATH`. Rows that fail a data contract are quarantined, not failed |
| dbt (step 4) | Model or test error | Read `dbt/logs/dbt.log` (or `DBT_LOG_PATH`). A failed schema test names its model and column |
| actuarial / semantic / publish | Traceback after the gates passed | The data is good. Fix the module and re-run `--from <step>`; the gates do not need to run again |
| reports | `report plug-in reports.<area> failed: ...` | The owner of that area fixes its `build()`. Nothing replaces yesterday's site until the build passes, including the broken-link check |
| any | `could not append audit.pipeline_run` warning | The run JSON in `data/runs/` is the fallback record. Check that no other writer has the warehouse locked (DuckDB allows one writer per file) |

## Stakeholder notice (copy, fill in, send by 07:30)

> **Subject:** Daily reports delayed for <business day>
>
> Today's refresh did not pass its quality checks, so the reports still show data as of
> <previous as-of date>. Nothing incorrect was published. We expect the refresh by <time>.
> Next update at <time>. Contact: <on-call role>.
