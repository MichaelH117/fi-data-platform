# Legacy vs better

Owner: Senior Director. This page describes how reporting usually grows up at a VSC administrator, what goes
wrong along the way, and how this platform handles each problem. It describes a common industry pattern, not
any one company. Every example and number in this repo is synthetic.

## How the legacy setup usually grows

| Stage | What it looks like | What it leaves behind |
|---|---|---|
| **1. App-database reporting** | The admin system is a custom app on its own database. Reports are views, stored SQL inside the app, and CSV or Excel exports. | No history. A changed row overwrites the old one, so "what did we report last quarter?" cannot be answered. |
| **2. BI over the operational database** | A BI tool connects straight to the admin database (or a copy of it). One model grows to cover sales, claims, finance and compliance. | A monolithic model with hundreds of measures, each copied for MTD, QTD, YTD and prior year. Counting rules live in DAX and in spreadsheet comments. |
| **3. Partner feeds and white-label programs** | Dealer-integration networks and partner programs send daily files over SFTP. White-label administrators need their own branding and scope. | Many more sources with different keys. Per-dealer exports are built by hand. Demo and test dealers leak into reports. |
| **4. Warehouse migration** | The team moves to an orchestrated pipeline into a cloud warehouse with layered models and a governed BI layer. | Done badly, the old problems move to the new warehouse. Done well, the migration is the moment to fix them. |

This repo is stage 4 done well. The legacy admin system (`data/source/legacy_admin.db`) holds the historical
book, entered before the **2023-01-01 cutover**. Admin Core holds everything after it. Both load side by side
every day, so the migration is visible in the data itself.

In this repo a **tenant** is a white-label partner program (`LKS`, `NGT` and `PVA` in the synthetic data). The
tenant is the unit that the row-level security (semantic model, Postgres and Snowflake policies), the raw-data
portal and the dealer packets scope by, together with the dealer. Each program gets its own branded packets under
`site/dealers/<tenant>/<dealer>/`, one folder per dealer, built every run by [publish/](../publish/).

## Pain points and the fix

