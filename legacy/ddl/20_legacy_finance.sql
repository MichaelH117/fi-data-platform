-- legacy_finance: the "Finance" layer (reinsurance / earnings). Mid-migration _gen2/_gen3 copies disagree.

create table legacy_finance.tbl_fact_gross_premiums as
select d."fxContractId", d."fxTransactionTypeId", min(d."PostedDate") as "PostedDate",
       sum(d."Amount")::numeric(14, 2) as "CoverageCost",
       max(case when d."fxTransactionTypeId" = 1 then c."ContractPurchasePrice" end) as "GrossPremium"
from legacy_finance.cube_disbursement d
left join legacy_ops.tbl_contract c on c."ContractId" = d."fxContractId"
group by 1, 2;

-- the big earnings series: one row per disbursement per month, straight-line over the term.
-- "fxEarningDateId" is an int YYYYMMDD; there is NO index on it (only on "fxContractId").
-- A share of converted contracts carry the sentinel 19000101 for their pre-conversion months.
create table legacy_finance.cube_earned_disbursement (
    "fxContractId" bigint, "fxDisbursementId" bigint, "fxAccountHolderId" bigint, "fxDisbursementTypeId" integer,
    "fxEarningDateId" integer, "EarnedAmount" numeric(14, 4));

insert into legacy_finance.cube_earned_disbursement
select d."fxContractId", d."DisbursementId", d."fxAccountHolderId", d."fxDisbursementTypeId",
       case when c."ConversionDate" = date '1900-01-01' and m < date '${cutover}' then 19000101
            else to_char((m + interval '1 month' - interval '1 day')::date, 'YYYYMMDD')::int end,
       round(d."Amount" / greatest(c."TermMonths", 1), 4)
from legacy_finance.cube_disbursement d
join legacy_ops.tbl_contract c on c."ContractId" = d."fxContractId"
left join (select "fxContractId", min("CancelDate") as cancel_date from legacy_ops.tbl_cancel group by 1) x
       on x."fxContractId" = d."fxContractId"
cross join lateral generate_series(
    date_trunc('month', c."EffectiveDate"),
    least(c."EffectiveDate" + make_interval(months => c."TermMonths"::int) - interval '1 day',
          coalesce(x.cancel_date, date '${as_of}'), date '${as_of}'),
    interval '1 month') as m
where d."fxTransactionTypeId" = 1 and d."fxDisbursementTypeId" in (1, 2) and c."EffectiveDate" is not null;

create view legacy_finance.vw_fact_contracts as
select c."ContractId" as "fxContractId", c."fxDealerId", c."fxProductId", c."fxAdminCompanyId", c."EnteredDate",
       c."CurrentContractStatus", g."GrossPremium", g."CoverageCost"
from legacy_ops.vw_contract_base c
left join (select "fxContractId", max("GrossPremium") as "GrossPremium",
                  sum("CoverageCost") filter (where "fxTransactionTypeId" in (1, 3, 4)) as "CoverageCost"
           from legacy_finance.tbl_fact_gross_premiums group by 1) g on g."fxContractId" = c."ContractId";

-- gen2 copy: stopped loading ${gen2_cutoff}, and joins disbursements to NON-deduped transactions (inflates)
create view legacy_finance.vw_fact_contracts_gen2 as
select c."ContractId" as "fxContractId", c."fxDealerId", c."fxProductId", c."EnteredDate",
       sum(d."Amount") as "CoverageCost"
from legacy_ops.tbl_contract c
join legacy_finance.cube_transaction t on t."fxContractId" = c."ContractId" and t."fxTransactionTypeId" in (1, 3, 4)
join legacy_finance.cube_disbursement d on d."fxTransactionId" = t."TransactionId"
where c."EnteredDate" <= date '${gen2_cutoff}'
group by 1, 2, 3, 4;

-- the refund waterfall: no cancel date on it
create view legacy_finance.vw_fact_cancels as
select x."fxContractId", x."CustomerRefund", x."CancelFee"
from legacy_ops.tbl_cancel x;

-- gen3 copy: refunds negated and zero-refund cancels dropped
create view legacy_finance.vw_fact_cancels_gen3 as
select x."fxContractId", x."CancelDate", -x."CustomerRefund" as "RefundAmount"
from legacy_ops.tbl_cancel x
where x."CustomerRefund" > 0;

-- gen2 earned reserve: silently drops the sentinel rows (date id filter)
create view legacy_finance.vw_fact_earned_reserve_gen2 as
select e."fxContractId", e."fxEarningDateId" / 100 as "EarningMonthId", sum(e."EarnedAmount") as "EarnedReserve"
from legacy_finance.cube_earned_disbursement e
where e."fxDisbursementTypeId" = 1 and e."fxEarningDateId" >= 20190101
group by 1, 2;

create view legacy_finance.vw_dim_cession_type as
select t."CessionTypeId", t."CessionType" from legacy_finance.tbl_cession_type t;
