-- pattern:  claim-grain export with status / substatus
-- question: One row per claim reported this year, with contract, dealer, status AND substatus, and a
--           paid amount the business can sum, for a "decision-free" flat export sliced later in Excel.
-- trap:     the ops claim view shows the amount NEGATIVE, converted claims have no substatus at all
--           (NULL, not "unknown"), and denied claims carry a 0 paid amount that is not "no claim".
--           Export the sign-fixed amount, label the missing substatus, and keep the status pair intact so
--           CLOSED_PARTIALLY_PAID / PAID is never collapsed into "paid".

set statement_timeout = '60s';

select cl."ClaimNumber" as "Claim Number",
       case when cl."ClaimNumber" ~ '^0' then 'CONVERSION' else 'CORE' end as "Source",
       c."ContractNumber" as "Contract Number",
       d."Dealer Name",
       c."Reportable State",
       c."ProductType",
       cl."ReportedDate" as "Created Date",
       cl."LossDate" as "Loss Date",
       cl."Claim Status",
       coalesce(cl."Claim Substatus", '(none in source)') as "Claim Substatus",
       cl."RequestedAmount" as "Requested",
       -cl."ClaimAmount" as "Claims Paid",
       case when cl."Claim Status" in ('CLOSED_FULLY_PAID', 'CLOSED_PARTIALLY_PAID', 'PAID') then 1 else 0 end as "Is Paid",
       case when cl."Claim Status" = 'DENIED' then 1 else 0 end as "Is Denied",
       1 as "Claims Count"
from legacy_ops.vw_dim_claims cl
left join legacy_ops.tbl_contract c on c."ContractId" = cl."fxContractId"
left join legacy_ops.tbl_dealer d on d."DealerId" = c."fxDealerId"
where cl."ReportedDate" >= date '2026-01-01'
    and cl."ReportedDate" <= date '2026-10-21'
order by cl."ReportedDate", 1;