| # | Legacy pain point | Why it hurts | What this platform does | Where |
|---|---|---|---|---|
| 1 | Reports query the operational database | Slow queries on the live system, no history, numbers change under you | Reports read marts only. Raw, staging and intermediate are off limits to reports | `reports/`, design 3 |
| 2 | No change history | Last quarter's figures cannot be reproduced | Change-tracking CDC into append-only raw tables with `_loaded_at`, `_source_version` and `_is_deleted`; daily SCD2 snapshots from day one | `ingest/`, `dbt/snapshots/` |
| 3 | Status history starts late | Older claims show blank status and dates | Snapshots run every day from the first run, and a history backfill rebuilds older statuses from the change log | `dbt/snapshots/`, `snap_claim_status` |
| 4 | Two sources, two keyspaces | Direct joins between the legacy and new systems match almost nothing | A conformed contract key and a normalized claim-number bridge, with a coverage gate (99% contracts, 98% claims) | `int_` key bridges, `pipeline/diagnostics.py` |
| 5 | Zero-padded vs unpadded claim numbers | Claims silently fail to join | Claim numbers normalized once in the bridge | `dbt/models/intermediate/` |
| 6 | Stale product dimension | Inner joins drop the newest vintages without any error | Left joins, an "unknown" member, and a match-rate gate by entered year (at least 99.5%) | diagnostics gate |
| 7 | Contract fact at transaction grain | Joining to it double-counts contracts | A fan-out guard on every contract-grain model: row count equals distinct contracts | diagnostics gate |
| 8 | Claim amounts stored negative in one source | Reserve formulas flip sign (a "minus claims" becomes a "plus") | Signs conformed in staging; a test checks that marts are positive and raw legacy stays negative | staging, diagnostics gate |
| 9 | Two overlapping claims facts | Two answers to "claims paid" | One claims fact at payment-line grain; every roll-up is derived from it, and reconciliation ties them | `fct_claim_payment_line` |
| 10 | A very wide contract dimension (well over 100 columns) | Slow refreshes, unclear ownership | Six split contract dimensions: core, terms, finance, vehicle, actuarial class, claim summary | `dbt/models/marts/core/` |
| 11 | Facts without a posting period | Late events restate months that were already closed and reported | Every financial fact carries `posting_period_key`; late events post to the first open period | design 2.5 |
| 12 | Demo and test dealers filtered in each report | Someone forgets, and test volume shows up in a filing | Excluded once, in `int_reportable_scope`; a gate checks that no mart holds a demo or test row | intermediate, diagnostics gate |
| 13 | Counting rules hidden in Excel comments and DAX | Two analysts get two answers to "contracts in force" | Report-definition and status-set seeds, with tests. A filing line names its status set, date rule, scope and basis | `dbt/seeds/regulatory/` |
| 14 | Monolithic BI model with copied time-intelligence measures | Hundreds of near-duplicate measures that drift apart | Four domain models generated from `metrics.yml`, plus one calculation group (MTD, QTD, YTD, PY, YoY %, T12M) | `semantic/` |
| 15 | Static row-level security table | Access goes stale as dealers and users change | One user-to-dealer-and-tenant map drives the semantic model's dynamic RLS, the dealer packets and the portal | `semantic/`, `publish/`, `portal/` |
| 16 | Hand-built Excel exports per dealer | Hours of work, and the wrong dealer's rows can end up in the wrong inbox | Per-dealer packets built every run, branded per administrator, with a test that a packet holds only its own dealer's rows | `publish/`, `site/dealers/` |
| 17 | Dealers ask for their raw data by email | Slow turnaround, ad-hoc extracts | A read-only raw-data portal (CLI and HTTP API) that cannot see another dealer's or tenant's rows | `portal/` |
| 18 | Feed files trusted as delivered | A resent or truncated file double-loads or drops deals | Manifest row counts and checksums, data contracts, and a quarantine table with a reject reason for every bad row | `ingest/feed`, `contracts/` |
| 19 | "Does this match the source?" asked after a complaint | Errors reach regulators and dealers first | Row and dollar reconciliation from source to raw to marts on every run, plus calibration control totals | `pipeline/reconcile.py` |
| 20 | A broken load still publishes | Wrong numbers go out on schedule | The gates block publish and reports. A test injects a failure to prove it | `pipeline/run.py`, `tests/test_pipeline.py` |
| 21 | Knowledge lives in people's heads | Nobody can cover for a sick analyst | Runbook per failed check, SLAs, an intake form, a release process, a postmortem and a generated data dictionary | `docs/operating-model/` |
| 22 | PII copied into every extract | Every export is a privacy risk | PII hashed or dropped at staging; dealer packets carry no customer data at all; the raw-data portal returns initials and masked VINs, full contact data only to a dealer admin, and logs every export | staging, `publish/packets.py`, `portal/` |

## How the migration is shown, not just the end state

- **Parallel sources.** The legacy system and Admin Core both feed the warehouse every day. Converted claims and
  amendments in Admin Core point back to legacy contracts, so the key bridge has real work to do.
- **Reconciliation as the cutover test.** Row counts and dollar totals per source and entity must tie out (to
  $0.01 and 0 rows) after documented exclusions. That is the evidence a cutover sign-off needs.
- **Environments.** Dev, UAT and prod run locally: `python -m pipeline.run --env dev|uat|prod`
  ([pipeline/envs.py](../pipeline/envs.py)) gives each one its own sources, warehouse
  (`data/env/<env>/warehouse.duckdb`; prod keeps `data/warehouse/warehouse.duckdb`), dbt target, site, publish
  and run-log folders, so a UAT build never touches prod. UAT and dev start from a copy of the prod sources.
  Before promotion, [tools/env_diff.py](../tools/env_diff.py) compares UAT with prod table by table (row counts
  and money totals) and fails on any difference the change ticket does not explain
  ([RELEASE](operating-model/RELEASE.md)). On a server the same tiers are the dbt `snowflake` target databases
  and zero-copy clones, plus the Dev, Test (UAT) and Prod BI workspaces ([ACCESS_MATRIX](ACCESS_MATRIX.md));
  the Snowflake scripts also set up separate loading, transforming and reporting warehouses and roles.
- **Cost controls.** The Snowflake scripts use extra-small warehouses with auto-suspend and a resource monitor
  that warns at 80% and suspends at 100%.

## What is still legacy-shaped

Honest gaps, ranked by value, risk and effort in the [roadmap](ROADMAP.md):

