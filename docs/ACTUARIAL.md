# Actuarial methods

`actuarial/` turns contracts and claims into the numbers an actuary signs off on:
- **Earned premium**: how much of the money collected has been "used up", under five earning methods.
- **Unearned premium reserve**.
- **IBNR**: claims that have already happened but have not been reported yet. It is kept apart from the losses still to come on coverage that has not run yet.
- **Early-claim emergence** at 30 to 180 days.
- **Quarterly loss-ratio forecast**, with a time-split backtest.

The math is in pure modules with no I/O (`earning.py`, `reserving.py`, `emergence.py`,
`forecast.py`). Every formula is unit tested against hand-worked examples and a textbook triangle
(`tests/test_actuarial.py`).

```
python -m actuarial [--warehouse PATH] [--as-of YYYY-MM-DD] [--tail 1.0] [--strict-fit] [--write-docs]
```

`--write-docs` rewrites the generated figures block in section 6 of this file from the run's
`run_summary`, so the quoted IBNR, runoff and forecast figures never drift from the code.

It runs as pipeline step 7 (after the dbt, diagnostics and reconcile gates) in about 5 seconds.

## Inputs (marts only)

| Input | From | Notes |
|---|---|---|
| Contracts | `core.dim_contract` + `dim_contract_terms` + `dim_contract_finance` + `dim_contract_vehicle` | Reportable rows only. Demo and test rows are excluded once, upstream. **Premium** is the claims-reserve part of dealer cost (`reserve_amount`), which is the money that actually pays claims. Earning starts at `effective_date`. |
| Cancellations | `dim_contract.cancel_date` (when `is_cancelled`) and the `fct_cancellation` odometer | A reinstated contract is not treated as cancelled. |
| Claims | `core.dim_claim` | Loss date, reported date, odometer at loss, status group. |
| Claim money | `core.fct_claim_payment_line`, the only claims fact | **Incurred** = `incurred_change` (paid plus the change in the case reserve). **Paid** = `paid_amount`. Each is dated by `posted_at`. |

## Outputs (schema `actuarial`)

| Table | Grain | What it holds |
|---|---|---|
| `earned_premium` | posting period × product cell (category, subcategory, product_code, new/used, term) × method | written, earned, UPR at month end, in force, incurred (by loss month), loss ratio |
| `earning_method_assignment` | category × subcategory × new/used | method, custom curve, contracts, average miles per month, rationale |
| `earning_fit` | method cell (category × subcategory × new/used) and family (category × new/used, subcategory `*`) × term decile | share of lifetime claims emerged vs share of premium earned (selected and the four standard methods), gap, A/E, KS distance, noise band, best-fitting method, `status` |
| `loss_triangle` | segment × basis (`reported`, `paid`, `count`) × effective-quarter cohort × age | incremental and cumulative |
| `development_factors` | segment × basis × age | age-to-age factor, CDF, `cohorts_in_factor` (how many cohorts fitted the link), `tail` and `tail_source`, `max_term_months`, `observed_months` |
| `ibnr_by_cohort` | segment × cohort | written premium, premium to date (net of cancellations so far), premium retention CDF, ultimate (net) premium; paid, reported and claim count; CL, BF and paid-CL ultimates; selected ultimate; `selected_ibnr` (true IBNR); `unexpired_risk_losses`; ultimate loss ratio; `tail`, `tail_source`, `term_elapsed_share`, `elr_source`. ALL rows are the sum of the segments (`method = sum_of_segments`), with the pooled-triangle `all_triangle_ultimate` as a diagnostic |
| `accident_quarter` / `accident_triangle` | loss quarter | reported, CDF, ultimate, IBNR; loss ratio on the selected and pro-rata earned premium |
| `early_claim_emergence` | cohort × category × subcategory × new/used × window | exposed contracts; early claims (excluding denied, and including denied); claims inside the waiting period; rate |
| `early_claim_dealer` | dealer × window (90, 180) | observed vs expected early claims, A/E, z-score, flag |
| `loss_ratio_forecast` | quarter | history (`actual`) plus 4 forecast quarters for each model; the selected forecast and its band; per forecast step the scored points, raw step RMSE and the band RMSE |
| `forecast_backtest` | model | MAE, RMSE, bias, scored points, `points_by_step`, `is_selected` |
| `valuation_view` | segment × method | inception to the valuation date: earned premium, incurred (losses dated and posted by then), loss ratio, UPR at the valuation month end |
| `premium_deficiency` | segment | selected UPR, losses on unexpired coverage, claims-handling expense, unexpired losses / UPR, PDR amount and flag |
| `run_summary` | metric | control totals: lines in vs triangle, lines with no contract and with a loss before the sale (`incurred_excluded_loss_before_sale`, `claims_excluded_*`), the selected ultimate as sum of segments vs pooled triangle, IBNR (and `ibnr_source_<system>`), `factor_start_quarter`, `accident_link_age1`, the selected model and its next-quarter forecast; the valuation-date ITD figures, `upr_selected`, `unexpired_to_upr_ratio`; report-lag stats, `ibnr_days_of_incurred`; the IBNR back-test on new claims only (`ibnr_runoff_*`, with the expected-to-date A/E and `ibnr_runoff_flag`) and case-reserve development on known claims (`case_development_known_*`); the earning-fit gate (`earning_fit_*`) |

