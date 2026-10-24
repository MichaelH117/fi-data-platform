# DAX to SQL: how a report number is rebuilt in SQL

A Power BI visual does not run one measure. It runs a measure **inside a filter context**: the
visual's axis and legend fields, its own filters, the page and report filters, any calculation item,
the relationships between the tables and the user's RLS role. To get the same number in SQL you have
to rebuild all of that, not only the formula. This page is the method, with five worked examples.

The SQL style matches the analyst playbook (`sql/playbook/`, see its README): lowercase keywords,
positional `group by`, `set statement_timeout` first. The examples run against the modern marts on the
local Postgres (`vsc_analytics`, schemas `core`, `claims`); the legacy translations run against the
legacy database (`vsc_legacy`). The playbook's DAX-measure file and the legacy-vs-modern benchmark pairs
in `sql/playbook/benchmark/` hold the runnable copies with expected row counts; this page explains why
each SQL looks the way it does and does not repeat them line for line.

## The method, step by step

1. **Find the visual's fields.** Open `Report_Field_Usage.csv` (from `python -m semantic export-docs`,
   see [`docs/SEMANTIC_STRATEGY.md`](SEMANTIC_STRATEGY.md)). Filter on the page and visual title. Each row
   is one field in one role: `Category`/`Rows`/`Columns`/`Legend` become the `group by`, `Values`/`Y`
   are the measures, `filter` rows become the `where` (the `filter_values` column has the selected
   values), and a `Time Intelligence` filter tells you which calculation item applies.
2. **Get each measure's DAX and dependencies.** `Model_Documentation.csv` has the full expression;
   `Measure_Report_Map.csv` lists `depends_on`, so a ratio is expanded into its inputs before you write
   SQL. The `dbt_metric` column points at the one definition in `dbt/models/metrics.yml`, which is the
   tie-breaker if DAX and SQL disagree.
3. **Turn columns into physical names.** `Report_Field_Usage.csv` already carries `source_table` and
   `source_column` for every column field (`Posting Period[Fiscal Year]` is
   `core.dim_posting_period.fiscal_year`).
4. **Turn relationships into joins.** Copy the `sql_join` from `Model_Relationships.csv` for each dim
   the visual touches. A Power BI relationship behaves like a `left join` from the fact to the dim
   (unmatched rows land on a blank member), so use `left join`, never an inner join that drops rows.
   Our dims have an unknown member (`-1`), so the left join and an inner join agree; in the legacy
   database they do not (playbook `03_diag_dim_match_by_year.sql` measures the gap).
5. **Aggregate at the right grain.** Additive measures (`SUM`, `COUNTROWS`) are summed per group. A
   ratio is **sum over sum per group**, never an average of row ratios. A balance (in force, unearned)
   takes **one** period, never a sum across periods.
6. **Apply the calculation item** as a `where` on `posting_period_key` (key arithmetic on yyyymm),
   capped at `core.dim_reporting_date.last_closed_period_key`.
7. **Apply RLS** if the number is for a dealer or tenant user: join `core.dim_user_dealer_access` on
   the role's key, the same map the model's roles read (`Model_Documentation.csv`, `rls_filter` rows).
8. **Check against ground truth.** Performance Analyzer in Power BI Desktop ("Copy query") gives the
   exact DAX the visual sent. Run it in DAX Studio and compare the totals to your SQL. Differences are
   almost always step 4 (a dropped row), 5 (wrong grain) or 6 (open month included).

## Worked examples

### 1. A sum with a calculation item: Claims Paid by participation structure, T12M

Visual: *Finance* page, "Cession by participation structure · T12M". Fields: `Dealer[Participation
Structure]` on Category, `Claims Paid` on Y, filter `Time Intelligence[Time Calculation] = T12M`.

```dax
Claims Paid = SUM('Claim Payment Line'[Paid Amount])
-- T12M item: CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS('Posting Period'),
--   'Posting Period'[Posting Period Key] <= _p, 'Posting Period'[Posting Period Key] > _p - 100)
--   where _p = MIN(MAX(selected period key), MAX('Reporting Date'[Last Closed Period Key]))
```

```sql
set statement_timeout = '60s';

with cap as (select last_closed_period_key as p from core.dim_reporting_date)
select d.participation_structure, sum(f.paid_amount) as claims_paid
from core.fct_claim_payment_line f
left join core.dim_dealer d on f.dealer_key = d.dealer_key
cross join cap
where f.posting_period_key <= cap.p and f.posting_period_key > cap.p - 100
group by 1
order by 1;
```

Why: `REMOVEFILTERS('Posting Period')` plus the two key conditions is exactly the `where` on the
fact's `posting_period_key`. `p - 100` on a yyyymm key is "same month last year", so the window is the
12 closed months. No sign fix is needed: `paid_amount` is positive after staging.

### 2. A ratio: Loss Ratio (on earned premium) by fiscal year

Visual: *Executive overview*, "Loss ratio by fiscal year". Fields: `Posting Period[Fiscal Year]` on
Category, the measure on Y, filter `Posting Period[Is Closed] = true`.

```dax
Loss Ratio (on earned premium) = DIVIDE([Incurred Claims (accident month)], [Earned Premium])
Incurred Claims (accident month) = SUM('Contract Month'[Incurred Claims])
Earned Premium = SUM('Contract Month'[Earned Premium])
```