- The legacy in-force book (15,873 contracts, $19.6M coverage cost at the cutover) is not converted into Admin
  Core. It stays on the legacy system of record, and reconciliation reports it as `not_migrated`.
- The 7 AM SLO has no tracked days yet, and no live UAT-to-prod promotion check has been run.
- Remittance late fees and the combined ratio each still have two definitions.
- A few V1 pages still read the retired schema names.
- The Snowflake, Postgres and Airflow paths are written but not executed in this repo.

## More controls, each backed by a test or a gate

**Operations and run health**
- The quality and operations pages show the current run and the full dbt build results.
- The documented `NOT_MIGRATED` migration row is reported as an exclusion, not a reconciliation break.
- `--append-day` is tested end to end: each day loads exactly the new source versions and moves the snapshots forward.
- The README's model, seed, snapshot and test counts are checked against the dbt manifest.

**Intake and migration**
- Two more intake sources (a CRM call-center export and a paginated admin API extract) have data contracts and quarantine.
- A migration parallel-run mart compares both systems at cutover, and reconcile records it in `audit.reconciliation`.
- Every legacy contract gets an obligor during conforming; the `obligor_basis` column says whether it came from the
  source record or the conversion state map, so filings can footnote it.

**Claims and actuarial**
- The filing's claims-paid count and dollars cover the same contracts, and each filing line says which contracts it covers.
- Claims with no matched contract are their own P&L line and are explained on the claims KPI tile; a test ties
  claims reported across pages.
- Claims are released on their report day, with a realistic slow tail (parts waits, pending invoices, late
  supplements), so aged-claim measures behave as they would in production.
- One report-lag model serves the full build and appended days, so no loss is counted twice.
- The actuarial pages use one valuation cut-off and explain IBNR size from measured report lag.
- IBNR is back-tested against what emerged after valuation and against the generator's true IBNR.
- Losses on unexpired coverage are shown separately from IBNR, so the claims and cohort tables add up.
- A premium deficiency test checks each segment's unearned premium against its unexpired losses.

**Finance**
- Reinstated cancels are added back into net written, so counts and dollars use the same basis.
- A cancelled contract's unearned balance is trued up at cancel, so every dollar is either earned or refunded.
- Commission advances earn down on the contract's own curve, tested against unearned commission.
- Profit share is split into payable and dealer-entity surplus, with a seeded yearly payout.
- Two clearly named loss ratios ("on earned premium" and "on claims reserve"); no two metrics may share a label.
- Every core fact carries `posting_period_start_date` next to its posting-period key.
- Remittance aging uses one shared band seed, and a test checks that dealer and finance late fees agree.
- The Executive overview reads the same P&L mart as the Finance pages, so the same label shows the same number.
- The finance Excel pack leads with readable keys, hides hash keys and sets money, percent and count formats;
  negative money uses one format (`-$5,689`) on every page.

**Dealers and access**
- A dealer scorecard (one row per dealer and month, with trailing-12 measures and watch flags) has its own page.
- Dealer users in the semantic model see only their own rows in every split contract table and in the payee table.
- Synthetic business names are drawn from a generic word list, so no generated name reads as a real person.

## Improvement log

Each line is one control added in the latest iteration; the test or gate that holds it is named in brackets.

