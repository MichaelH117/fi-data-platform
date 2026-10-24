-- database: legacy
-- timeout: 120s
-- question: Loss ratio for the oldest entered year (2019) from the earnings series: paid claims over
--           earned claims reserve.
-- trap:     the earnings fact has no index on its int date id, so the date range is a full scan of millions
--           of rows. Converted contracts carry the sentinel date id 19000101 for pre-conversion months; the
--           range filter drops them, so the oldest year's earned base shrinks and its loss ratio inflates.
set statement_timeout = '120s';

with e as (
    select c."ContractId", sum(e."EarnedAmount") as earned
    from legacy_finance.cube_earned_disbursement e
    join legacy_ops.tbl_contract c on c."ContractId" = e."fxContractId"
    where e."fxEarningDateId" between 20190101 and 20261031
      and e."fxDisbursementTypeId" = 1 and extract(year from c."EnteredDate") = 2019
    group by 1
), s as (
    select sum(e."EarnedAmount") as earned
    from legacy_finance.cube_earned_disbursement e
    join legacy_ops.tbl_contract c on c."ContractId" = e."fxContractId"
    where e."fxEarningDateId" = 19000101 and e."fxDisbursementTypeId" = 1
      and extract(year from c."EnteredDate") = 2019
), p as (
    select sum(abs(cl."PaidAmount")) as paid
    from legacy_ops.tbl_claim cl
    join legacy_ops.tbl_contract c on c."ContractId" = cl."fxContractId"
    where cl."PaidDate" is not null and extract(year from c."EnteredDate") = 2019
)
select round(100 * p.paid / nullif(sum(e.earned), 0), 2) as loss_ratio_pct,
       round(sum(e.earned), 2) as earned_in_range,
       round(max(s.earned), 2) as earned_on_sentinel_date,
       round(100 * p.paid / nullif(sum(e.earned) + coalesce(max(s.earned), 0), 0), 2) as loss_ratio_with_sentinel_rows,
       round(p.paid, 2) as paid
from e, s, p
group by p.paid;