```sql
set statement_timeout = '60s';

select p.fiscal_year, sum(f.incurred_claims) / nullif(sum(f.earned_premium), 0) as loss_ratio
from core.fct_contract_monthly f
left join core.dim_posting_period p on f.posting_period_key = p.posting_period_key
where p.is_closed
group by 1
order by 1;
```

Why: `DIVIDE(a, b)` returns BLANK when `b` is 0; `nullif(b, 0)` gives the same NULL. Each input is
summed for the fiscal year first and then divided. `avg(incurred_claims / earned_premium)` over rows
would weight a one-day contract the same as a full year and gives a different, wrong number. The
earnings-series version of the same question, with the legacy sentinel-date trap, is playbook
`14_loss_ratio_earnings_sentinel.sql` (legacy) and `.modern.sql`.

### 3. A balance: the Contracts In Force card

Visual: *Executive overview*, card "Contracts in force". No axis, so the filter context is the whole
book.

```dax
Contracts In Force =
VAR _cap = MAX('Reporting Date'[Last Closed Period Key])
VAR _period = CALCULATE(MAX('Contract Month'[Posting Period Key]), 'Contract Month'[Posting Period Key] <= _cap)
RETURN CALCULATE(CALCULATE(COUNTROWS('Contract Month'), 'Contract Month'[Is In Force Eom] = TRUE()),
                 'Posting Period'[Posting Period Key] = _period)
```

```sql
set statement_timeout = '60s';

with cap as (select last_closed_period_key as p from core.dim_reporting_date),
     latest as (select max(f.posting_period_key) as k from core.fct_contract_monthly f, cap where f.posting_period_key <= cap.p)
select count(*) as contracts_in_force
from core.fct_contract_monthly f
join latest on f.posting_period_key = latest.k
where f.is_in_force_eom;
```

Why: the count is a month-end **snapshot**. Summing `is_in_force_eom` over 12 months counts each
contract 12 times. The DAX takes the latest period at or before the last closed one, so the card never
shows the open, partial month; the SQL does the same with `latest`.

### 4. An inactive relationship: Claims Opened by reported month

`Model_Relationships.csv` shows `Claim Summary.reported_month -> Posting Period` with `is_active =
false`. Every other claim measure slices by **loss** month; this one switches to the reported month.

```dax
Claims Opened = CALCULATE(COUNTROWS('Claim Summary'),
                          USERELATIONSHIP('Claim Summary'[Reported Month], 'Posting Period'[Period Start]))
```

```sql
set statement_timeout = '60s';

select p.posting_period_key, count(*) as claims_opened
from claims.mart_claim_summary c
left join core.dim_posting_period p on c.reported_month = p.period_start
group by 1
order by 1;
```

Why: `USERELATIONSHIP` means "join on the other date for this measure only". In SQL that is simply a
different `on` clause, so it is easy to get wrong silently: join on `loss_month` and the numbers look
plausible but answer a different question.

### 5. Legacy: the same measure name, two answers

The legacy models define `Contracts` and `Claims Paid` differently per model
(`legacy/semantic_models/README.md`). Translated literally:

```dax
-- Ops model            Contracts   = COUNTROWS(Contracts)                          -- legacy_ops.vw_dim_contracts
-- Reporting model      Contracts   = DISTINCTCOUNT('Fact Contracts'[fxContractId]) -- legacy_reporting.vw_fact_contracts
-- Ops model            Claims Paid = SUM(Claims[ClaimAmount])                      -- legacy_ops.vw_dim_claims
-- Reporting model      Claims Paid = SUM('Fact Claims'[Claim Amount])               -- legacy_reporting.vw_fact_claims
```

```sql
set statement_timeout = '60s';

select (select count(*) from legacy_ops.vw_dim_contracts) as ops_contracts,
       (select count(distinct "fxContractId") from legacy_reporting.vw_fact_contracts) as reporting_contracts,
       (select sum("ClaimAmount") from legacy_ops.vw_dim_claims) as ops_claims_paid,
       (select sum("Claim Amount") from legacy_reporting.vw_fact_claims) as reporting_claims_paid;
```

Why it matters: on the current synthetic build the two `Contracts` agree (59,985 each), but only because
the ops view happens to have one row per contract; one duplicated join upstream and `COUNTROWS` drifts
while `DISTINCTCOUNT` does not. The two claim totals have opposite signs: -12,848,708.52 in the ops
layer and +12,848,708.52 in reporting. The translation is mechanical; deciding which number is "right" is not,
and that is the cost of one model per schema. In the modern models both names have one definition from
`metrics.yml`, so examples 1-4 give the same answer in every domain model.

## Things that do not translate one to one

| DAX behaviour | SQL equivalent | Trap |
|---|---|---|
| Relationship (many to one, single direction) | `left join` fact -> dim | an inner join drops orphans; a dim with duplicate keys (legacy dealers) multiplies fact rows |
| `DIVIDE(a, b)` | `a / nullif(b, 0)` | dividing averages, or dividing before grouping |
| `DISTINCTCOUNT` | `count(distinct ...)` | `DISTINCTCOUNT` counts BLANK as a value; `count(distinct)` ignores NULL |
| Blank member for unmatched rows | the `-1` unknown member (modern) or `coalesce(..., '(blank)')` | legacy totals change with the join type |
| Calculation item | `where` on `posting_period_key`, capped at the last closed period | forgetting the cap includes the open month |
| RLS role | join `core.dim_user_dealer_access` on the role's key | testing as an admin hides a missing filter |
| Visual totals | a separate query without the `group by` (or `grouping sets`) | a ratio's total is not the sum of the rows' ratios |