- Contracts never remitted are voided 120 days after entry, in the build and in the daily sweep. [`test_generator.py`]
- Every source build stamps a source epoch; ingest reloads a reset source in full, and the pipeline then runs dbt with `--full-refresh` and rebuilds the snapshots. [`test_ingest.py`, `test_pipeline.py`]
- `python -m pipeline.run --seed N` regenerates the sources when the seed on disk differs. [`test_pipeline.py`]
- Late paperwork is simulated: a small share of postings is recorded after the month closes, without moving its posting date. [`test_generator.py`]
- Legacy pulls re-read a five-minute look-back window and skip rows already loaded by fingerprint, so late commits are not missed. [`test_ingest.py`]
- Each contract has its own miles per day, and claims stop at the earlier of the term end and the mileage limit. [`test_generator.py`]
- Ancillary claims emerge evenly over the term instead of with the back-loaded vehicle-service shape. [`test_generator.py`]
- Losses inside the waiting period are generated and always denied with the waiting-period reason. [`test_generator.py`]
- Dealer packets read only marts, carry period labels and formats, and tie to the scorecard's trailing-12 totals. [`test_platform.py`, `test_publish.py`]
- Each partner program gets its own packet with program totals, a dealer list and claims by product line. [`test_platform.py`]
- `--env dev|uat|prod` builds into separate paths, and `tools/env_diff.py` compares UAT with prod before promotion. [`test_platform.py`]
- The front-page watchlist and the dealer scorecard use one counting helper, so both show the same dealers. [`test_dealer.py`]
- The scorecard names its denominator "earned claims reserve", and the site never calls it earned premium. [`test_dealer.py`]
- The cession page and the profit-share statement show the same earned, paid and closing-due figures. [`fin_cession_due_ties_to_statement`]
- Profit-share distributions leave the reserve, and the roll-forward shows them as a surplus-released flow. [`fin_reserve_rollforward_surplus`]
- Payables and overpayments are shown separately instead of netted. [`test_finance.py`]
- The close-controls table covers closed periods only and states its range. [`test_finance.py`]
- Current reserve held on the regulatory filing is the funded reserve less claims paid on the same in-force block. [`reg_current_reserve_net_of_claims`]
- Time-intelligence items and balance measures in the semantic model stop at the last closed period. [`test_semantic.py`]
- A claim-grain fact in the semantic model counts claims from both source systems. [`test_semantic.py`]
- Every headline tile on the site maps to a semantic-model measure. [`test_semantic.py`]
- Row-level security filters are run as SQL for a sample dealer and partner user and checked against the packet filter. [`test_semantic.py`]
- Earning curves are checked every run against actual claim emergence, with a warning gate on the Actuarial page. [`test_actuarial.py`]
- The README pitch names all eight diagnostics gates and every intake source, and its dbt counts track the manifest. [`test_docs.py`]

## Round 3 improvements

One line per improvement; the test or gate that holds it is named in brackets.

- The raw-data portal masks customer contact data by default, opens full contact data only to a dealer admin for its own dealer, and logs every export and read. [`test_portal.py`]
- A late or repaired feed drop is loaded by its own sequence number instead of being skipped, and the freshness gate fails on a sequence gap or a drop that was seen but never loaded. [`test_ingest.py`, `diag_freshness`]
- The Admin Core data contract lists every column's type and NOT NULL rule, and rows that break it go to quarantine instead of loading as NULL. [`test_admin_core.py`]
- The generator now produces wrong-amount surcharges and RVs with a blank chassis VIN, so those checks are exercised. [`test_generator.py`, `warn_rv_missing_chassis_vin`]
- Late remittance varies by dealer and slips whole batches, so dealers spread across the late-rate range. [`test_generator.py`]
- Each dealer has one published deliverable, the tenant packet, and every cancel-rate label names its window (ITD, T12 or month). [`test_dealer.py`]
- `tools/demo_history.py` builds a few days of run history with one real gate failure and its recovery, and `tasks.ps1 schedule` registers a daily run. [`test_platform.py`]
- The quality page reads only audit tables and core marts, with one quarantine row per rejected intake row and rows counted per layer. [`test_diagnostics.py`, `test_ops.py`]
- Source freshness runs after every successful build and warns on a stale source. [`test_pipeline.py`]
- History models are rebuilt from every raw version, and `--full-refresh` keeps raw; only `--reload-raw` drops it. [`core_snapshots_agree_with_versions`, `test_pipeline.py`]
- Every staging model has key tests that match its data contract. [`test_pipeline.py`]
- A converted claim with no contract on the Admin Core copy takes the legacy copy's contract. [`core_converted_claims_resolve_and_agree`]
- Legacy refund percent uses the same basis as Admin Core and falls back to the cancel method's share. [`core_refund_pct_matches_method`, `recon_earned_le_written`]
- Every metric names an owner and a certification status, and every executive headline tile maps to a certified metric. [`test_semantic.py`]
- The README and release docs describe the dev/UAT/prod paths and the env_diff promotion check as the code builds them. [`test_docs.py`]
- ARCHITECTURE.md and DATA_MODEL.md describe the current layers and name only models that exist. [`test_docs.py`]
- The profit-share monthly table shows payables and overpayments separately and ties to the statement cards. [`test_finance.py`]
- The commission statement ties to the cost-split commission, including legacy contracts with no agent ledger. [`fin_commission_ties_to_cost_split`]
- Commission expensed on a cash basis is bridged to the P&L earned commission. [`fin_commission_bridges_to_pnl`]
- The funded reserve reconciliation computes the rest of the book independently instead of as a plug. [`fin_reserve_rollforward_ties_to_cession`]
- Actuarial labels say "earned claims reserve", and development factors use only diagonals from the current posting regime. [`test_actuarial.py`]
- BF uses premium net of expected cancellations and a mature-cohort loss ratio, and the forecast band never narrows further out. [`test_actuarial.py`]
- The market survey stops at the last closed period, and its premium lines are labelled gross written. [`reg_windows_within_closed_periods`, `test_regulatory.py`]
- Ruff line-length findings in the report shell and semantic build were fixed so `ruff check .` is clean.

