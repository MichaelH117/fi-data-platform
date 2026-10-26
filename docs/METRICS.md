# Metrics

**One definition per metric.** Each formula is written once, in `dbt/models/metrics.yml`
(MetricFlow semantic models plus metrics). This page explains those formulas in plain words. The
catalog at the bottom is generated from the YAML, so it cannot drift. The Power BI-style semantic
model (`python -m semantic`) builds its measures from the same file. Marts may precompute a metric,
but each area keeps a dbt test that checks its mart value against the definition
(`dbt/tests/<area>/`, and `dbt/tests/reconciliation/recon_loss_ratio_math.sql` for the loss ratio).

Changing a definition is a release ([operating-model/RELEASE.md](operating-model/RELEASE.md)):
metrics.yml, this page and the mart test all change in the same pull request, and the Senior Power
BI Specialist signs it off.

## Rules every metric follows

1. **Sum, then divide.** A ratio at any grain is `sum(numerator) / sum(denominator)` over that grain.
   Never average ratios.
2. **Time variants are not metrics.** MTD, QTD, YTD, prior year, YoY % and trailing-12 come from the
   semantic model's one time-intelligence calculation group. Nobody adds a `*_ytd` copy. The V1
   `t12_*` entries are kept for the V1 pages and are tagged in the YAML as calculation-group items.
3. **Two time bases, never mixed in one ratio:**

   | Basis | Time key | Use it for | Example metrics |
   |---|---|---|---|
   | **Posting period** (ledger) | `posting_period_key` (yyyymm of the *post date*) | finance, statements, filings, anything that must tie to accounting | `posted_loss_ratio`, `claims_paid`, `remitted_cash`, `earned_reserve` |
   | **Accident / earning month** (actuarial) | month of loss or month earned | pricing, reserving, loss trends | `loss_ratio`, `t12_loss_ratio`, `claim_frequency` |

   Closed periods are locked. A late event posts to the first open period and keeps its true event
   date (`int_posting_periods`). Earned facts are never restated after close.
4. **Reportable scope only.** Demo dealers and test-range contracts are removed once, in
   `int_reportable_scope`. No metric filters them again.
5. **Counting rules are seeds.** Which statuses count (in force only, or written = pending + in
   force + cancelled + expired), the scope (ITD to period end, or within the period) and the money
   basis (coverage cost, or retail) come from `seeds/regulatory/report_definitions.csv`,
   `report_status_sets.csv` and the area seeds, never from code comments.
6. **Claims come from one fact.** Every claims metric derives from `core.fct_claim_payment_line`
   (paid, incurred change, case-reserve change) or its status history `core.fct_claim_history`.
   Claim amounts are always positive after conforming.

## Premium and earning

| Term | Definition | Source |
|---|---|---|
| Coverage cost (dealer cost) | What the dealer remits for the contract, split into admin fee, insurer fee, premium tax, commission, claims reserve and profit-share reserve | `core.fct_disbursement` (the split always ties to its transaction; calibration test) |
| Claims reserve ("premium" for loss ratios) | The `CLAIMS_RESERVE` part of the cost split, which funds claims | `fct_disbursement.reserve_amount` |
| Gross consideration | Coverage cost or retail price written in the period, by basis | `fct_gross_premium` (`gross_coverage_cost`, `gross_retail_price`) |
| Earned % | Pro-rata by days in force over the term (default). Rule of 78s is available with dbt var `earning_method: r78` | `int_disbursements_earned` |
| Earned reserve | Claims reserve x earned % for the period | `fct_earned_disbursement.earned_reserve` |
| Unearned premium (month end) | A balance: sum across contracts, never across months | `fct_contract_monthly.unearned_premium_eom` |
| Dealer markup | Retail price - coverage cost | `dealer_markup_written` |
| Earned % (Experience page, metric `earned_pct`) | Earned claims reserve / net claims reserve written, inception to date | `finance.mart_experience.earned_pct` |
| Cancel % (Experience page, metric `cancel_pct`) | Contracts effectively cancelled / contracts written (entered-month cohort on the page) | `finance.mart_experience.cancel_pct` |
| New reserves | Gross claims reserve booked at sale, by entered month, before cancels; the reserve bridge walks it to net written | `finance.mart_new_reserves_by_term`, `mart_reserve_bridge` |

Caveat: pro-rata earns a VSC too fast early in the term, because most claims come late. Young books
therefore read low on any loss ratio over earned premium. The actuarial module (`docs/ACTUARIAL.md`)
compares curves and estimates IBNR.

