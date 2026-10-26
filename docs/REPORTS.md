# Reports, dealer packets and the publish layer

Everything a stakeholder sees is built by the pipeline, from marts that passed the gates. Nothing is
exported by hand.

| Output | Built by | Where | Audience |
|---|---|---|---|
| Static report site | `python -m reports` (step 10) | `site/` (opens from `file://`, no server) | internal: executive, finance, claims, compliance |
| Excel downloads | each report area | `site/downloads/<area>/` | the same audiences, plus filings |
| Per-dealer packets | `python -m publish` (step 9), copied into the site by the shell | `data/publish/dealers/<tenant>/<dealer>/`, then `site/dealers/<tenant>/<dealer>/` | one dealer each, with its administrator's branding |
| Peer benchmarks | `python -m publish` | `data/publish/*.csv` | every dealer (aggregate and suppressed) |
| Raw-data portal | `python -m portal export --tenant T --dealer D` | stdout or file | one dealer's own contract rows |
| Semantic model | `python -m semantic` (step 8) | `semantic/model/` | Power BI developers |

Reports, publish and semantic run only after dbt, diagnostics and reconcile pass (the gate rule in
`pipeline/run.py`). A failed day leaves yesterday's site in place.

## How the site is assembled (plug-ins)

The shell (`reports/build.py`, `reports/__main__.py`) builds the five V1 pages itself. It then
**discovers report plug-ins**, so nobody edits the shell to add a report:

1. `render_site` clears `site/`, copies the assets, and copies the per-dealer packets from
   `data/publish/dealers/` to `site/dealers/<tenant>/<dealer>/`.
2. For each area in `reports.build.PLUGIN_AREAS` (`regulatory`, `dealer`, `claims`, `finance`,
   `ops`, `actuarial`) that exists, it imports `reports.<area>` and calls
   `build(con, out_dir) -> list[reports.build.Page]` with a **read-only** warehouse connection. The
   plug-in writes its Excel files under `out_dir/downloads/<area>/` and returns its pages.
3. Each area's first page becomes one nav entry, labelled by the module's `NAV_LABEL`. A child page
   sets `Page.nav` to its parent, which keeps the nav item active, and `Page.crumb` for its
   back-link text.
4. Every page renders through the one template (`reports/templates/page.html.j2`). The build then
   fails if any local link or asset does not resolve.

A plug-in that raises fails the whole build with `report plug-in reports.<area> failed: ...` (see
"Other failures" in `operating-model/RUNBOOK.md`). A broken report never ships quietly next to good
ones.

Plug-ins read **marts only** (`core`, `finance`, `claims`, `dealer`, `regulatory`, `actuarial`,
`seeds`), and the Operations page also reads `audit`. They never read raw, staging or intermediate.

## Rules every page follows

1. **Metrics are defined once** (`dbt/models/metrics.yml`, [METRICS.md](METRICS.md)). Pages sum
   additive columns and apply the governed formula. They never average ratios.
2. **Counting rules come from seeds.** The status set, scope and basis of each report line live in
   `dbt/seeds/*/` and are shown on a page (for example `regulatory/report-definitions.html`).
3. **Statements use closed periods.** Finance statements and filings read the latest closed posting
   period, so a published figure never moves. Preliminary figures are labelled as preliminary.
4. **Dealers see peers only through the publish layer.** Peer numbers are aggregate and suppressed.
   A dealer's own numbers come from its packet or its scorecard.
5. **Readable by everyone.** Every chart has a plain-English "what this means" line and a data table
   ("Show data"). Status is always an icon plus a word. Light and dark mode both work.
