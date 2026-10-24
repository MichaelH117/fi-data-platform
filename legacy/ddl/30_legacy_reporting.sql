-- legacy_reporting: the "Reporting" layer. Thin base facts keyed by provider entity ("fxProducerId"),
-- and a contract "dim" that is really a UNION ALL spine over the facts.

create table legacy_reporting."FactContracts" as
select c."ContractId" as "fxContractId", c."fxProviderId" as "fxProducerId", c."fxProductId" as "fxProductKey",
       c."fxDealerId", to_char(c."EnteredDate", 'YYYYMMDD')::int as "EnteredDateId"
from legacy_ops.tbl_contract c;

create table legacy_reporting."FactCancels" as
select x."fxContractId", c."fxProviderId" as "fxProducerId",
       to_char(x."CancelDate", 'YYYYMMDD')::int as "CancelDateId", x."CustomerRefund" as "Refund Amount"
from legacy_ops.tbl_cancel x
left join legacy_ops.tbl_contract c on c."ContractId" = x."fxContractId";

-- "Claim Amount" is POSITIVE here (negative in legacy_ops.vw_dim_claims); no claim status
create table legacy_reporting."FactClaims" as
select cl."fxContractId", c."fxProviderId" as "fxProducerId", cl."ClaimNumber" as "Claim Number",
       abs(cl."PaidAmount") as "Claim Amount", cl."ReportedDate" as "Created Date"
from legacy_ops.tbl_claim cl
left join legacy_ops.tbl_contract c on c."ContractId" = cl."fxContractId";

create table legacy_reporting."FactEarnings" as
select e."fxContractId", c."fxProviderId" as "fxProducerId", e."fxEarningDateId", e."EarnedAmount"
from legacy_finance.cube_earned_disbursement e
left join legacy_ops.tbl_contract c on c."ContractId" = e."fxContractId";

create table legacy_reporting."DimProducer" as
select p."ProviderId" as "ProducerId", p."ProviderName" as "Producer" from legacy_ops.tbl_provider p;

-- "Producer" here = the PROVIDER ENTITY; legacy_ops "Producers" = the selling dealer. Same word, two meanings.
create view legacy_reporting.vw_dim_producers as
select p."ProducerId", p."Producer" from legacy_reporting."DimProducer" p;

create view legacy_reporting.vw_fact_contracts as
select f."fxContractId", f."fxProducerId", f."fxProductKey", f."fxDealerId", f."EnteredDateId"
from legacy_reporting."FactContracts" f;

create view legacy_reporting.vw_fact_claims as
select f."fxContractId", f."fxProducerId", f."Claim Number", f."Claim Amount", f."Created Date"
from legacy_reporting."FactClaims" f;

create view legacy_reporting.vw_dim_product as
select p."ProductKey", p."Product Code", p."Product", p."Product Type", p."Plan Year"
from legacy_reporting."DimProduct" p;

-- the contract "dim": every contract id seen in any fact, DISTINCT at query time, plus a coverage cost
-- aggregated over ALL disbursements. The materialized CTEs run in full before any outer WHERE applies.
create view legacy_reporting.vw_dim_contracts as
with spine as materialized (
    select "fxContractId" from legacy_reporting."FactContracts"
    union all select "fxContractId" from legacy_reporting."FactCancels"
    union all select "fxContractId" from legacy_reporting."FactClaims"
    union all select "fxContractId" from legacy_reporting."FactEarnings"
), ids as materialized (
    select distinct "fxContractId" from spine
), txn as materialized (
    select "TransactionId", max("fxCashTransactionId") as "fxCashTransactionId", count(*) as "Count"
    from legacy_finance.cube_transaction group by 1
), cost as materialized (
    select d."fxContractId", sum(d."Amount") as "CoverageCost"
    from legacy_finance.cube_disbursement d
    join txn t on t."TransactionId" = d."fxTransactionId"
    where d."fxTransactionTypeId" in (1, 3, 4)
    group by 1
)
select ids."fxContractId" as "ContractId", c."ContractNumber", c."CurrentContractStatus", c."EnteredDate",
       c."EffectiveDate", c."Reportable State", c."ProductType", c."ProductSubType", c."fxDealerId",
       c."fxProviderId" as "fxProducerId", c."ContractPurchasePrice", cost."CoverageCost"
from ids
left join legacy_ops.tbl_contract c on c."ContractId" = ids."fxContractId"
left join cost on cost."fxContractId" = ids."fxContractId";