Segments are `ALL` plus each product category (VSC, GAP, ANCILLARY, ...). The segments are fitted
separately, so their sum is not exactly `ALL`.

**For the report shell.** The site page is `actuarial/report.py`, exposed as the plug-in
`reports/actuarial/`. It writes `site/actuarial.html` plus CSV downloads in
`site/downloads/actuarial/`. For one release, the V1 tables also stay alive as **views** in
`gold_actuarial`: `earned_premium_by_curve` (three V1 curves, with V1 product codes), `loss_triangle`,
`ibnr_by_cohort` and `development_factors` (all segment `ALL`). The existing claims and products pages
keep working unchanged.

---

## 1. Earning methods

Picture a 12-month contract that cost $1,200. After 3 months, how much of that $1,200 has the company
"earned"? It depends on when you think the claims happen.

Notation: `n` = term in months, `k` = months elapsed, `E(k)` = fraction earned.

| Method | Formula `E(k)` | After 3 of 12 months | Shape | When it fits |
|---|---|---|---|---|
| Pro-rata | `k / n` | 25.0% | straight line | risk spread evenly; usually the legal basis for refunds |
| Pro-rata, mileage-aware | `max(k, n × miles_used / term_miles) / n` | 25% or more | straight line, faster for heavy drivers | time-and-mileage terms ("whichever comes first") |
| Rule of 78s | `k(2n − k + 1) / (n(n + 1))` | 42.3% | front-loaded | risk highest early (GAP: the loan balance falls over time) |
| Reverse Rule of 78s | `k(k + 1) / (n(n + 1))` | 7.7% | back-loaded | claims come late |
| Custom curve | piecewise-linear `interp(k / n, term_pct, earned_pct)` | per curve | any | a curve fitted to one product and new/used cell |

The name comes from 1 + 2 + … + 12 = 78. The two R78 rules mirror each other:
`R78(k) = 1 − reverseR78(n − k)`.

**Mileage-aware earning.** Miles used = the contract's miles per month × `k`. Miles per month comes
from odometer readings after the sale: every claim's `odometer_at_loss` and the cancellation odometer,
each compared with the sale odometer and the months elapsed. Readings are clipped to 100–6,000 miles a
month. A contract with no reading takes the median of its new/used segment (about 1,150 miles a month
on this book). When the miles run out first, the contract is fully earned and leaves the in-force
count early. This uses readings taken later than the month being earned, which is fine for a
reserving view but would leak in a pricing model.

**Which method each product uses** lives in two reviewable files, not in code:
- `actuarial/earning_methods.csv` holds rules by category, subcategory and new/used (`*` is a wildcard; the most specific match wins).
- `actuarial/earning_curves.csv` holds the knots of each custom curve. They are validated to run from (0, 0) to (1, 1) without going down.

| Rule | Method | Why |
|---|---|---|
| default | pro-rata | even risk |
| VSC, new vehicle | custom `vsc_new_warranty_overlap` (back-loaded) | the manufacturer warranty covers the early months |
| VSC, used vehicle | custom `vsc_used_early_wear` (slightly front-loaded) | wear claims start soon after sale |
| GAP | Rule of 78s | the gap is largest early in the loan |
| Maintenance, specialty (RV, powersports), tech coverage | mileage-aware pro-rata | use follows miles as much as time |

