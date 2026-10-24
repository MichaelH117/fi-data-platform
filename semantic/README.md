# Semantic layer (Power BI-ready)

`python -m semantic` generates `semantic/model/`: four TMDL-style Power BI models (Sales, Claims,
Finance, Compliance), built from `dbt/models/metrics.yml`. A measure is defined once, in dbt.
This folder only translates it to DAX. Open any `<Domain>.SemanticModel` folder in Tabular Editor,
or put it in a Power BI project (PBIP), to deploy it.

| File | Owner | What it holds |
|---|---|---|
| `dbt/models/metrics.yml` | Senior Power BI Specialist (area owners propose) | Every measure and metric. Power BI hints go under `config.meta.pbi` so `dbt parse` still passes |
| `semantic/spec.py` | Senior Power BI Specialist | The model's layout: tables, relationships, which dimensions each domain gets, drill fields, hierarchies, which tables refresh incrementally |
| `semantic/metrics.py` | Senior Power BI Specialist | Reads the YAML and translates it to DAX |
| `semantic/build.py` | Senior Power BI Specialist | Writes the TMDL files: calculation group, RLS roles, field parameter, M partitions |
| `semantic/columns.lock.json` | generated | A snapshot of the mart columns, so the build doesn't need a warehouse |
| `semantic/model/` | generated, do not edit | The TMDL output plus `manifest.json` (each measure and the dbt metric it comes from) |

```powershell
.venv\Scripts\python -m semantic                    # build; checks the column lock against the warehouse if one exists
.venv\Scripts\python -m semantic --check            # CI: exits 1 if semantic/model/ is out of date
.venv\Scripts\python -m semantic --refresh-columns  # after a mart column change
.venv\Scripts\python -m semantic export-docs        # model + PBIR report docs as 4 CSVs -> site\downloads\semantic\
```

`export-docs` (`semantic/export.py`) is the Tabular-Editor-style documentation export: measures, calc
items, RLS and partition sources, relationships as SQL joins, and report -> page -> visual -> field
usage read from the PBIR report, for the modern and the legacy models. See `docs/SEMANTIC_STRATEGY.md`
and `docs/DAX_TO_SQL.md`.

## How it improves on the legacy model

| Legacy BI model | This model |
|---|---|
| One model with about 100 tables and hundreds of measures | Four domain models, each serving one audience, with shared dimensions |
| MTD, QTD, YTD, PY and KPI copies of every measure | **One** calculation group: Current, MTD, QTD, YTD, PY, YoY %, T12M. The generator rejects any measure name with a time word in it |
| DAX formulas written by hand, drifting from SQL | Generated from `metrics.yml`. A formula the translator can't map fails the build; it is never quietly rewritten |
| Two overlapping claims facts | `Claim Payment Line` is the only claims-dollars fact. Claim counts, denial and severity come from `Claim Summary` (one row per claim, both admin systems, loss month with reported month as a role-playing date), never from status history, which starts at the 2023 cutover |
| Demo dealers removed with columns inside the model | Removed once upstream in `int_reportable_scope`, so the model never sees them |
| A static RLS table | Dynamic roles over `Security User Access` (user to tenant and dealer), the same map the dealer packets and the portal use |

## Translation rules (metrics.yml to DAX)

- **Simple** metric. `sum` becomes `SUM`. `count` on the primary key becomes `COUNTROWS`.
  `count_distinct` becomes `DISTINCTCOUNT`. `average` becomes `AVERAGE`. `sum_boolean` becomes
  `CALCULATE(COUNTROWS(T), T[flag] = TRUE())`. Three `CASE` shapes are also allowed:
  `case when flag then 1|col else 0 end`, `case when col = 'X' then 1 else 0 end`, and
  `case when col in ('A','B') then 1 else 0 end`, plus the filtered sums
  `case when col = 'X' then amount else 0 end` and `case when col in ('A','B') then amount else 0 end`.
  Anything else has to become a dbt column first.
