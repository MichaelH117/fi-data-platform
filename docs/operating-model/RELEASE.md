# Release process

Every change to a model, seed, metric, report, threshold or pipeline step ships the same way:
branch, checks, peer review, merge, scheduled run. There is no "quick fix in production". The gates
make a bad release *late* rather than *wrong*. This process keeps it from being late too.

## Environments

| Environment | Warehouse | Who writes | Used for |
|---|---|---|---|
| dev | your own copy: `data/warehouse/<role>.duckdb` (DuckDB) or `ANALYTICS_DEV` (Snowflake) | you | building and testing your slice |
| UAT | a zero-copy clone of prod (`warehouse/snowflake/07_*`), or a full local run on a scratch data folder | CI | parallel run and sign-off for risky changes |
| prod | `data/warehouse/warehouse.duckdb`, or `ANALYTICS` | only the scheduled run | what stakeholders see |

Dev rule for parallel work: set `WAREHOUSE_PATH`, `DBT_TARGET_PATH=target_<role>` (dbt resolves it under
`dbt/`, see design 4.2) and
`DBT_LOG_PATH=dbt/logs_<role>`, then build only your slice:
`dbt build --project-dir dbt --profiles-dir dbt --select "path:models/marts/<area>+"`.

### Running an environment locally

`python -m pipeline.run --env dev|uat|prod` points every path at that environment
(`pipeline/envs.py`): its own sources (dev and UAT start from a copy of prod's, the local stand-in for
the zero-copy clone), warehouse (`data/env/<env>/warehouse.duckdb`), dbt target (`dbt/target_<env>`),
publish, site and run-log folders. `--env prod` is the default layout.

**Promotion check.** Before a change that alters a metric or an external output is promoted, build it
in UAT and compare it with prod:

```powershell
.venv\Scripts\python -m pipeline.run --env uat
.venv\Scripts\python tools\env_diff.py --expect <ticket>_expected_diffs.csv
```

`tools/env_diff.py` compares the row count and every money total of each mart-layer table. A
difference listed in the `--expect` CSV (`table,measure,reason`) is *explained*; any other
difference fails the check (exit 1). The latest result is written to `data/env/env_diff.json` and
shown on the Operations page ("Promotion check"). The "parallel run in UAT for one full period" rule
below is met when this check passes for a UAT run whose `AS_OF_DATE` covers a closed period.

#### Sample promotion check

