# Industry products and structures (generic)

This note describes how the F&I protection-product industry is usually set up, written in general terms. It is based on public industry sources such as trade press, administrator marketing pages, and state regulator listings. It names no company, product brand, or trademark. Any dollar amounts and grids below are **recommended synthetic values** for our generator, not one provider's real pricing.

## 1. Who the parties are

Most programs have these roles. The data model should keep each one as its own entity:

| Role | What it does | Model note |
|---|---|---|
| Dealer (producer) | Sells contracts in the F&I office and keeps a markup | `dim_dealer`; can belong to a dealer group |
| Obligor / provider | The legal promisor on the contract. Licensed per state | Can vary **by state** for the same product, so store it on the contract |
| Administrator (TPA) | Handles rating, contract registration, claims, cancellations, reporting | Usually one administrator, so it can be a constant |
| Insurer | Issues the contractual liability policy (CLIP) that backs the obligor | `dim_insurer`; also excess-of-loss or stop-loss cover |
| Participation entity | Dealer-owned company that keeps the underwriting result | `dim_participation_entity` with a type (see section 2) |
| Lienholder | Lender on the vehicle; can start a cancellation | Store on the contract |
| Repair facility | Does the repair and gets paid by the administrator | `dim_repair_facility`; selling dealer vs network shop |

## 2. How dealers share in the profit

Dealer-owned reinsurance administrators typically offer several participation types:

- **Dealer-owned obligor company (domestic C-corp):** the dealer's own company is the obligor. Premium is earned over the contract life, and the company files as a P&C insurer for federal tax. It needs real starting capital (often tens of thousands of dollars, and some states require far more). Funds are held in a trust or a two-signature investment account. It often buys excess-of-loss cover.
- **Controlled foreign corporation (CFC) reinsurer:** the insurer cedes premium to an offshore reinsurer the dealer controls, usually with a domestic-tax election. It needs little capital, and its funds are held in trust under reserve guidelines.
- **Non-controlled foreign corporation (NCFC):** like a CFC, but with many unrelated owners (one cell per dealer). This was common before the 2017 tax-law change.
- **Producer-owned reinsurance company (PORC):** a general term for a reinsurer owned by the producer. It can be onshore or offshore.
- **Retro / retrospective commission:** the dealer owns no company. After an experience period, the dealer is paid a share of the underwriting profit.
- **Cash advance / enhanced commission:** the dealer gets more money up front and gives up any back-end profit.

The dealer earns money in three ways, so the model should show each one:
1. **Dealer markup:** retail price minus dealer cost, paid at the sale.
2. **Underwriting profit:** earned premium minus paid and reserved claims, minus admin fee, CLIP fee and taxes.
3. **Investment income:** the return on the reserve (trust) balance while premium is still unearned.

**Model recommendation:** split every contract's dealer cost into buckets: `admin_fee`, `clip_insurance_fee`, `premium_tax`, `commission` (agent), and `claims_reserve` (the ceded or obligor reserve). Only `claims_reserve` flows to the participation entity. Earn reserves monthly (see section 7). Track the trust account with a monthly balance and an investment yield of 2-5% a year.

## 3. Product catalog

Most administrators sell these lines. Recommended catalog for `dim_product`:

