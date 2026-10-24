-- tenant_b: Tenant B as a full MIRROR schema of legacy_ops instead of a tenant column. It was copied at
-- ${tenant_b_cutoff} (it lags), and its columns drifted: one renamed, one missing.

create table tenant_b.tbl_contract as
select * from legacy_ops.tbl_contract
where "fxAdminCompanyId" = ${tenant_b} and "EnteredDate" <= date '${tenant_b_cutoff}';
alter table tenant_b.tbl_contract rename column "CurrentContractStatus" to "ContractStatus";
alter table tenant_b.tbl_contract drop column "fxProviderId";

create table tenant_b.tbl_dealer as
select * from legacy_ops.tbl_dealer where "fxAdminCompanyId" = ${tenant_b};

create table tenant_b.tbl_product as select * from legacy_ops.tbl_product;

create table tenant_b.tbl_claim as
select cl.* from legacy_ops.tbl_claim cl
where cl."ReportedDate" <= date '${tenant_b_cutoff}'
  and cl."fxContractId" in (select "ContractId" from tenant_b.tbl_contract);

create table tenant_b.tbl_cancel as
select x.* from legacy_ops.tbl_cancel x
where x."CancelDate" <= date '${tenant_b_cutoff}'
  and x."fxContractId" in (select "ContractId" from tenant_b.tbl_contract);

create view tenant_b.vw_dim_contracts as
select c."ContractId", c."ContractNumber", c."fxDealerId", c."fxProductId", c."EnteredDate", c."EffectiveDate",
       c."ExpirationDate", c."ContractStatus", c."ContractPurchasePrice", c."VIN", c."Reportable State",
       c."ProductType", c."ProductSubType", c."FullName", c."PhysicalState", d."Dealer Name" as "Producers"
from tenant_b.tbl_contract c
left join tenant_b.tbl_dealer d on d."DealerId" = c."fxDealerId";

create view tenant_b.vw_dim_dealers as
select d."DealerId", d."Dealer Name", d."Number", d."PhysicalCity", d."PhysicalState", d."Status"
from tenant_b.tbl_dealer d;

create view tenant_b.vw_dim_products as select p.* from tenant_b.tbl_product p;

create view tenant_b.vw_dim_claims as
select c."ClaimId", c."ClaimNumber", c."fxContractId", c."ReportedDate", c."ClaimStatus" as "Claim Status",
       -abs(c."PaidAmount") as "ClaimAmount"
from tenant_b.tbl_claim c;

create view tenant_b.vw_dim_cancels as
select x."CancelId", x."fxContractId", x."CancelDate", x."CancelReason", x."CustomerRefund"
from tenant_b.tbl_cancel x;