**Monthly earned premium** for one contract is `P × (E(k) − E(k − 1))`. The effective month counts as
month 1. A cancelled contract earns through its cancel month, and the rest is refunded. Earned rows
carry the `posting_period_key` (yyyymm) of the month being earned.

### Why the method matters for the loss ratio

Loss ratio = incurred / **earned**. The claims are the same under every method; only the denominator
changes.

**One cut-off.** The monthly `earned_premium` table runs to the as-of month and earns that month
whole (a contract written on Oct 1-21 earns its full month 1). The page and the inception-to-date
figures therefore use `valuation_view`, which stops at the **valuation date** (the last complete
quarter end, the same cut as IBNR and the triangles): earned premium through the valuation month,
incurred on losses dated on or before it from lines posted by then. On this synthetic book (as of
2026-10-21, valuation 2026-09-30):

| Method | Earned premium to valuation | Loss ratio |
|---|---|---|
| Rule of 78s | about $33.1M | about 39% |
| Pro-rata, mileage-aware | about $27.1M | about 48% |
| Pro-rata | about $26.7M | about 49% |
| Selected (by product) | about $26.2M | about 50% |
| Reverse Rule of 78s | about $20.3M | about 64% |

**Basis.** "Earned premium" here is the claims-reserve slice of dealer cost, so the page calls it the
**earned claims reserve**. It ties to finance's earned claims reserve (`core.fct_earned_disbursement`,
same cut-off) within about 2%; the page shows both. The Overview, P&L and Finance pages use "Earned
premium" for **all cost-split buckets** (`finance.mart_pnl_by_product`: claims reserve plus admin fee,
commission, insurer fee and the rest), which is larger. The actuarial page quotes that all-bucket figure
and the all-bucket UPR (net written less earned) next to its own, so the two bases are never confused.

Front-loaded earning shows profit before the claims arrive, so a dealer looks great early and then
gets worse. Pick the method that matches when claims actually emerge. Otherwise profit is released too
early, and reinsurance dividends get paid out that are regretted later. Over a contract's whole life,
every method earns 100%.

### Does the earning curve mirror claim emergence? (`earning_fit`)

The rules above are judgement. `earning_fit` tests them against the claims every run:

- **Base**: contracts whose exposure is over (term run out, or cancelled by the as-of date), so their
  lifetime is known. A cancelled contract is truncated at its cancel date: it counts as exposed only
  up to the share of term it reached, and only its losses dated by then count.
- **Emergence**, life-table style so cancellations do not tilt it early: per term decile,
  `loss rate = incurred in the decile / premium exposed in the decile`; the cumulative share of
  lifetime claims is the running sum of those rates over their total. (Simply summing losses would put
  a contract cancelled at 35% of term entirely in the early deciles.)
- **Curve**: premium-weighted share earned by each decile end under the selected method, plus pro-rata,
  mileage-aware pro-rata and both Rule of 78s for comparison.
- **Gap** = curve minus emergence; `max_abs_gap` is the Kolmogorov-Smirnov distance. A positive gap means
  premium is earned ahead of the claims, so early loss ratios look too good. **A/E** per decile =
  (incurred / earned in the decile) / (incurred / earned over the life); 1.0 everywhere is a perfect fit.
- **Gate (warning)**: a row with at least 20 claims warns when `max_abs_gap` is above 10 points *and*
  above its noise band `1.36 / sqrt(claims)` (the 95% KS distance a true fit would still show; an
  approximation, since losses are dollar-weighted). `run_summary` carries `earning_fit_gate`
  (`pass` / `warn`), the worst row and its gap. `python -m actuarial` logs a warning;
  `--strict-fit` makes it exit 1.

The site shows one chart per family (earned vs emerged, new and used) and the gate table. On this
synthetic book both VSC custom curves fit within about 6 points, as do GAP new and maintenance. The gate
warns on: ancillary new on pro-rata (about 28% of claims emerged at mid-term against 50% earned; the
reverse Rule of 78s fits within about 6 points), specialty on mileage-aware pro-rata (earned runs up to
about 35 points ahead; plain pro-rata is closer), limited warranty (back-loaded claims) and GAP used.
The gate is evidence for changing `earning_methods.csv`, not an automatic switch.

## 2. Unearned premium reserve (UPR)