| Product line | Code | Typical buyer | Pricing basis | Covers |
|---|---|---|---|---|
| Vehicle service contract | VSC | New and used | Rated by tier, term, miles, vehicle class, deductible | Mechanical breakdown |
| Dealer limited warranty | LTDW | Used, often bundled into the sale | Flat | Powertrain only |
| Certified pre-owned wrap | CPO | Used | Flat per vehicle | Powertrain or exclusionary, plus roadside |
| GAP waiver / GAP insurance | GAP | Financed or leased | Flat or by amount financed | Gap between insurance payout and loan payoff |
| Tire & wheel | TW | All | Flat, by wheel size band | Road-hazard tire and wheel repair or replacement, optional cosmetic wheel |
| Key / fob replacement | KEY | All | Flat | Lost, stolen or broken keys |
| Paintless dent repair | PDR | All | Flat | Small dents with paint intact |
| Windshield | WS | All | Flat | Chip repair, optional replacement (1 or 2 per term) |
| Appearance / interior-exterior | APP | All | Flat | Paint, fabric and leather stains, tears, burns, chips |
| Prepaid maintenance | MAINT | All | By number of visits | Oil changes, rotations, inspections |
| Lease excess wear & tear | LEWT | Leases | Flat | End-of-lease damage charges |
| Theft / total-loss credit | THEFT | All | Flat | Etch or a vehicle tracker plus a replacement-vehicle credit |
| Payment protection | PAY | Financed | Flat | Car payments after job loss or disability |
| Bundle | BNDL | All | Sum of parts minus a discount per added part | Pick-your-own ancillary package |

Also include vehicle class families: car/light truck (default), EV, powersports, RV, commercial truck, golf cart. EVs need tech/battery components, and commercial use carries a surcharge.

### Ancillary product rules (synthetic)
- **Bundles:** 2 to 6 components chosen from {TW, KEY, PDR, WS, roadside, ID-theft}. Take 5% off for each component after the first, capped at 20%.
- **Tire & wheel:** terms 36/48/60/72 months, no deductible, per-claim cap $1,500, 18-inch+ wheel surcharge. Expect about 25-35% of contracts to claim, at an average of $250-$450.
- **Key:** terms 36-84 months, per-claim cap $800, 2 replacements per term. About 8-12% of contracts claim, averaging $350-$600.
- **Dent:** terms 36-84 months, panel size limit of about 4 inches. About 20-30% claim at $100-$200 each.
- **Windshield:** Repair-only option, plus a premium option with 1 replacement and an ultra option with 2.
- **GAP:** term matches the loan term. Max benefit capped at 125-150% of ACV (LTV). Benefit = payoff minus insurance settlement, capped. The deductible is often reimbursed up to $1,000. About 2-4% of contracts claim, averaging $3k-$8k.
- **Payment protection:** pays up to 6 monthly payments, capped at about $7,500 total, after a 30-day elimination period.
- **Total-loss credit:** for new vehicles, 10% of MSRP with a $2,500 minimum. For used vehicles, the lower of 50% of ACV and the plan cap.
- **Lease wear & tear:** 90-day standard term or 1-year flex term, with a per-item cap and an aggregate cap of about $5,000.
- **Appearance:** base tier plus add-ons (paint, fabric, wheel finish, glass, odor). Spray-on paint lasts up to 7 years, and some coatings last the life of the vehicle.

## 4. VSC coverage tiers

The industry usually has four levels, each one building on the last. Recommended `dim_coverage_tier`:

| Tier | Code | Covered component groups | Relative cost | Mix |
|---|---|---|---|---|
| 1 Powertrain | PT | Engine, transmission, drive axle, transfer case, seals and gaskets | 1.00 | 15% |
| 2 Powertrain plus | PTP | Tier 1 + brakes (non-wear), steering, suspension, A/C, basic electrical | 1.35 | 25% |
| 3 Stated component | SC | Tier 2 + enhanced electrical, fuel system, cooling, tech modules, hybrid parts | 1.70 | 30% |
| 4 Exclusionary | EXC | Everything except a short exclusion list (wear items, glass, body, trim) | 2.10 | 30% |

All tiers include roadside (towing about 50 miles, jump-start, flat tire, lockout, fuel delivery) and rental (about $40-$60 a day, max 5-10 days a claim). Rental kicks in when the shop holds the vehicle 8 hours or more.

**Optional add-ons:** wear-and-care (brake pads/rotors, belts, hoses, battery, bulbs, wipers, alignment), tech package (ADAS sensors, cameras, infotainment, HUD, keyless entry), and EV battery.

Use a `component_group` dimension: engine, transmission, drive_axle, AC, electrical, steering, suspension, brakes, fuel, cooling, tech_adas, hybrid_ev, seals_gaskets. Give each tier a coverage bridge table `tier_component(tier_code, component_group)`, so the generator only creates claims on covered groups.