## Round 4 improvements
- Write-audit-publish: dbt builds into a staged copy of the warehouse, and it goes live only after the diagnostics and reconcile gates pass on that same build; a failed gate leaves the last good build live. [`test_pipeline.py`]
- The staged copy keeps the live file name (in `data/warehouse/_staged/`), so the catalog name inside every view still matches after the swap. [`python -m pipeline.run`]
- Claim headers are reconciled source to staging to `dim_claim` by system and status group (denied, open, paid), with counts and requested dollars, plus Admin Core pending dollars. [`recon_raw_to_core_counts`, `test_reconcile.py`]
- Earned premium follows the status history: earning starts at activation, closed months are caught up in the first open period, and a void after activation posts a reversal instead of deleting history. [`core_earned_never_restates_closed_periods`, `test_warehouse.py`]
- An AMEND earns only from its own posting period onward, so a price change never re-prices closed months. [`test_warehouse.py`]
- The cancel true-up threshold rounds before it compares, so float noise cannot move a half-cent true-up in or out of a closed period between builds. [`test_warehouse.py`]
- The P&L program margin now equals 1 minus the combined ratio, because the admin fee is a program cost in both. [`fin_pnl_margin_matches_combined_ratio`, `test_finance.py`]
- Commission statements count each contract once over periods and agents, so the total ties to contracts sold. [`fin_commission_contracts_within_sales`, `test_finance.py`]
- The dealer scorecard's early-claim watch uses the same actual-vs-expected test as the Actuarial page, so both name the same dealers. [`assert_scorecard_early_claim_watch_matches_ae`, `test_dealer.py`]
- Early-claim labels say which basis they use (share of claims vs per-contract incidence). [`test_dealer.py`]
- Young actuarial segments get a tail factor borrowed from the longest-history segment, instead of an assumed 1.0. [`test_actuarial.py`]
- The IBNR back-test scores only claims first reported after valuation, and flags the IBNR as short when they run ahead of the expected report lag. [`test_actuarial.py`]
- Actuarial drops claims with no contract or a loss before the sale, and shows them as reconciling lines. [`test_actuarial.py`]
- The ALL actuarial segment is the sum of the segments; the pooled triangle is kept only as a check. [`test_actuarial.py`]
- IBNR now comes from daily report lags (claims per day x days of lag still to come x severity, plus expected development on known claims), is tested against the generator's expected IBNR, and is back-tested over 12 earlier quarter-ends. [`test_actuarial.py`]
- Funded-reserve shortfall tiles say they add up latest filings, split the shortfall by basis, and explain each state's cause. [`test_regulatory.py`]
- The Operations and Data quality pages show the run that builds them as OK once its gates pass, and mark a run that stopped part way as INCOMPLETE. [`test_ops.py`]
- The README shows four screenshots of the built site, and `tools/screenshots.py` re-takes them. [`test_docs.py`]
- A ranked roadmap lists the known gaps with business value, risk, effort and owner. [`docs/ROADMAP.md`, `test_docs.py`]
- The semantic model's column lock is refreshed to include this round's new mart columns. [`python -m semantic --check`]