`UPR = Σ P_i × (1 − E_i(k_i))` over the contracts in force. Cancelled contracts drop out in their
cancel month, because that money is refunded. The selected-method UPR at the valuation month end is
`run_summary.upr_selected` (see the figures block in section 6). Like earned premium it is the
**claims slice** of the unearned dealer cost, the part that funds the claims still to come; the
finance pages' all-bucket UPR also holds the unearned fees and commissions.

## 3. Cancellation refund

`refund = max(0, retail_price × (1 − E(elapsed)) − fee)`. It is pro-rata by default, with a $50 fee.
Fee caps, and whether paid claims may be deducted, depend on state law and the contract wording.

## 4. Loss-development triangles (on payment lines)

**Policy-cohort triangle.** Rows are contracts grouped by their **effective quarter**. Columns are the
development age: the quarter a payment line was posted, minus the cohort quarter, plus 1. Each cell is
cumulative.
- `reported` adds up `incurred_change`, which is paid **plus the change in the case reserve**, so open claims' case reserves are already in it (2026Q1 cohort: reported $79.7K against paid $64.1K). Mature cohorts show reported = paid only because their claims are closed.
- `paid` adds up `paid_amount`.
- `count` counts claims by reported date.

The valuation date is the **last complete quarter end** before `--as-of`. A line posted before its
contract's effective quarter is put in age 1.

**Accident-quarter triangle.** Rows are the quarter of the claim's **loss date**, and the age is the
posting quarter minus the loss quarter. A VSC claim is usually reported within weeks, so this
triangle matures fast.

## 5. Chain-ladder and Bornhuetter-Ferguson

1. Volume-weighted link ratios: `f_j = Σ_i C[i, j+1] / Σ_i C[i, j]`.
2. `CDF_j = f_j × … × tail`.
3. Ultimate = latest × CDF.

**Tail per segment (`build.segment_tails`).** Each segment fits its own triangle, so the tail is the
development still to come past that segment's oldest age. A segment whose history spans its longest
term (3 × ages ≥ max term months: VSC, GAP, Ancillary, Limited warranty) has seen it all: tail 1.0
(`--tail`). Specialty and Maintenance only start in 2023Q1, so their oldest cohort is 15 quarters old
while their terms run to 84 and 48 months; a tail of 1.0 would call them fully developed. They borrow
one instead: the long-history segment with the most premium (VSC) read at the **same share of term**
(`ref_age = 15 × VSC mean term / segment mean term`, premium weighted), floored by an exposure tail
(1 / the share of lifetime exposure their contracts have run by then). The larger wins.
`development_factors` shows `tail`, `tail_source` (`observed`, `reference:VSC`, `exposure`,
`pooled_diagnostic`), `max_term_months` and `observed_months`; a test checks that a segment with a
longer term than its history gets a tail above 1. (VSC's own exposure tail at age 15, 1.29, is close
to its fitted CDF there, 1.27, which is why the exposure floor is a fair stand-in.)

**Which history fits the factors.** Losses on the legacy admin system posted their first incurred
line about three weeks after the loss; since the Admin Core migration (contracts from 2023Q1) lines
post on the report date, about 5 days after the loss. Pooling both regimes gave an accident-quarter
age-1 factor of about 1.09, against about 1.04 on post-migration experience alone, and every open
quarter (all Admin Core business) was being developed with the blended one. So a link `j -> j+1`
uses cohort `i` only when its from-cell lies on a calendar diagonal at or after the factor-start
quarter (`i + j - 1 >= start`, `reserving.link_cohorts`). Old cohorts still inform the late ages
through their recent diagonals, and a link with too few qualifying cohorts falls back to all of
them. `Settings.factor_start_quarter = "auto"` takes the quarter the newest source system's book
starts (2023Q1 here); `None` uses all history. This applies to both triangles;
`development_factors.cohorts_in_factor` shows the count behind each link.

**Bornhuetter-Ferguson.** Young cohorts have huge CDFs (age 1 is above 100 on the policy triangle).
A cohort that has reported less than half its ultimate (`1 / CDF < Settings.bf_max_reported = 0.5`,
about the first 9 quarters here) uses **BF**: `IBNR = ELR × premium × (1 − 1 / CDF)`; after that,
chain-ladder.
- The ELR is the chain-ladder ultimate loss ratio of **mature** cohorts: those that have run at
  least `Settings.elr_min_term_elapsed = 95%` of their term (premium weighted, cancelled contracts count
  as run). A low CDF is not enough: a young segment's oldest cohort has CDF = tail by construction. A
  segment with no mature cohort (Specialty) uses its chain-ladder cohorts, then 70%;
  `ibnr_by_cohort.elr_source` says which.