## 5. Term / mileage grid and deductibles

Public pages rarely publish the grid. A realistic synthetic grid:

- **Terms (months):** 12, 24, 36, 48, 60, 72, 84, 96, 120.
- **Mileage limits:** 12k, 24k, 36k, 48k, 60k, 75k, 100k, 125k, 150k, unlimited for limited warranties.
- **Valid pairs:** plans run from roughly 12k miles a year up to 20k miles a year. Examples: 36/36k, 48/50k, 60/60k, 60/75k, 72/75k, 72/100k, 84/100k, 96/100k, 120/125k.
- **New vs used mileage basis:**
  - New-vehicle plans count from 0 miles and from the in-service date.
  - Used-vehicle plans add the term miles to the odometer reading at sale.
  - The contract ends at time or miles, whichever comes first.
- **Dealer limited warranty:** 3, 6, or 12 months, or lifetime. Lifetime warranties require scheduled maintenance.
- **Deductibles:**
  - Amounts: $0, $50, $100, $200, $250, $500. A disappearing deductible ($100, or $0 at the selling dealer) is also common.
  - Rate factors: $0 = 1.15, $50 = 1.07, $100 = 1.00, $200 = 0.94, $250 = 0.92, $500 = 0.85.
  - Mix: about 55% of contracts at $100.
- **Surcharges** (flat add-ons to dealer cost, store them as a list):
  - Vehicle and drivetrain: 4x4/AWD, diesel, turbo/supercharged, hybrid/EV.
  - Use: commercial use, lift kit or oversize tires.
  - Vehicle class: luxury/high-performance class.
  - Wear: high-mileage band. Charge $50-$300 each.
- **Vehicle rating class:** classes 1-6 by make/model reliability and repair cost. Luxury and EV vehicles fall in the higher classes.

## 6. Eligibility rules (synthetic)

| Rule | New | Used |
|---|---|---|
| Max vehicle age at sale | Current or prior model year with less than 7,500 miles (factory warranty still in force) | Up to 10-15 model years old depending on tier |
| Max odometer at sale | 7,500 | Tier 4 is limited to about 80k miles. Tiers 1-2 allow up to about 150k-200k miles on a high-mileage program |
| Highest tier available | Tier 4 | Tier 4 needs fewer than 80k miles and an age of 8 years or less. Tier 3 needs fewer than 125k miles. Above that, only Tiers 1-2 |
| Waiting period | None if new | 30 days and 1,000 miles for claims |
| Excluded | Salvage or rebuilt titles, gray-market vehicles, commercial use (unless surcharged), and certain exotic makes | Same |
| Pre-existing conditions | n/a | Excluded |
| Lifetime limited warranty | n/a | Needs maintenance records; claims are denied if maintenance is missed |

The generator should **reject or re-roll** draws that break these rules. The data-quality tests should assert that no contract breaks them.

## 7. Contract lifecycle, earnings, cancellations

**Contract statuses:** `QUOTED -> SOLD -> REMITTED -> ACTIVE -> (EXPIRED | CANCELLED | TRANSFERRED | CLAIMED_OUT)`. Remittance usually happens 5-30 days after the sale, so include a lag field.

**Earning methods**, stored as `earning_method` on the product:
- Pro rata by time is the default for ancillary products.
- Rule of 78 or a blended curve is used for VSC, because claims grow over the contract's life.
- For mileage-based plans, earn by the greater of time used or miles used.