- **Role-playing dates.** `config.meta.pbi.date_role: <column>` wraps the measure in
  `USERELATIONSHIP` over an inactive period relationship (`spec.ROLE_PLAYING_PERIODS`), e.g. Claims
  Opened by reported month while every other claim measure is by loss month.
- **Balance** measures carry a MetricFlow `non_additive_dimension` (contracts in force, unearned
  premium, dealers on watch). They take the last period in context **at or before the last closed
  period**, so the default card never shows a partial month, and never add up across months.
- **Grain guards** (`config.meta.pbi`, generated, never hand DAX) stop a measure from adding rows
  that do not add up. `default_filter: {scope: ITD}` reads only ITD filing rows unless one Scope is
  sliced, so the unsliced Calculated Reserve equals the regulatory index (each state's latest ITD
  filing). `single_value: [cols]` returns BLANK unless each column has one value (Filing Line Value
  needs one report, line, scope and basis; the dealer peer percentile needs one dealer).
  `window: {column, order_by, last_when}` keeps a multi-window total blank except for LAST lines
  (in force), which show the latest window.
- **Dealer scorecard** ratios (loss ratio on claims reserve, cohort cancel rate, late-remit rate,
  average days late) are measures over the mart's monthly additive columns. Apply `T12M` for the
  site's trailing-12 scorecard, per dealer or rolled up to a group, tenant or the portfolio. The
  mart's rolling `t12_*` and precomputed ratio columns are not imported (`spec` table `drop`), since
  summing them across months counts each month 12 times; only the peer-percentile rank stays, behind
  a month-end snapshot measure.
- **Ratio** metrics become `DIVIDE([numerator], [denominator])`. **Derived** metrics replace each
  metric name with its measure; a single `/` becomes `DIVIDE`.
- **Cumulative** 12-month metrics, and anything tagged `calculation_item`, become **no** measure.
  They map to the `T12M` calculation item applied to the base measure, and `manifest.json` records
  that mapping.
- Domains come from `config.meta.pbi.domains` on the semantic model, and a metric can override
  them. Ratio inputs are pulled into the same model automatically.

## Time intelligence: posting-period calendar

The facts post to `posting_period_key` (yyyymm), so the calculation group filters the
**Posting Period** table, not a day-level calendar. This has three effects:

- **Year-earlier and trailing periods are simple key arithmetic.** PY is the same key minus 100, and
  T12M is the 12 keys up to the capped one (Oct 2025 to Sep 2026 for an Oct 21, 2026 as-of date).
- **Totals stop at the last closed month.** `Reporting Date[Last Closed Period Key]` caps the
  "latest period" logic, the same "complete months through" rule every site page uses, so the open
  month never leaks a partial month into MTD, YTD or T12M, and empty future periods never blank them.
- **Late events never restate a closed month.** They post to the first open period (the period
  lock), so the time items can't change a closed period either.

At month grain MTD equals the current period. It is kept so report authors see the familiar item.

## Storage mode: Import vs DirectQuery

**Default: Import for every table.** The marts are rebuilt once a day by 7 AM (see the SLA), and
Import gives the fastest visuals with the full DAX engine. The REPORTING warehouse is X-Small with
auto-suspend, so it only wakes for the refresh, never per visual.

| Option | Use it when | Trade-off |
|---|---|---|
| **Import** (default) | Daily-refreshed marts, heavy DAX (calc group, RLS) | Data is as fresh as the last refresh. Model size counts against capacity |
| **Import + incremental refresh** (generated on `Contract Month`, `Earned Disbursement`, `Disbursement`, `Claim Payment Line`) | Facts above about 1M rows | 10-year rolling window. Only the last 3 posting periods refresh, matching the period lock (closed months never change). Needs `RangeStart` / `RangeEnd` (in `expressions.tmdl`) and a filter that folds to Snowflake on `POSTING_PERIOD_KEY` |
| **Hybrid table** (incremental + a DirectQuery partition for the open period) | Claims staff need intraday payments | Premium or Fabric capacity. Every visual queries the open period, so the REPORTING warehouse stays awake |
| **DirectQuery** | Data must be live, or is too large to import | Slower visuals. Limited DAX: avoid the calculation group on very large DirectQuery facts. Every slicer click costs Snowflake credits |
| **Dual** (dimensions in a composite model) | When any fact is DirectQuery | Dimensions serve both modes without limited relationships |