## Loss ratios

| Metric | Formula | Basis | Notes |
|---|---|---|---|
| `loss_ratio` | incurred claims / earned premium | accident month | V1 actuarial view. Incurred = paid + case reserve, with no IBNR |
| `t12_loss_ratio` | sum of the last 12 months' incurred / sum of the last 12 months' earned | accident month | Dealer watchlist threshold 80% (var `high_loss_ratio_threshold`) |
| `posted_loss_ratio` | claims incurred as posted / earned reserve | posting period | Ledger view, used by finance and cession statements |
| Cession loss ratio | incurred / earned reserve, per participation entity | posting period | `finance.mart_cession_experience` |
| Combined ratio (P&L) | (incurred + earned acquisition cost + earned admin fee) / earned premium | posting period | `finance.mart_pnl_by_product`. Not in metrics.yml yet (see open items) |
| Combined ratio (V1 overview) | T12 loss ratio + (admin fee + commission written) / reserve written | trailing 12 | Executive overview page. A second definition, so it is an open item |

The denominator is always **earned**, never written. A ratio over written premium would look far too
good on a growing book.

## Claims

| Metric | Formula | Notes |
|---|---|---|
| `claims_paid` | sum of `paid_amount` posted in the period | always >= 0 (`diag_claim_sign`) |
| `claims_incurred` | paid + change in case reserve, as posted | |
| `claims_opened`, `claims_denied`, `claims_closed_paid` | claims (one row per claim, both admin systems) in `claims.mart_claim_summary`: opened by reported month, denied / paid by loss month | status history (`fct_claim_history`) starts at the 2023 cutover, so it is never used for counts |
| `denial_rate` | denied / reported, loss month | same as `mart_claims_kpi.denial_rate` |
| `paid_claim_severity` | paid on paid claims / paid claims, loss month | same population top and bottom; same as `mart_claims_kpi.claim_severity` |
| `claim_frequency` | non-denied claims / earned contract-years | accident month |
| Early claims | claims with a loss 30, 60, 90, 120, 150 or 180 days after the effective date | flags in `dim_contract_claim_summary`; KPI cube `claims.mart_claims_kpi` |
| Open aged 30+ | open claims reported more than 30 days ago | `dim_claim.aging_30_plus` |

## Contract statuses and which reports count them

Eleven statuses (`generator/lifecycle.py` `CONTRACT_STATUSES`, transitions in `TRANSITIONS`). Report
membership comes from the seeds `report_status_sets.csv` (regulatory: IN_FORCE, WRITTEN, CONTRACT_ANY)
and `dealer_report_status_sets.csv` (dealer reports); a status a seed does not list is not counted.
The three hold statuses never move money, so a contract on hold keeps counting where it counted before.

| Status | Reached from | IN_FORCE | WRITTEN | CONTRACT_ANY | Dealer production | Dealer pending | Specialty line, surcharge audit |
|---|---|---|---|---|---|---|---|
| `PENDING` | booking | - | yes | yes | yes | yes | yes |
| `INFORCE` | PENDING (remitted), CANCELLED (reinstated) | yes | yes | yes | yes | - | yes |
| `CANCELLED` | INFORCE | yes | yes | yes | yes | - | yes |
| `EXPIRED` | INFORCE | yes | yes | yes | yes | - | yes |
| `VOIDED` | PENDING (voided, or never remitted) | - | - | yes | - | - | - |
| `DENIED` | PENDING | - | - | yes | - | - | - |
| `SUSPENDED` | INFORCE (account hold) | yes | yes | yes | yes | - | yes |
| `REINSTATEMENT_PENDING` | CANCELLED (reinstatement requested) | yes | yes | yes | yes | - | yes |
| `REINSTATEMENT_SUSPENDED` | REINSTATEMENT_PENDING (documents missing) | yes | yes | yes | yes | - | yes |
| `REINSTATEMENT_VOIDED` | CANCELLED | - | - | yes | - | - | - |
| `REINSTATEMENT_DENIED` | CANCELLED | - | - | yes | - | - | - |

Dealer production also splits its written contracts into pending, in force and cancelled with the
seed rules `DEALER_PROD_PENDING`, `DEALER_PROD_IN_FORCE` and `DEALER_PROD_CANCELLED`: a hold counts
with the status it came from (`SUSPENDED` in force; `REINSTATEMENT_PENDING` and
`REINSTATEMENT_SUSPENDED` cancelled until the reinstatement is approved).

