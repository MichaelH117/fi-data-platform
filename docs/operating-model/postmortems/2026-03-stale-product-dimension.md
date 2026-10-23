# Postmortem: recent contracts missing from product reports (stale product dimension)

*Example postmortem. The incident is a generic, made-up scenario written to show the template in
use. It is the classic failure this platform's first diagnostic was built to catch. All dates,
counts and dollars are synthetic.*

| Field | Value |
|---|---|
| Severity | Sev 2 (wrong numbers reached an internal audience) |
| Status | Postmortem complete |
| Detected | 2026-03-04 09:40, by a Financial Analyst reviewing the month-end P&L by product |
| Resolved | 2026-03-04 15:10 |
| Incident lead | Data Engineering Manager |
| Affected outputs | Product performance page, P&L by product, dealer production pivot |
| Audience reached | Internal: finance and the executive overview, for 3 business days |
| SLA impact | Met on time, but published wrong (the worse failure) |

## Summary

A new model-year plan went on sale on 2026-03-01 before it was added to the product catalog that
the warehouse reads. The product mart joined contracts to the product dimension with an **inner
join**, so the 412 contracts sold on the new plan dropped out of every product-level report. Totals
by dealer still included them, so product totals no longer tied to dealer totals. A Financial
Analyst noticed the gap during month-end review. We fixed the catalog feed, changed the join, and
added the product match-rate diagnostic that now blocks publishing.

## Impact

- 412 contracts and $508K of coverage cost were missing from product-level reports for sales
  dated 2026-03-01 to 2026-03-03. That was 6.1% of month-to-date written.
- The loss ratio for the affected tier read 2.3 points high, because earned premium was missing
  while claims on older plans were not.
- No external filing or dealer packet used the product view in that window. Nothing was re-filed.
- Product pages for the three affected days were restated with a change note.

## Timeline (local time)

| Time | Event |
|---|---|
| 03-01 00:00 | New plan year goes on sale in the admin system. The catalog extract still lists last year's plans |
| 03-02 07:00 | Daily reports publish on time. The new contracts are absent from product pages; no check fails |
| 03-04 09:40 | Financial Analyst I flags that P&L by product is $508K below the dealer production total for March |
| 03-04 09:55 | Incident opened as Sev 2. Notice sent to finance and executive subscribers: "product views understated, fix in progress" |
| 03-04 11:20 | Cause found: an inner join to a product dimension that has no rows for the new plan year |
| 03-04 13:30 | Catalog extract fixed. Product mart changed to a left join onto the unknown member |
| 03-04 14:40 | Re-run from ingest. Product and dealer totals tie |
| 03-04 15:10 | Reports republished. Restatement note added to the product page |

## Detection

A person found it, not a check, and three days late. Every existing test passed: the product
dimension was internally valid (unique, not null), and nothing compared the contracts *in* the
dimension join with the contracts *outside* it. **This is the main finding.**

## Root cause

1. Reports were missing contracts **because** the product mart used an inner join to the product
   dimension.
2. The inner join dropped rows **because** the dimension had no row for the new plan year.
3. The dimension had no row **because** the catalog extract filtered to "active plan years" from a
   config list that is updated by hand once a year, after the plans go on sale.
4. Nobody noticed **because** no check measured how many contracts matched the dimension, and the
   product and dealer totals were never reconciled against each other.

## What went well / what went poorly

- **Went well:** the analyst's habit of tying product totals to dealer totals at month end. The
  cause was found in under two hours because the gap matched one plan year exactly.
- **Went poorly:** three business days of wrong numbers. The join failed silently, and a hand-kept
  config list sat on the critical path.
- **Where we got lucky:** no regulatory filing ran off the product view that week.

## Action items

| # | Action | Type | Owner (role) | Due | Status |
|---|---|---|---|---|---|
| 1 | Add `diag_product_dim_match_rate`: by entered year, at least 99.5% of contracts must match the product dimension, or publishing is blocked | detect | Data Engineering Manager | 03-06 | Done (`dbt/tests/diagnostics/`) |
| 2 | Fact-to-dimension joins are left joins onto the `'-1'` unknown member, never inner joins | prevent | Data Engineering Manager | 03-06 | Done (`marts/core`) |
| 3 | Read the full catalog from the source, with no hand-kept plan-year list; keep history in `snap_product` | prevent | Data Engineer | 03-11 | Done |
| 4 | Reconcile row and dollar totals across layers every run, so product and dealer views tie by construction | detect | Data Engineering Manager | 03-13 | Done (`pipeline/reconcile.py`) |
| 5 | Add "new plan year on sale" to the release checklist as a source change that needs a catalog check | document | Reporting Manager | 03-06 | Done ([RELEASE.md](../RELEASE.md)) |
| 6 | Add this failure to the runbook entry for the new check | document | Reporting Manager | 03-06 | Done ([RUNBOOK.md](../RUNBOOK.md#diag_product_dim_match_rate)) |

## Verification

- `diag_product_dim_match_rate` fails when a plan year is removed from the catalog. That is the
  injected-failure pattern in `tests/test_diagnostics.py`, where a dbt diagnostic fails once its
  threshold is raised.
- The diagnostics gate blocking publish and reports is proven in `tests/test_pipeline.py`.
- The headline number of every run is in `audit.diagnostics_result` and on `site/ops.html`.