**Cancellation rules** (the industry norm; states change details):
- **Free-look:** 30 days, or 60 in some states. The customer gets a full refund, with no fee, as long as there are no claims.
- **After free-look:** pro-rata refund by the greater of time and miles used. Subtract a cancel fee of $25-$75 (some states cap or ban it). Subtract paid claims if the state allows that.
- **Who can cancel:** customer, lienholder (payoff, repossession, total loss), dealer (deal unwind), or administrator (fraud, misrepresentation).
- **Lienholder refund:** when a lien exists, the refund goes to the lienholder.
- **Not cancellable:** some products, such as certain limited warranties or included-at-no-charge products.
- **Refund flow:** the administrator calculates the refund. The dealer pays back its share (dealer markup chargeback). The reserve gives back the unearned claims reserve. The administrator, CLIP and tax buckets are refunded pro rata or not at all, depending on state.
- **Inputs needed:** contract id, VIN, odometer at cancel, cancel date, reason code, documents (such as a total-loss letter).
- **Cancellation rates:** about 15-25% of VSCs cancel before expiry, and most early cancellations come from loan payoff or trade-in. Use reason codes: `PAYOFF`, `TRADE_IN`, `TOTAL_LOSS`, `REPOSSESSION`, `CUSTOMER_REQUEST`, `FREE_LOOK`, `DEAL_UNWIND`, `ADMIN`.

**Transfers:** allowed on many VSCs to a private buyer for a $25-$75 fee within about 30 days of the vehicle sale. They are not allowed to dealers.

## 8. Claim flow

The industry standard is **prior authorization**: the shop must get approval before teardown or repair. Unauthorized work is denied. The administrator pays the repair facility directly, by credit card over the phone or by ACH or check. The customer pays the deductible to the shop. Reimbursement claims, where the customer paid and then files with receipts, are the exception (roadside, rental, key).

Recommended `claim_status` states:

```
REPORTED -> DIAGNOSIS_AUTHORIZED -> ESTIMATE_RECEIVED -> (INSPECTION_REQUESTED -> INSPECTED)
  -> ADJUDICATED -> {APPROVED | PARTIALLY_APPROVED | DENIED}
  -> (APPROVED) AUTHORIZED -> REPAIR_COMPLETE -> INVOICE_RECEIVED -> PAID -> CLOSED
  side paths: WITHDRAWN, REOPENED, GOODWILL (paid outside coverage)
```