**Security under each mode.** RLS works the same in Import and DirectQuery because the roles live
in the model. The Snowflake row access policy (`warehouse/snowflake/04_*`) is a second layer for
anyone who queries the warehouse directly. In the dev workspace, a deployment-pipeline rule sets
the `AnalyticsDatabase` parameter to `ANALYTICS_DEV`.

## Row-level security

| Role | Filter | Audience |
|---|---|---|
| Internal | none | Employees, through the workspace or app |
| Tenant Partner | `Tenant`, and every tenant-keyed dimension, limited to the signed-in user's tenants | White-label partner programs |
| Dealer User | Tenant as above, plus `Dealer`, `Dealer Group`, `Contract`, `Claim` and `Participation Entity` limited to the user's dealers | External dealers |

- The security table is hidden and has no relationships; roles read it with
  `USERPRINCIPALNAME()`. In production `user_login` holds the Entra ID UPN. The synthetic data uses
  placeholder logins.
- Every fact relates to both `Dealer` and `Tenant`, so a role filter always reaches the facts.
  `tests/test_semantic.py` checks this.
- The split contract dimensions (terms, vehicle, finance and so on) have no identifying columns.
  A dealer user sees only the aggregated values of their own rows.
- Compliance carries only the Internal role. External audiences never get it (see
  `docs/ACCESS_MATRIX.md`).
- To test a role, use "View as" in Power BI Desktop or Tabular Editor. A test login is any
  `user_login` from `core.dim_user_dealer_access`.

## Field parameter and hierarchies

- **`Drill Level`** is a field parameter over tenant, dealer group, dealer, state, product levels,
  reportable state, term band, rating cell, claim status and so on. Each model gets only the
  levels whose tables it has. Put it on a matrix's rows. Use it twice for a 1st-level and
  2nd-level pick.
- **Natural hierarchies:**
  - Posting Period: fiscal year, quarter, period.
  - Product: category, subcategory, product, plan, program.
  - Dealer: state, city, dealer.

## Craft rules followed

- Star schemas only. Explicit measures only (`discourageImplicitMeasures`), in one `Key Measures`
  table with display folders. Numeric fact columns are hidden.
- Transforms happen upstream in dbt. M only connects to the tables and filters for incremental
  refresh.
- Many thin reports run on these shared models. The models move Dev, Test, Prod through a
  deployment pipeline, and the TMDL is versioned in git.
- Profile slow visuals with Performance Analyzer and DAX Studio before release (see
  `docs/operating-model/` release process).

## Source targets (`--target`)

| Target | Command | Output | M source |
|---|---|---|---|
| `snowflake` (default) | `python -m semantic` | `semantic/model/` | `Snowflake.Databases(SnowflakeServer, SnowflakeWarehouse)`, `ANALYTICS.<SCHEMA>.<TABLE>` |
| `postgres` | `python -m semantic --target postgres` | `semantic/model_postgres/` | `PostgreSQL.Database(PostgresServer, PostgresDatabase)`, defaults `localhost:5432` / `vsc_analytics` |

Both are Import mode and share every measure, the calculation group, relationships and RLS roles; only the
partitions, source column case and the parameters differ. Snowflake stays the default because it is the
production layout; the Postgres variant is for the local server that `python -m warehouse.postgres sync`
fills (see [warehouse/postgres/README.md](../warehouse/postgres/README.md)). `--check --target postgres`
guards the committed Postgres folder the same way.

## Power BI report

`powerbi/` holds the five-page F&I Data Platform report (PBIP, PBIR) and its internal reporting model,
built from the same plan as these domain models (`python -m powerbi`). See `powerbi/README.md`.