## Cancellations and reinstatements

| Metric | Formula | Notes |
|---|---|---|
| `cancellations`, `effective_cancellations` | cancels posted; effective = not later reinstated | |
| `effective_cancel_rate` | effective cancels / contracts written, same period | Period view. The V1 `cancel_rate` is by sale cohort ("so far") |
| `customer_refunds`, `cancel_fees`, `dealer_chargebacks`, `coverage_cost_refunds` | columns of `fct_cancellation` | method: PRORATED, FULL_REFUND or R78 |
| `net_coverage_cost` | gross coverage cost - coverage-cost refunds | |
| `reinstatements`, `reinstatement_reversals` | `fct_reinstatement` | |

## Remittance, commissions and dealer operations

| Metric | Formula | Notes |
|---|---|---|
| `remitted_cash`, `remittances` | cash received, posted | entered -> sent -> received lifecycle |
| `late_remittance_rate` | late remittances / all remittances | late = received more than `remit_late_days` (30) after entry |
| `late_fees` | late remittance amount x `remit_late_fee_pct` (1.5%) | |
| `commissions`, `commission_advances` | `fct_commission` | the statement nets advances and chargebacks (`finance.mart_commission_statement`) |
| Production | contracts written per dealer x subcategory x month | `dealer.mart_dealer_production` |

## Regulatory

The funded-reserve filing and market survey are **line engines**, not free-form metrics. Each line
item is a seed row in `report_definitions.csv` (status set, scope, basis, filters).
`regulatory.mart_regulatory_line_values` evaluates every line for every window and provider. The
Section 3 computation is: reserve = state method rate x net consideration, from
`state_reserve_method.csv` (all 50 states + DC; states without a requirement are skipped).

## Open governance items

- **Remittance aging is defined twice.** `dealer.mart_remittance_aging` (age bands by posting
  period) and `finance.mart_remittance_late_fees` (seed `remit_aging_buckets`, days from entry to
  receipt) both bill late fees. They must use one band seed and one late-fee rule, or show that they
  tie. Owners: Senior Data Analyst (dealer & claims) and Financial Analyst II.
- **Combined ratio has two formulas**: the finance P&L (earned basis) and the V1 executive overview
  (written-expense basis). The P&L formula should become the one `metrics.yml` entry, and the
  overview should switch to it. The cession loss ratio is also computed in a mart but is not yet a metric.
- The V1 semantic models (`contract_month`, `contract_sale`, `claim`) still read
  `marts/core/v1_compat/`. They are retired when the V1 report pages move to `core`.

## Metric catalog (generated)

<!-- metric-catalog:start -->
<!-- Generated by `python -m reports.dictionary` from dbt/models/metrics.yml. Do not edit. -->

