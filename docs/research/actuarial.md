# VSC / Extended Warranty Actuarial Metrics (research spike, 2026-10-22)

Notation: contract term `n` months, `k` months elapsed, `P` = premium (dealer cost net of admin fee unless noted).

## Premium & earning
- **Written premium (WP)**: premium on contracts sold in the period, booked at sale date.
- **Earned premium (EP)**: the part of WP whose risk period has passed. `EP_period = Σ P_i × (E_i(end) − E_i(start))`.
- **Earning curves (fraction earned, E(k))**:
  - Pro-rata: `k / n`
  - Rule of 78s (front-loaded): `k(2n − k + 1) / (n(n+1))`
  - Reverse Rule of 78s (back-loaded, typical for new cars under factory warranty): `k(k+1) / (n(n+1))`
  - Custom curves: derived from historical claim emergence by term, mileage band, new/used, and coverage tier. Best practice is to earn premium in proportion to expected losses (CAS, Edie).
  - Mileage-limited terms: earn on the greater of the time fraction and the mileage fraction.
- **Unearned premium reserve (UPR)**: `Σ P_i × (1 − E_i(k))`. For a typical VSC book it is 10–20x the claim reserve (SOA), so choosing the curve matters more than IBNR.

## Claims
- **Paid**: cash paid in the period. **Incurred** = paid + change in case reserves + change in IBNR.
- **IBNR (chain-ladder)**: build a cumulative triangle (rows = sale or loss month, columns = development age). Age-to-age factor `f_j = Σ C_{i,j+1} / Σ C_{i,j}` (volume-weighted). Cumulative development factor (CDF) = product of the f's to ultimate. `Ultimate_i = latest_i × CDF_i`, `IBNR = Ultimate − latest reported`. Use Bornhuetter-Ferguson for immature cohorts. Mack's method gives standard errors.
- **Frequency** = claim count / earned exposure (contract-years). **Severity** = incurred $ / claim count. Pure premium = freq × severity.

## Ratios
- **Loss ratio** = incurred losses / earned premium (not written premium). Trade press puts dealer reinsurance targets around 70%, with a 50–90% range.
- **Expense ratio** = (admin fees + commissions + premium tax + other expenses) / EP (statutory convention divides by WP).
- **Combined ratio** = loss ratio + expense ratio. Above 100% means an underwriting loss.
- **Cancellation rate** = cancels / contracts written (by cohort). **Retention / persistency** = 1 − cumulative cancel rate.
- **Contracts in force (CIF)** = sold − cancelled − expired (by term end date or mileage), counted as of a date.

## Cancellations / refunds
`Refund = P_retail × (1 − E(k)) − cancel_fee − claims_paid (if the state allows deducting claims)`. The method (pro-rata vs Rule of 78s) and fee caps (often ~$50–75) are set by **state law and contract wording**. Most states require pro-rata for consumer refunds. The dealer chargeback is the unearned share of the dealer markup.

## Dealer cost vs retail
`Markup = retail − dealer cost`. `Dealer cost = admin fee + reserve/premium + CLIP/insurance + commissions`. In dealer-owned reinsurance (CFC, dealer-owned warranty company, NCFC, RPC), the **reserve portion** is ceded to the dealer's company. Dealer profit = earned reserve − incurred claims + investment income. Underwriting profit only appears as business **earns**, so loss ratios on the trailing 12 and 24 months are the key dealer KPI.

## Regulator (state DOI) reporting needs
- Reserve adequacy: the NAIC-style test holds the UPR at the larger of (a) the refund liability, (b) future-loss-ratio × gross premium, (c) projected future losses (Edie / CAS).
- Funded-reserve providers: reserve ≥ ~40% of gross consideration less claims paid, plus a security deposit of ≥5% (SC example). Otherwise a **reimbursement insurance policy** is required (NAIC Model 685).
- Typical filing data: contracts written and in force by state, WP/EP/UPR, paid and incurred claims, cancellation/refund records, the claims handling audit trail, and record retention. Statutory Annual Statement Schedule P triangles apply to the insurer/reinsurer, not to the TPA.

## Tradeoffs
- Pro-rata is simple and legally safe for refunds, but overstates early earnings and profit on new-car VSCs.
- Rule of 78s and reverse Rule of 78s are closed-form and easy to code, but only crudely match how losses actually emerge.
- Empirical curves are the most accurate, but they need years of claim history and versioned assumptions.
- Chain-ladder is standard and explainable, but unstable on thin or young cohorts, so blend it with Bornhuetter-Ferguson.

## RECOMMENDATION
1. Keep **two separate earning methods**: a statutory/refund method (pro-rata, per-state rules table) and a management method (curves stored as a `dim_earning_curve` keyed by term × mileage × new/used, shipped with pro-rata, R78 and reverse-R78 as seeds).
2. Build a monthly **earned-premium snapshot fact** (`fact_contract_month`: contract, month, E(k), EP, UPR, in-force flag). Every ratio comes from this one table.
3. Report **loss ratio = incurred/earned** by dealer, reinsurance company, product, cohort, and state, with a trailing 12/24-month view and a high-loss-ratio alert (for example > 80%).
4. Build **paid and reported claim triangles** (cohort = sale month, 3-month development) and calculate chain-ladder IBNR in Python, with Bornhuetter-Ferguson for cohorts under 12 months old.
5. Ship a **DOI pack view**: CIF, WP/EP/UPR, claims, and cancellations/refunds by state.

## Sources
- [SOA, Kerper: VSC reserves (UPR 10–20x claim reserve)](https://www.soa.org/globalassets/assets/library/newsletters/newsdirect/2001/summer/ndn-2001-iss37-kerper.pdf)
- [CAS, Edie: Evaluating the UPR for Auto Extended Service Contracts](https://www.casact.org/abstract/evaluating-unearned-premium-reserve-automobile-extended-service-contracts) · [PDF](https://www.casact.org/pubs/forum/99fforum/99ff085.pdf)
- [CAS Forum 1999 (VSC earning)](https://www.casact.org/sites/default/files/database/forum_99fforum_99ff177.pdf)
- [NAIC Service Contracts Model Act #685](https://content.naic.org/sites/default/files/model-law-685.pdf)
- [SC service contract statute (reserve 40% / deposit 5%)](https://www.scstatehouse.gov/sess113_1999-2000/prever/1039_20000504.htm)
- [CAS, Halliwell: Chain-Ladder Bias](https://casact.org/sites/default/files/2021-07/Chain-Ladder-Bias-Halliwell.pdf)
- [Friedland, Estimating Unpaid Claims Using Basic Techniques (CAS)](https://www.casact.org/sites/default/files/old/studynotes_friedland_estimating.pdf)
- [F&I Magazine: provider contracts / loss ratios](https://www.fi-magazine.com/articles/dig-into-the-details-with-fi-provider-contracts)
- [CBT News: dealer reinsurance](https://www.cbtnews.com/dealers-to-reassess-profit-as-reinsurance-evolve/)
- [Powersports Business: reinsurance profits](https://powersportsbusiness.com/top-stories/2024/08/27/maximizing-fi-profits-through-reinsurance/)

Caveats: the state rules quoted are examples (some from older statutes). Check current state law before using any figure. The Friedland link was not checked in this research.