- **ALL is the sum of the segments**, cohort by cohort (`method = sum_of_segments`). A pooled triangle
  with one ELR would apply factors fitted mostly on old, VSC-heavy cohorts to young cohorts with a
  different mix. The pooled result stays as a diagnostic (`ibnr_by_cohort.all_triangle_ultimate`,
  `run_summary.selected_ultimate_pooled_triangle`); a test checks ALL equals the segment sum.
- Premium is the cohort's **expected ultimate net premium** (`premium_development`): a cohort x age
  triangle of premium kept (written at age 1, less each cancellation's refunded share in the quarter
  it lands) is chain-laddered like losses. Its factors are below 1. A young cohort has had few
  cancellations yet, so without this it would be credited with premium its future cancellations will
  take back (a 2026Q3 cohort keeps 96% of written premium today, but about 85% by the end, as older
  Admin Core cohorts do). The ELR and the premium it multiplies are then on the same net basis.

Tests check this against a hand-worked 3×3 triangle and against Taylor & Ashe / Mack (1993): the
published link ratios and the total reserve of 18,680,856.

## 6. IBNR vs losses on unexpired coverage (the V3 fix)

A policy-cohort chain-ladder projects each sale quarter to its **life-of-contract** ultimate. For a
book that is only half earned, most of `ultimate − reported` is claims that **have not happened yet**,
on coverage that still has years to run. That is not IBNR: the unearned premium funds it. V1 labelled
the whole gap "IBNR". That is why IBNR looked about as large as reported losses ($12.1M against
$13.0M).

V3 splits the gap:
- `selected_ibnr` is the **true IBNR**. Each cohort's reported losses are split by loss quarter and developed with that loss quarter's accident-triangle CDF: `IBNR = Σ_a reported[cohort, a] × (CDF_acc(a) − 1)`. Summed over cohorts it equals the accident-quarter IBNR (lines with no contract key aside).
- `unexpired_risk_losses` is the rest of the development to ultimate (floored at 0).
- `development_to_ultimate` = `selected_ultimate − reported_to_date` = the sum of the two.

The page labels the second amount **"expected losses on unexpired coverage (not IBNR)"**, and the
claims page shows it as its own column so that Reported + IBNR + Unexpired coverage = Ultimate for every
cohort. The ultimate is on a policy-year (sale-cohort) basis, which is why it includes coverage that
has not run yet.

### Why the IBNR is small, and what the back-test says about its size

The figures for this run (generated, see the top of this file):

<!-- run_summary:start -->
_Generated by `python -m actuarial --write-docs` from `actuarial.run_summary`; do not edit by hand._

| Figure | This run |
|---|---|
| Valuation date (as of) | 2026-09-30 (2026-10-21) |
| Development factors fitted on diagonals from | 2023Q1 |
| Accident-quarter age-1 to age-2 factor | 1.040 |
| Selected IBNR (true IBNR, all sources) | $39.5K on $12.74M reported (0.31%) |
| Accident-quarter IBNR (before the cohort allocation) / on Admin Core lines | $39.5K / $39.5K |
| IBNR in days of incurred losses | 4.3 days |
| Report-lag IBNR: unreported claims / development on known claims / quarterly chain ladder (comparison) | $59.6K / -$20.1K / $30.9K |
| Unreported claims: claims per day x mean report lag x severity | 12.2 x 5.84 days x $835 |
| IBNR back-test, 12 earlier quarter-ends: mean absolute error / mean realized / total estimate to realized | $11.6K / $21.7K (54%) / 1.34x |
| Report lag: mean / within 14 days / 30 days or more | 5.5 days / 95.1% / 4.5% |
| New claims reported in the 21 days after valuation (IBNR back-test) | $26.7K = 68% of IBNR, against 56% expected by now (A/E to date 1.21, ibnr_short) |
| Case-reserve development on claims known at valuation (not IBNR) | -$26.0K (denials -$16.7K, other -$9.3K) |
| Selected ultimate: sum of segments / pooled-triangle diagnostic | $22.32M / $22.30M |
| Claims outside the Claims-mart scope (not in the triangles) | no contract $48.4K; loss before the sale $10.9K (13 claims) |
| Losses on unexpired coverage (not IBNR) | $9.54M |
| Selected UPR, claims slice | $19.66M; unexpired losses are 49% of it |
| PDR needed (sum of segments) | $0 |
| Earned claims reserve to valuation (selected) / loss ratio | $25.93M / 49.1% |
| Forecast model | ewma |
| Last history quarter (developed) / next quarter forecast | 51.1% / 50.3% |
| Next-quarter band (about 95%) | 44.2% to 56.4% |
<!-- run_summary:end -->