| Metric | Label | Type | Definition | Source fact | Folder | Owner | Certified |
|---|---|---|---|---|---|---|---|
| `written_premium` | Written premium | simple | `sum(written_premium)` | `fct_contract_monthly` | Premium | Data Scientist |  |
| `earned_premium` | Earned premium (pro-rata) | simple | `sum(earned_premium)` | `fct_contract_monthly` | Premium | Data Scientist |  |
| `incurred_claims` | Incurred claims | simple | `sum(incurred_claims)` | `fct_contract_monthly` | Claims | Data Scientist |  |
| `loss_ratio` | Loss ratio (on earned premium) | ratio | `incurred_claims / earned_premium` |  | Ratios | Data Scientist | yes |
| `t12_earned_premium` | Earned premium, trailing 12 months | cumulative | `sum(earned_premium) over 12 months` | `fct_contract_monthly` |  | Data Scientist |  |
| `t12_incurred_claims` | Incurred claims, trailing 12 months | cumulative | `sum(incurred_claims) over 12 months` | `fct_contract_monthly` |  | Data Scientist |  |
| `t12_loss_ratio` | Loss ratio (on earned premium), trailing 12 months | derived | `t12_incurred_claims / t12_earned_premium` |  |  | Data Scientist |  |
| `claim_frequency` | Claim frequency (claims per earned contract-year) | ratio | `claim_count_metric / exposure_years_metric` |  | Ratios | Data Scientist | yes |
| `claim_count_metric` | Claims (non-denied) | simple | `sum(claim_count)` | `fct_contract_monthly` | Claims | Data Scientist |  |
| `exposure_years_metric` | Earned contract-years | simple | `sum(exposure_years)` | `fct_contract_monthly` | Exposure | Data Scientist |  |
| `paid_claim_count` | Paid claims | simple | `sum(case when claim_status = 'paid' then 1 else 0 end)` | `fct_claim` |  | Senior Data Analyst (dealer & claims) |  |
| `claim_paid_amount` | Claim dollars paid | simple | `sum(paid_amount)` | `fct_claim` |  | Senior Data Analyst (dealer & claims) |  |
| `claim_severity` | Claim severity (paid per paid claim) | ratio | `claim_paid_amount / paid_claim_count` |  |  | Senior Data Analyst (dealer & claims) |  |
| `contracts_sold` | Contracts sold | simple | `count(contract_id)` | `fct_contract_sale` |  | Senior Data Analyst (dealer & claims) |  |
| `contracts_cancelled` | Contracts cancelled | simple | `sum(case when is_cancelled then 1 else 0 end)` | `fct_contract_sale` |  | Senior Data Analyst (dealer & claims) |  |
| `cancel_rate` | Cancel rate (cancelled / sold, by sale date) | ratio | `contracts_cancelled / contracts_sold` |  |  | Senior Data Analyst (dealer & claims) | yes |
| `contracts_written` | Contracts written | simple | `sum(contracts_written)` | `fct_gross_premium` | Sales | Senior Data Analyst (dealer & claims) | yes |
| `gross_coverage_cost` | Gross coverage cost | simple | `sum(coverage_cost)` | `fct_gross_premium` | Sales | Senior Data Analyst (dealer & claims) | yes |
| `gross_retail_price` | Gross retail price | simple | `sum(retail_price)` | `fct_gross_premium` | Sales | Senior Data Analyst (dealer & claims) |  |
| `gross_purchase_price` | Gross purchase price | simple | `sum(purchase_price)` | `fct_gross_premium` | Sales | Senior Data Analyst (dealer & claims) |  |
| `avg_coverage_cost` | Average coverage cost per contract written | ratio | `gross_coverage_cost / contracts_written` |  | Sales | Senior Data Analyst (dealer & claims) |  |
| `dealer_markup_written` | Dealer markup on written business (retail - coverage cost) | derived | `gross_retail_price - gross_coverage_cost` |  | Sales | Senior Data Analyst (dealer & claims) | yes |
| `contracts_in_force` | Contracts in force (month end) | simple | `sum_boolean(is_in_force_eom)` | `fct_contract_monthly` | Portfolio | Senior Data Analyst (dealer & claims) | yes |
| `unearned_premium_balance` | Unearned premium (month end) | simple | `sum(unearned_premium_eom)` | `fct_contract_monthly` | Premium | Financial Analyst II |  |
| `cancellations` | Cancellations | simple | `count(cancel_key)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `effective_cancellations` | Effective cancellations | simple | `sum_boolean(is_effective_cancel)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `customer_refunds` | Customer refunds | simple | `sum(customer_refund)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `cancel_fees` | Cancel fees | simple | `sum(cancel_fee)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `dealer_chargebacks` | Dealer chargebacks | simple | `sum(dealer_portion)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `coverage_cost_refunds` | Coverage cost refunds | simple | `sum(coverage_cost_refund)` | `fct_cancellation` | Cancellations | Financial Analyst II |  |
| `effective_cancel_rate` | Effective cancel rate (effective cancels / contracts written, same period) | ratio | `effective_cancellations / contracts_written` |  | Cancellations | Financial Analyst II |  |
| `net_coverage_cost` | Net coverage cost (gross - coverage cost refunds) | derived | `gross_coverage_cost - coverage_cost_refunds` |  | Sales | Senior Data Analyst (dealer & claims) |  |
| `reinstatements` | Reinstatements | simple | `count(reinstatement_key)` | `fct_reinstatement` | Cancellations | Financial Analyst II |  |
| `reinstatement_reversals` | Reinstatement reversals | simple | `sum(reversal_amount)` | `fct_reinstatement` | Cancellations | Financial Analyst II |  |
| `claims_paid` | Claims paid | simple | `sum(paid_amount)` | `fct_claim_payment_line` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_incurred` | Claims incurred (posted) | simple | `sum(incurred_change)` | `fct_claim_payment_line` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_reserve_change` | Change in case reserve | simple | `sum(pending_amount)` | `fct_claim_payment_line` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_with_activity` | Claims with posted activity | simple | `count_distinct(claim_key)` | `fct_claim_payment_line` | Claims | Senior Data Analyst (dealer & claims) |  |
| `avg_claim_process_days` | Average claim process days | simple | `average(process_days)` | `fct_claim_payment_line` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_opened` | Claims opened (reported month) | simple | `count(claim_key)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_reported_loss_month` | Claims reported (loss month) | simple | `count(claim_key)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) | yes |
| `claims_denied` | Claims denied (loss month) | simple | `sum(case when status_group = 'denied' then 1 else 0 end)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) |  |
| `claims_closed_paid` | Paid claims (loss month) | simple | `sum(case when status_group = 'paid' then 1 else 0 end)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) |  |
| `paid_on_paid_claims` | Claim dollars paid on paid claims (loss month) | simple | `sum(case when status_group = 'paid' then paid_amount else 0 end)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) |  |
| `open_claims` | Open claims (as of date) | simple | `sum(case when status_group = 'open' then 1 else 0 end)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) | yes |
| `open_claims_aged_30_plus` | Open claims aged 30+ (as of date) | simple | `sum_boolean(is_aged_open)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) | yes |
| `avg_claim_turnaround_days` | Average turnaround days (reported to closed) | simple | `average(turnaround_days)` | `mart_claim_summary` | Claims | Senior Data Analyst (dealer & claims) | yes |
| `collected_deductibles` | Collected deductibles (admin core status history) | simple | `sum(collected_deductible)` | `fct_claim_history` | Claims | Senior Data Analyst (dealer & claims) |  |
| `denial_rate` | Denial rate (denied / reported, loss month) | ratio | `claims_denied / claims_reported_loss_month` |  | Ratios | Senior Data Analyst (dealer & claims) | yes |
| `paid_claim_severity` | Paid severity (paid on paid claims / paid claims, loss month) | ratio | `paid_on_paid_claims / claims_closed_paid` |  | Ratios | Senior Data Analyst (dealer & claims) | yes |
| `posted_loss_ratio` | Loss ratio (on claims reserve), posted | ratio | `claims_incurred / earned_reserve` |  | Ratios | Financial Analyst II | yes |
| `earned_pct` | Earned % | ratio | `earned_reserve / disbursed_claims_reserve` |  | Ratios | Financial Analyst II |  |
| `cancel_pct` | Cancel % | ratio | `effective_cancellations / contracts_written` |  | Ratios | Financial Analyst II |  |
| `remitted_cash` | Remitted cash | simple | `sum(amount)` | `fct_cash_transaction` | Remittance | Financial Analyst II |  |
| `remittances` | Remittances | simple | `count(cash_txn_key)` | `fct_cash_transaction` | Remittance | Financial Analyst II |  |
| `late_remittances` | Late remittances | simple | `sum_boolean(is_late)` | `fct_cash_transaction` | Remittance | Financial Analyst II |  |
| `late_fees` | Late fees | simple | `sum(late_fee)` | `fct_cash_transaction` | Remittance | Financial Analyst II |  |
| `avg_remit_age_days` | Average remit age (days) | simple | `average(remit_age_days)` | `fct_cash_transaction` | Remittance | Financial Analyst II |  |
| `late_remittance_rate` | Late remittance rate (late / all remittances) | ratio | `late_remittances / remittances` |  | Remittance | Financial Analyst II |  |
| `disbursed_amount` | Disbursed amount | simple | `sum(amount)` | `fct_disbursement` | Disbursements | Financial Analyst II |  |
| `disbursed_claims_reserve` | Claims reserve disbursed | simple | `sum(reserve_amount)` | `fct_disbursement` | Disbursements | Financial Analyst II |  |
| `disbursed_profit_share_reserve` | Profit-share reserve disbursed | simple | `sum(profit_share_reserve)` | `fct_disbursement` | Disbursements | Financial Analyst II |  |
| `earned_disbursement_amount` | Earned disbursements | simple | `sum(earned_amount)` | `fct_earned_disbursement` | Disbursements | Financial Analyst II | yes |
| `earned_reserve` | Earned reserve | simple | `sum(earned_reserve)` | `fct_earned_disbursement` | Disbursements | Financial Analyst II | yes |
| `earned_admin_fee` | Earned admin fee | simple | `sum(case when disbursement_type = 'ADMIN_FEE' then earned_amount else 0 end)` | `fct_earned_disbursement` | Disbursements | Financial Analyst II |  |
| `earned_acquisition_cost` | Earned acquisition cost (commission + insurer fee + premium tax) | simple | `sum(case when disbursement_type in ('COMMISSION', 'INSURER_FEE', 'PREMIUM_TAX') then earned_amount else 0 end)` | `fct_earned_disbursement` | Disbursements | Financial Analyst II |  |
| `combined_ratio` | Combined ratio (claims + acquisition + admin fee, on earned premium) | derived | `(claims_incurred + earned_acquisition_cost + earned_admin_fee) / earned_disbursement_amount` |  | Ratios | Financial Analyst II | yes |
| `scorecard_claims_reported` | Claims reported (dealer scorecard) | simple | `sum(claims_reported)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `early_claims` | Early claims (headline window) | simple | `sum(early_claims)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `early_claim_rate` | Early-claim rate (early claims / claims reported, headline window) | ratio | `early_claims / scorecard_claims_reported` |  | Dealer Scorecard | Senior Data Analyst (dealer & claims) | yes |
| `dealers_on_watch` | Dealers on watch (month end) | simple | `sum_boolean(is_watch)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) | yes |
| `dealers_on_loss_ratio_watch` | Dealers on loss-ratio watch (month end) | simple | `sum_boolean(is_watch_loss_ratio)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) | yes |
| `scorecard_contracts_written` | Contracts written (dealer scorecard, entered month) | simple | `sum(contracts_written)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_contracts_cancelled` | Contracts cancelled (dealer scorecard, sale cohort) | simple | `sum(contracts_cancelled)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_earned_claims_reserve` | Earned claims reserve (dealer scorecard) | simple | `sum(earned_claims_reserve)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_incurred_claims` | Incurred claims (dealer scorecard, loss month) | simple | `sum(incurred_claims)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_remittances` | Remittances (dealer scorecard) | simple | `sum(remittances)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_late_remittances` | Late remittances (dealer scorecard) | simple | `sum(late_remittances)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_days_late_total` | Total days late (dealer scorecard) | simple | `sum(days_late_total)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_loss_ratio` | Dealer loss ratio (on earned claims reserve, dealer scorecard) | ratio | `scorecard_incurred_claims / scorecard_earned_claims_reserve` |  | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_cancel_rate` | Cancel rate (dealer scorecard, cancelled / written by entered month) | ratio | `scorecard_contracts_cancelled / scorecard_contracts_written` |  | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_late_remit_rate` | Late remittance rate (dealer scorecard) | ratio | `scorecard_late_remittances / scorecard_remittances` |  | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `scorecard_avg_days_late` | Average days late (dealer scorecard, late remittances only) | ratio | `scorecard_days_late_total / scorecard_late_remittances` |  | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `loss_ratio_peer_percentile` | Loss-ratio peer percentile (dealer scorecard, month end) | simple | `max(t12_loss_ratio_peer_pct)` | `mart_dealer_scorecard` | Dealer Scorecard | Senior Data Analyst (dealer & claims) |  |
| `calculated_reserve` | Calculated reserve (funded-reserve filing) | simple | `sum(case when is_default_basis then calculated_reserve else 0 end)` | `mart_funded_reserve_summary` | Filings | Senior Data Analyst (regulatory) | yes |
| `current_reserve` | Current reserve held (funded-reserve filing) | simple | `sum(case when is_default_basis then current_reserve else 0 end)` | `mart_funded_reserve_summary` | Filings | Senior Data Analyst (regulatory) |  |
| `reserve_shortfall` | Reserve shortfall (funded-reserve filing) | simple | `sum(case when is_default_basis then reserve_shortfall else 0 end)` | `mart_funded_reserve_summary` | Filings | Senior Data Analyst (regulatory) | yes |
| `complaints_received` | Complaints received (funded-reserve filing) | simple | `sum(case when is_default_basis then complaints_received else 0 end)` | `mart_funded_reserve_summary` | Filings | Senior Data Analyst (regulatory) | yes |
| `filing_line_value` | Filing line value (one report line) | simple | `sum(value)` | `mart_regulatory_line_values` | Filings | Senior Data Analyst (regulatory) |  |
| `commissions` | Commissions | simple | `sum(amount)` | `fct_commission` | Commissions | Financial Analyst II |  |
| `commission_advances` | Commission advances | simple | `sum(case when is_advance then amount else 0 end)` | `fct_commission` | Commissions | Financial Analyst II |  |

<!-- metric-catalog:end -->