6. **KPIs stop at the last complete month**, because the as-of month is partial.
7. **Product scope comes from the compliance family and the program type, never from a name.**
   `seeds/regulatory/compliance_product_family.csv` maps every subcategory to VSC, GAP, MAINTENANCE or
   ANCILLARY_OTHER (Limited Warranty is ANCILLARY_OTHER, never VSC); `dim_product.compliance_family`
   and `program_type` carry it. Product names change every plan year and a "- DNU" twin reuses a
   live code, so pages group by code or family. Who backs a contract is `dim_obligor.obligor_kind`:
   `internal_provider` (the provider's own obligors) or `dealer_owned` (a dealer-owned obligor entity).

## Pages

| Page | Question it answers | Owner | Main sources |
|---|---|---|---|
| `index.html` Executive overview | Is the book healthy? T12 written and earned premium, loss and combined ratio, contracts in force, cancel rate, IBNR, dealer watchlist | Reporting Manager | V1 marts (via `marts/core/v1_compat/`), actuarial |
| `products.html` Product performance | Which tier, term and vehicle type makes or loses money? | Reporting Manager | V1 marts, actuarial earning curves |
| `claims.html` Claims & IBNR | What is still to come? Reported vs IBNR, ultimate loss ratio, triangle | Reporting Manager | actuarial, V1 claims trend |
| `dealers.html`, `dealers/<code>.html` Dealer scorecards | How is my dealership doing against its peers? | Reporting Manager | V1 dealer marts, `data/publish/*` |
| `quality.html` Data quality | Can I trust today's numbers? dbt results, freshness, ingest, suppression | Reporting Manager | `data/runs/*.json`, dbt artifacts, publish manifest |
| `ops.html` Operations | Was today on time, gated and reconciled? SLA calendar (07:00), diagnostics with runbook keys, reconciliation, database health (empty serving tables, stale statistics, refresh login used ad hoc), dealer packet index | Reporting Manager | `audit.pipeline_run`, `audit.diagnostics_result`, `audit.reconciliation`, `audit.db_health` |
| `regulatory/index.html` Regulatory filings | Which states need a filing, and what goes in it? | Senior Data Analyst (regulatory) | `regulatory.*`, seeds |
| `regulatory/funded-reserve-<st>.html` | State funded-reserve / provider renewal filing: Section 2 activity, Section 3 computation on both bases, notes. Excel tabs: Summary, ITD and period contracts, ITD and period claims | Senior Data Analyst (regulatory) | `mart_funded_reserve_summary`, `_contracts`, `_claims` |
| `regulatory/market-survey-<st>.html` | State market survey: numbered items by provider x calendar year, with detail tabs | Senior Data Analyst (regulatory) | `mart_market_survey` |
| `regulatory/inforce-list-<st>.html` | Which service contracts are in force in a state today, with lienholder, form code (from the contract document, never inferred) and obligor? Excel tabs: In Force Contracts, By Product, Form Code Coverage | Senior Data Analyst (regulatory) | `mart_state_inforce_list` |
| `regulatory/report-definitions.html` | Which statuses, scope and basis does each line count? | Senior Data Analyst (regulatory) | `seeds.report_definitions`, `report_status_sets`, `state_reserve_method` |
| `dealer/index.html` Dealer ops hub | What each dealer report answers, plus the Excel downloads | Senior Data Analyst (dealer & claims) | `dealer.*` |
| `dealer/production.html` | Dealer x subcategory x month production pivot | same | `mart_dealer_production` |
| `dealer/pending.html` | Pending contracts each dealer still has to finish (fake customer and co-buyer names, flagged PII). The packet workbook has a fixed column list: customer, co-buyer (from the e-contracting feed), product, lienholder, provider and administrator, names in Proper Case, every column filterable | same | `mart_dealer_pending_contracts` |
| `dealer/surcharge-audit.html` | Contracts that should, or should not, carry a vehicle surcharge | same | `mart_surcharge_audit` |
| `dealer/specialty-line.html` | RV (with chassis VIN) and powersports contracts | same | `mart_specialty_line` |
| `dealer/remittance-aging.html` | Remittance age bands and late fees per dealer | same | `mart_remittance_aging` |
| `claims/kpi.html` Claims KPIs | Frequency, severity, denial rate, early claims (30-180 days), open claims aged 30+, top components, turnaround | Senior Data Analyst (dealer & claims) | `claims.mart_claims_kpi`, `mart_claim_summary`, `mart_claim_components` |
| `downloads/claims/claims_header.xlsx`, `claims_detail.xlsx`, `claim_components.xlsx`, `claim_payees.xlsx` | Row-level claims exports, one per question, same columns every month; linked from the Claims KPIs page | Senior Data Analyst (dealer & claims) | `claims.mart_claim_summary`, `mart_claim_payment_lines`, `mart_claim_component_lines`, `mart_claim_payees` |
| `finance/index.html` Finance | Hub: statement period, headline ratios, the Excel pack | Financial Analysts | `finance.*` |
| `finance/cession.html` | Cession / experience per participation entity: earned reserve, claims, loss ratio, reserve balance, profit share; run-off details and transfer summary | same | `mart_cession_experience`, `mart_participation_contract` |
| `finance/profit-share.html` | Profit-share statement: opening due, movement, closing due, funding gap | same | `mart_profit_share_statement` |
| `finance/commissions.html` | Commission statement: earned, chargebacks, advances, net, advance balance | same | `mart_commission_statement` |
| `finance/pnl.html` | P&L by product: written to earned to margin, loss and combined ratio | same | `mart_pnl_by_product` |
| `finance/remittance.html` | Remittance aging, the dealer late-fee bill and the partner settlement statement | same | `mart_remittance_late_fees`, `mart_partner_settlement` |
| `finance/controls.html` | Close controls: cost split vs transaction totals, reserve roll-forward, reserve bridge (New Reserves to Experience), booked cost vs rate card | same | `mart_disbursement_control_totals`, `mart_reserve_rollforward`, `mart_reserve_bridge`, `mart_rate_card_exceptions` |
| `finance/experience.html` | Experience with a View by switch: net written, unearned, earned, claims, loss ratio, contracts, earned % | same | `mart_experience` |
| `finance/new-reserves.html` | New reserves by product category x term-year bucket: average and % mix | same | `mart_new_reserves_by_term` |
| `finance/commission-hierarchy.html` | Commission District > Regional > National against volume targets | same | `mart_commission_hierarchy` |
| `actuarial.html` Actuarial | Earning curves, IBNR by cohort, loss-ratio forecast and backtest, early-claim emergence | Data Scientist | schema `actuarial` |

The nav lists the five V1 pages first, then Regulatory, Dealer ops, Claims KPIs, Finance,
Operations and Actuarial.

## Per-dealer packets

`python -m publish` writes one packet per reportable dealer:
- production, pending contracts, remittance aging and claims KPI, as CSV files;
- one Excel workbook;
- an HTML page branded from `dim_tenant`.

Every packet is filtered through the same user -> dealer, tenant access map as the semantic RLS
table (`core.dim_user_dealer_access`). A test in `tests/test_publish.py` proves that a packet holds
only its own dealer's rows. The report shell copies the packets to
`site/dealers/<tenant>/<dealer>/index.html`, and the Operations page lists them all.

## Publish layer: peer benchmarks (`publish/`)

| File | Grain | Population |
|---|---|---|
| `peer_benchmark_monthly.csv` | state x coverage tier x term band x month, each with an `ALL` margin | VSC tiers, known dealer, complete months only |
| `peer_benchmark_t12.csv` | the same cells, trailing 12 complete months | the same |
| `manifest.json` | rules, last published month, row and suppression counts | |

**Suppression** (`publish/suppression.py`):

1. **Primary.** A cell is hidden if it has fewer than 11 contracts, fewer than 5 dealers, or one
   dealer holds more than 25% of its contracts or earned premium.
2. **Complementary.** No margin group may have exactly one hidden cell. If one does, the smallest
   visible member is hidden too.
3. **Exact-disclosure audit.** The layer solves the published equations as a linear system (one
   null-space calculation per period). It keeps hiding cells until no hidden cell can be solved for.

Hidden cells keep their dimensions and `suppression_reason`, and their measures are blank. With 51
states x 4 tiers and 150 dealers, most state-level cells are suppressed: about 95% of benchmark
cells. Dealer pages therefore fall back to national cells. A region-level view is the proposed next
step. Lowering the rules is a commercial and legal decision, not a code change.

## Power BI concept -> this project

| Power BI concept | Here |
|---|---|
| Semantic model and measures | `semantic/`, generated from `metrics.yml`: domain models (Sales, Claims, Finance, Compliance) and one time-intelligence calculation group |
| Row-level security | `core.dim_user_dealer_access` (user -> dealer, tenant). The same map filters dealer packets and the portal. Postgres enforces it in `warehouse/postgres/rls.sql`, and Snowflake through row access policies |
| Report pages and apps | One HTML page per question: the internal site, plus per-dealer packets. Audiences and roles are in `docs/ACCESS_MATRIX.md` |
| Scheduled refresh | `pipeline.run` daily, with reports by 07:00 ([operating-model/SLA.md](operating-model/SLA.md)) |
| Paginated / Excel exports | openpyxl workbooks under `site/downloads/<area>/` |

## Adding a report

1. File an intake request ([operating-model/INTAKE_FORM.md](operating-model/INTAKE_FORM.md)).
2. Build the mart in your area folder, with its metric test.
3. Add the pages in `reports/<area>/__init__.py` (`build(con, out_dir)`, `NAV_LABEL`). A new area
   is one more entry in `PLUGIN_AREAS`.
4. Pass the peer-review checklist in [operating-model/RELEASE.md](operating-model/RELEASE.md), then
   add the page to the table above.