Real output of `tools/env_diff.py` on a copy of the prod warehouse in which a test change raised the admin fee
by 2% (listed in the ticket's CSV) and, by mistake, dropped three remittance-aging rows (not listed). It was run with `--base-warehouse` and
`--candidate-warehouse` pointed at the two files; on a real promotion the defaults pick UAT and prod, as shown:

```text
> type CHG-0142_expected_diffs.csv
table,measure,reason
finance.mart_pnl_by_product,earned_admin_fee,CHG-0142 admin fee rate +2%

> .venv\Scripts\python tools\env_diff.py --expect CHG-0142_expected_diffs.csv
env_diff uat vs prod: 56 tables, 228 pass, 1 explained, 3 unexplained -> data\env\env_diff.json
  fail      dealer.mart_remittance_aging.rows: 8898.0 -> 8895.0
  fail      dealer.mart_remittance_aging.late_fees: 162572.31 -> 162380.44
  fail      dealer.mart_remittance_aging.remitted_amount: 45199346.04 -> 45186554.75
  explained finance.mart_pnl_by_product.earned_admin_fee: 2750141.88 -> 2805144.72 CHG-0142 admin fee rate +2%
```

Exit code 1: the change is not promoted until the three `fail` lines are fixed or explained in the ticket.
With no change at all, every measure passes: `56 tables, 232 pass, 0 explained, 0 unexplained`, exit 0.
Promotion itself is the merge (step 6 below); the next scheduled prod run rebuilds prod from source.

## Steps

1. **Ticket.** The change has an intake ticket with an owner, a reviewer and a definition of done
   ([INTAKE_FORM.md](INTAKE_FORM.md)).
2. **Branch** from `main`. One change per branch.
3. **Build and test locally:**
   ```powershell
   .venv\Scripts\ruff check .
   .venv\Scripts\python -m pytest -q
   .venv\Scripts\dbt build --project-dir dbt --profiles-dir dbt --select "<your models>+"
   ```
4. **Pull request.** CI runs `lint-test`, `dbt-duckdb`, `e2e` (full run plus one appended day) and
   `portability` (`.github/workflows/ci.yml`). All must be green except `portability`, which is
   advisory.
5. **Peer review** against the checklist below. The reviewer is never the author. Financial Analyst
   I work is signed off by Financial Analyst II, and metric changes by the Senior Power BI Specialist.
6. **Merge.** The next scheduled run is the release. No hand-run deploys.
7. **Verify** the morning after: `site/ops.html` shows the gates green and the SLA met, and the
   numbers named in the definition of done tie.
8. **Announce** user-visible changes in the release notes (below).

## Peer-review checklist (before any report goes live)

- [ ] **Numbers tie to source.** Each new total ties to a control total: reconciliation, a finance
      total, or an existing page. The tie-out is in the pull request.
- [ ] **One definition.** New metrics live in `dbt/models/metrics.yml` and `docs/METRICS.md`, with a
      dbt test that checks the mart value against the definition. Ratios are summed-then-divided,
      never averaged.
- [ ] **Counting rules are seeds.** Status sets, scope and basis come from a seed with a test, not
      from a code comment.
- [ ] **Reads marts only.** Reports never read raw, staging or intermediate.
- [ ] **Scope.** Demo and test rows come out only through `int_reportable_scope`.
- [ ] **Grain.** Contract-grain models pass the fan-out guard; joins to dimensions are left joins
      onto the unknown member.
- [ ] **Security.** RLS and suppression are tested: a dealer sees only its own rows, peer cells are
      suppressed, and PII stays in staging (except marts flagged `contains_pii`).
- [ ] **Filters documented** on the page: period, statuses, basis, exclusions.
- [ ] **Readable.** Every chart has a plain-English caption and a data table, and status is never
      shown by color alone.
- [ ] **Docs.** `docs/REPORTS.md` and `docs/METRICS.md` are updated; the data dictionary is
      regenerated (`python -m reports.dictionary`).
- [ ] **Brand scrub.** `python tools/brand_scrub.py` is clean.

## Changes that need more than a review

| Change | Extra step |
|---|---|
| A diagnostics threshold, reconciliation exclusion or SLA | Director approval; record the reason in the pull request and in [SLA.md](SLA.md) or [RUNBOOK.md](RUNBOOK.md) |
| A metric definition used in an external report (filing, dealer packet) | Parallel run in UAT for one full period. Restatement note if past numbers change |
| A source change: new plan year, new product, new partner program, new tenant | Catalog check: `diag_product_dim_match_rate` passes on a UAT run that includes the new codes ([example postmortem](postmortems/2026-03-stale-product-dimension.md)) |
| A source-system migration or cutover | Parallel run of both sources, with row and dollar reconciliation per entity, before the cutover date. Keep a rollback clone (`warehouse/snowflake/07_*`) |
| Removing a report | Usage review shows no opens in 90 days, and the owner is told 2 weeks ahead |
| Removing deprecated models (for example `marts/core/v1_compat/`) | All consumers moved, then one release with the models tagged deprecated, then removal |

## Freeze and rollback

- **Error-budget freeze:** when SLO 1 has spent its budget ([SLA.md](SLA.md)), only fixes ship.
- **Month-end freeze:** no model or metric changes from the last business day of the month through
  business day 5, while the period closes (dbt var `period_close_business_day`).
- **Rollback** means reverting the merge commit. The next run rebuilds from source, because every
  layer is reproducible. Snapshots are never rolled back: SCD2 history is append-only.

## Monthly report-usage review

On the first Monday of each month the Reporting Manager reviews report usage. Unused pages are
retired, or merged into a page that is used. Every retirement is announced in the release notes.

## Release notes

Release notes go in the pull request description under `## Release notes`, one line per
user-visible change: what changed, who is affected, and whether any past number moved.