## Round 5 improvements
- The marts now also live in a real database: an opt-in step copies 104 tables into a local PostgreSQL 16 server in one transaction, and commits only when row counts and dollar totals tie out to DuckDB. [`python -m warehouse.postgres reconcile`, `test_postgres.py`]
- Dealer row-level security runs on that server from the same user-to-dealer map, and the report reader can only read (no writes, no access map). [`warehouse/postgres/rls.sql`, `test_postgres.py`]
- A five-page Power BI project (PBIP) opens in Power BI Desktop and reads from Postgres; it is generated from the same plan as the semantic model, so every measure still maps to `metrics.yml`. [`python -m powerbi --check --validate`, `test_powerbi.py`]
- The Power BI files are checked against Microsoft's published JSON schemas, and every field a visual uses must exist in the model. [`test_powerbi.py`]
- The measure table is named `Key Measures`, because Desktop reserves the name `Measures` and refuses to open a model that uses it. [`test_semantic.py`]
- `python -m semantic --target postgres` writes the four domain models pointed at Postgres; the default Snowflake output is unchanged. [`python -m semantic --check`]
- The raw-data portal has a browser UI with sign-in, previews, CSV/JSON downloads and an access-log panel, and isolation is checked on the server for every request. [`test_portal.py`]
- The report site has a grouped sidebar menu, a Start here tour, KPI tiles with trend lines and "i" explanations, and light and dark themes. [`test_reports.py`]
- Postgres stays opt-in: the default run and CI need no server, and the live tests skip without one. [`python -m pipeline.run`, `pytest -q`]

## Round 6 improvements
- A legacy database is built in Postgres from the same synthetic raw rows (`python -m legacy build`, about 50 s): three source-named schemas with no keys, duplicate dealer ids, a stale product dimension, opposite claim signs, two "producer" columns, a sentinel date, test contracts, a mirrored second-tenant schema, clutter and a separate custom-field (EAV) database. [`test_legacy.py`]
- `python -m legacy demo` asks the same 10 business questions of the legacy and the modern database; all 10 legacy answers are wrong or fail (rows lost, books zeroed, a sign error, a doubled filing, a 70x slower filter, a VIN lookup that times out). Results are in `docs/LEGACY_BENCHMARK.md` and on the site page Platform > Legacy vs better. [`test_legacy_benchmark.py`]
- Core dimensions and facts declare their primary key in a dbt contract, every key column has a relationships test, and the Postgres copy enforces 97 validated foreign keys plus a BRIN date index on the big monthly facts. [`dbt/models/marts/core/_core_keys.yml`, `test_postgres.py`]
- A build test fails when more than 2% of a fact's contract, dealer or product keys fall on the unknown member (-1). [`core_unknown_member_share`]
- Duplicate dealer ids are merged by a survivorship rule into one dealer key, with a crosswalk from every source id. [`dim_dealer_crosswalk`, dbt unit test]
- The producer is labelled `selling_dealer` or `provider_entity`, and the contract carries both keys, so filtering the wrong one can no longer zero a book. [`dim_producer`]
- Sentinel dates map to NULL with a flag once in staging, and API test contracts are excluded once in the reportable scope. [`dbt/macros/foundations.sql`, dbt unit test]
- Custom fields stored as name/value rows are pivoted into typed columns on the contract vehicle dimension. [`stg_admin_core__contract_custom_field`]
- A schema registry names the owner, tier, purpose and retention of every schema, and CI fails on backup, empty or personal objects in serving schemas. [`pipeline/schema_registry.yml`, `python -m pipeline.clutter_check`]
- A SQL playbook of 24 analyst queries (catalog discovery, freshness probe, diagnostics, filing pulls, exports, DAX-to-SQL translations) runs with per-file timeouts and row-count checks. [`python -m legacy playbook`, `test_playbook.py`]
- The semantic model's measures, relationships and report field usage are exported to CSV for DAX-to-SQL work, and every report build re-writes them to `site/downloads/semantic/`. [`python -m semantic export-docs`, `test_semantic_export.py`]
- The product is named "F&I Data Platform" on every page, the Power BI project and the portal; values that round to zero no longer show as `-$0.00` or `-0.0%`, and long notes wrap. [`test_reports.py`, `test_platform.py`]
- Runtime: slow nested-loop joins were rewritten, one 8M-row model became a view, and workbook writes run in a process pool. A full run from empty took 543 s this round (dbt 228 s, reports 82 s, publish 40 s), and `pipeline/runtime_check.py` fails a run that is more than 20% slower than the last comparable one.
