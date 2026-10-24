-- pattern:  loss ratio from the earnings series, with the sentinel-date filter
-- question: Monthly loss ratio (claims paid / earned claims reserve) since Jan 2025, and the same ratio
--           by entered-year cohort over the whole book.
-- trap:     converted contracts carry the sentinel date id 19000101 for their pre-conversion months.
--           to_date() on it makes a "January 1900" bucket, and a lazy ">= 20190101" filter silently drops
--           that earned money from every cohort total. For a CALENDAR view, filter an explicit integer range
--           (sargable, and it states the sentinel is out on purpose). For a COHORT view, keep the sentinel
--           rows: their month is unknown but their contract is not. The table is 3M+ rows with no index on
--           the date id, so raise the timeout for this script, explain first, and set it back at the end.

set statement_timeout = '5min';

explain select "fxEarningDateId" / 100 as month_id, sum("EarnedAmount")
from legacy_finance.cube_earned_disbursement
where "fxDisbursementTypeId" = 1
    and "fxEarningDateId" between 20250101 and 20261031
group by 1;

select count(*) filter (where "fxEarningDateId" = 19000101) as sentinel_rows,
       sum("EarnedAmount") filter (where "fxEarningDateId" = 19000101) as sentinel_earned,
       round(100.0 * sum("EarnedAmount") filter (where "fxEarningDateId" = 19000101) / sum("EarnedAmount"), 3) as sentinel_pct
from legacy_finance.cube_earned_disbursement
where "fxDisbursementTypeId" = 1;

with e as (
    select extract(year from c."EnteredDate")::int as entered_year,
           sum(x."EarnedAmount") as earned
    from legacy_finance.cube_earned_disbursement x
    join legacy_ops.tbl_contract c on c."ContractId" = x."fxContractId"
    where x."fxDisbursementTypeId" = 1
    group by 1
), p as (
    select extract(year from c."EnteredDate")::int as entered_year,
           -sum(cl."ClaimAmount") as paid
    from legacy_ops.vw_dim_claims cl
    join legacy_ops.tbl_contract c on c."ContractId" = cl."fxContractId"
    where cl."Claim Status" in ('CLOSED_FULLY_PAID', 'CLOSED_PARTIALLY_PAID', 'PAID')
    group by 1
)
select entered_year,
       e.earned,
       p.paid,
       round(100 * p.paid / nullif(e.earned, 0), 1) as loss_ratio_pct
from e
full join p using (entered_year)
order by 1;

with e as (
    select "fxEarningDateId" / 100 as month_id,
           sum("EarnedAmount") as earned
    from legacy_finance.cube_earned_disbursement
    where "fxDisbursementTypeId" = 1
        and "fxEarningDateId" between 20250101 and 20261031
    group by 1
), p as (
    select to_char("PaidDate", 'YYYYMM')::int as month_id,
           -sum("ClaimAmount") as paid
    from legacy_ops.vw_dim_claims
    where "Claim Status" in ('CLOSED_FULLY_PAID', 'CLOSED_PARTIALLY_PAID', 'PAID')
        and "PaidDate" between date '2025-01-01' and date '2026-10-31'
    group by 1
)
select month_id,
       e.earned,
       p.paid,
       round(100 * p.paid / nullif(e.earned, 0), 1) as loss_ratio_pct
from e
full join p using (month_id)
order by 1;

set statement_timeout = '60s';