The IBNR is small because the synthetic claims are reported fast: the generator draws the report lag
as 95% geometric (mean about 2 days) and 5% at 30 to 120 days, a mean of about 6 days, and the
observed lag (`run_summary.report_lag_*`) matches. So IBNR should be about a week of incurred losses
or less (`ibnr_days_of_incurred` = IBNR / (last 4 accident quarters' ultimate / 365)).

**Back-test against the runoff.** The as-of date is after the valuation date, so the days in between
show what actually emerged on losses dated on or before valuation. IBNR is scored **only on claims first
reported after valuation** (`ibnr_runoff_new_claims`, `ibnr_runoff_ratio` = new claims / IBNR).
Development on claims already known at valuation (case reserves released on claims later denied,
settlements below the reserve) is a case-reserve adequacy figure, `case_development_known_*`, shown on
its own line; netting it against new claims once made the IBNR look 45 times too big. A 21-day window
can only see part of the report-lag tail, so the actual is also compared with what the window should
already show: with a steady loss flow and lag `L`, `E[min(L, W)] / E[L]` of the IBNR
(`ibnr_runoff_expected_share`, lags from losses at least 180 days old). `ibnr_runoff_ae_to_date` = new
claims / (IBNR × that share); above 1 sets `ibnr_runoff_flag = ibnr_short`. One quarter's runoff is a few
dozen late claims with skewed severities, so the flag and these figures are information on the page and
in the log, not a gate. Calibration is judged over many valuations instead: `actuarial.ibnr_backtest`
re-runs the IBNR at each of the 12 earlier quarter-ends with only the claims and lines known then and
compares it with the incurred that posted later on losses dated by it (`ibnr_backtest_*` in
`run_summary`; a test holds the mean absolute error within 60% of the mean realized IBNR).

**How the IBNR is estimated (report lag, by day).** `build.report_lag_ibnr` splits the accident-quarter
IBNR in two. (1) Unreported claims: claims per loss day × `S(k)` (the share of claims reported more
than `k` days after the loss, fitted on losses 180 days to 2.5 years old) × severity per claim, summed
over the loss days before valuation. Claims per day are the last 91 loss days' reported claims grossed up
for those still to come; severity is ultimate incurred per claim (denied as 0) on the year of losses
before that. (2) Development on claims already known at valuation (reserve releases, settlements,
supplements): a report-quarter triangle chain-laddered from the factor-start quarter. The quarterly
accident triangle could not separate these: its age-1 link netted reserve releases on known claims
against late claims from a whole quarter of losses, when the late claims all come from the last few
weeks. Its result is kept as `accident_quarter.chain_ladder_ibnr` for comparison.

**Claims scope.** `actuarial/data.py` applies the Claims marts' validity rule
(`mart_claim_summary.is_contract_matched`): a claim needs a contract and a loss on or after its
effective date. The legacy feed back-dates some losses to before the sale, and lines with no contract
exist; both are left out of the triangles and accident quarters (which would otherwise show losses
against $0 earned premium) and carried in `run_summary` as reconciling lines
(`incurred_lines_without_contract`, `incurred_excluded_loss_before_sale`, `claims_excluded_*`), so
triangle + exclusions = every line in the claims fact.

**Against the generator's truth.** A warehouse test compares the Admin Core IBNR
(`ibnr_source_admin_core`) with the generator's **expected** Admin Core IBNR at valuation: claims per
loss day × the days of report lag still to come (from the generator's lag model) × mean planned
severity, plus, for claims already reported with a payment or supplement still to come, their planned
ultimate less the incurred posted by valuation (the expected reserve release). The tolerance is ±25%.
It is not compared with the realized outcome, which the test prints as information: at the 2026-09-30
valuation 64 claims were unreported (about 72 expected) but their paid ones averaged about $1,470
against about $950, so the realized $66.0K is far above what the process implies. The quarterly chain
ladder gave $30.9K (−29% against the expected $43.6K); the report-lag IBNR gives $39.5K (−10%).

### Premium-deficiency test

The losses on unexpired coverage are funded by the UPR, and the page now shows it per segment
(`premium_deficiency`): `PDR = max(0, unexpired losses + maintenance expense − UPR)`, where the
maintenance (claims-handling) expense is an assumed 5% of the unexpired losses
(`Settings.maintenance_expense_ratio`; there is no expense data). The UPR here is the claims slice,
the part of unearned dealer cost that funds claims; the page also shows the all-bucket UPR for
context. The total ratio and the PDR are in the figures block above; the per-segment ratios are in
`premium_deficiency` and on the page.

## 7. Early-claim emergence

A contract counts in a window (30, 60, 90, 120, 150 or 180 days) only after it has been effective for
the whole window ("exposed"). Its first loss on or after the effective date decides whether it is
early. Losses dated before the effective date are ignored as data errors.
- `early_claim_contracts` excludes denied claims. This is the same rule as `core.dim_contract_claim_summary.is_early_claim_<N>`, and a warehouse test ties the two together.
- `early_reported_contracts` includes denied claims, because an early claim is a signal even when it is not paid.
- `within_wait_period` counts early claims inside the contract's claim waiting period.

**Dealer watchlist.** Expected early claims use each contract's own subcategory and new/used rate, so
a dealer is never flagged only for its product mix. `A/E = observed / expected` and
`z = (observed − expected) / √expected`. A dealer is flagged when it has at least 30 exposed contracts,
A/E ≥ 1.5 and z ≥ 2. On this book, used-vehicle VSCs have far more early claims than new ones (656
against 74 contracts by 180 days), as expected behind the factory warranty.

## 8. Loss-ratio forecast with backtest

The series is the **accident-quarter ultimate loss ratio**: losses by the quarter they happened,
developed to ultimate, over the premium earned (selected method) in that quarter. Quarters with less
than 2% of the peak earned premium are dropped. Developing first follows the rule of never forecasting
from raw immature loss ratios.

Four explainable baselines compete:
- naive (last quarter)
- mean of the last 4 quarters
- EWMA with α = 0.3
- least-squares trend over the last 8 quarters

The **backtest is split by time.** For each of the last 8 origins, each model is fitted on the
quarters before the origin only, then scored on the next 4. A test proves that changing a later value
never changes an earlier forecast. The model with the lowest MAE is selected, and its band is
±1.96 × its backtest RMSE at each step ahead, **made non-decreasing over the steps** (a running
maximum). With 8 origins, step 1 has 8 scored points and step 4 only 5, so the raw per-step RMSE is
noisy and can shrink with the horizon; published as is, that told the reader the forecast is surer
four quarters out than next quarter. `forecast_backtest.points_by_step` and the forecast rows'
`band_points` show how many points stand behind each step. The selected model and the next-quarter
forecast and band are in the figures block in section 6.

## Limits (honest list)

- These are deterministic point estimates. There are no Mack standard errors or bootstrap yet. The forecast band comes from the backtest, not from a model.
- The backtest scores against today's developed ultimates, not against what was known at each origin. With a fast-reporting book the difference is small.
- The custom curves are set by judgement, not fitted. The next step is to fit each curve to the claim emergence in `accident_triangle` and `early_claim_emergence`.
- The forecast is on the whole portfolio only. A per-segment forecast would need a credibility blend.
- The generator's late-reporting tail is thin (5% at 30 to 120 days), so the policy-cohort triangle shows little reporting development and the IBNR back-test has few late claims to score. A wider tail is a generator setting, not an actuarial one.
- The premium-deficiency test uses an assumed expense loading and ignores investment income. Its UPR already excludes cancelled contracts; future cancellations on the in-force book reduce both the UPR (refunds) and the unexpired losses, and are not modelled separately there.
- The factor-start quarter is a single cut. A posting-regime change that phased in over several quarters would need a blend.