| What to generate | Recommended value |
|---|---|
| Inspections | Independent inspector for claims over about $2,000-$3,000, or when fraud flags fire. About 10-20% of large claims |
| Denial reasons | NOT_COVERED_COMPONENT, PRE_EXISTING, LACK_OF_MAINTENANCE, WAITING_PERIOD, EXPIRED_TIME_MILES, NO_PRIOR_AUTH, WEAR_ITEM, MODIFICATION, OTHER. Target about 10-20% of claims denied |
| Claim lines | Separate parts, labor (hours × the shop's approved labor rate), diagnostic (paid only if the repair is covered), shop supplies, disposal fees, and tax (state-dependent) |
| Labor rate | Capped at the posted rate, or an industry labor guide |
| Parts | May require used or rebuilt parts |
| Repair facility mix | About 50-60% at the selling dealer. The rest at other dealers or a network of certified independent shops |
| Timing | Report-to-authorize: same day to 3 days. Authorize-to-paid: 3-21 days |

Adjusters are usually ex-technicians; store `adjuster_id` on each claim.

Store the timestamp of each status change in `fact_claim_status_history`, which lets us measure cycle time.

## 9. Dealer reporting (what administrators advertise)

Build these marts so each report is one query:

1. **Production report:** contracts sold by product, tier and term; penetration (contracts ÷ retail units); average retail and markup; PVR. Drill down by F&I manager.
2. **Remittance / unremitted report:** contracts sold but not yet paid for, plus aging.
3. **Cancellation report:** cancels by reason, refund amounts, dealer chargebacks, and cancel rate by vintage.
4. **Claims report:** paid and pending claims, top components, average severity, frequency, denial rate, and cycle time.
5. **Loss ratio / experience report:** earned premium vs incurred claims by product, vintage (sale-year cohort) and dealer. Also IBNR estimate (ties to actuarial.md).
6. **Reserve / trust statement:** opening balance, ceded premium, claims paid, refunds, investment income, fees, and closing balance, each month.
7. **Cession report:** premium ceded to the reinsurer or participation entity by month and product.
8. **Earned / unearned premium report:** the UPR roll-forward.
9. **Participation P&L:** underwriting profit, investment income, taxes and NOL carryforward, and distributions.
10. **Retro / profit-sharing statement:** the retro payout calculation, for retro dealers.
11. **Compliance / licensing report:** contracts sold in states where the obligor is not licensed (should be zero), and F&I manager audit findings.
12. **Claims-service KPIs:** call volume, time to answer, first-contact resolution, and authorization turnaround.
13. **F&I health / ROI:** PVR vs benchmark for the dealer type (franchise, independent, buy-here-pay-here), and digital vs in-person menu share.

## 10. Data model changes to consider

- Add `obligor_id` and `insurer_id` to `fact_contract`. Obligor can differ by state.
- Add `participation_type` (DEALER_OWNED, CFC, NCFC, PORC, RETRO, NONE) and `participation_entity_id` to `dim_dealer` (SCD2, since dealers switch structures).
- Split dealer cost into fee buckets (section 2). Add `retail_price` and `dealer_markup`.
- Add `deal_type` (cash, finance, lease), `lienholder_id`, `amount_financed`, and `loan_term` (needed for GAP and payment products).
- Add `vehicle_class` (car, EV, powersports, RV, commercial, golf), `new_used`, `odometer_at_sale`, `in_service_date`, `rating_class`, and a surcharge bridge table.
- Add `earning_method` on the product, plus a monthly `fact_earnings` snapshot.
- Add `fact_cancellation`: reason code, who initiated it, free-look flag, fee, refund split by bucket, and refund payee (customer or lienholder).
- Add `fact_claim_line` and `fact_claim_status_history`. Add `repair_facility_type` (selling dealer, other dealer, network independent).
- Add `fact_trust_account_monthly` for reserves and investment income.
- Add a `dealer_type` attribute (franchise, independent, buy-here-pay-here) and `dealer_group_id`.

## 11. Typical data team at a VSC administrator

Synthesized from public job postings and careers pages of mid-size F&I product administrators (generic, no company named).

- **Where it sits:** usually a "Business Intelligence / Data Analytics & Reporting" group that is separate from IT. IT owns infrastructure, cloud, application developers and the DBA. BI owns pipelines, the warehouse, models and reports. Finance (FP&A, reinsurance accounting) and claims leadership are the main internal customers. Actuarial work is often done by finance, outside consultants or insurer partners, not by the BI team.
- **Levels:** Senior Director or Director of data/BI, then a BI/reporting manager, then senior individual contributors (analytics developer or analytics engineer, BI developer, senior data analyst, data engineer).
- **Typical stack:** Python and SQL for ingest, an orchestrator (often Apache Airflow) for ETL/ELT, a cloud data warehouse plus an operational PostgreSQL or SQL database, SQL-based modeling inside the warehouse, and a self-service BI tool with a governed workspace and semantic model (DAX-style measures, Power Query-style shaping). Sources are the policy/claims admin system, dealer and agent master data, insurer and reinsurer statements, and call-center/IVR data.
- **Recurring work:**
  - Migrate data from the admin system into the warehouse and reconcile it (row counts and dollar totals).
  - Build dealer-facing (client) reporting with row-level security per dealer or dealer group.
  - Produce reinsurance/participation reporting: cession, experience, reserve and trust statements, and NOL/tax support for dealer-owned obligor entities.
  - Track claims KPIs (authorization turnaround, approval/denial rate, severity, repair-facility mix).
  - Report product performance and loss ratio by product, term, dealer and vehicle class.
  - Report cancellations and chargebacks.
  - Support F&I performance and ROI tools (PVR, penetration).
- **What this means for the platform:** provide an incremental, orchestrated ELT pipeline with data contracts and reconciliation, a star-schema gold layer with one metric definition shared by every report, dealer-level row security or suppression, and cession/experience and claims-KPI marts. Those are the deliverables these teams need.
