-- legacy_ops: the "Ops" layer (dealer + customer views). Views over views over tbl_* base tables.
-- Deliberately legacy: quoted CamelCase columns, no primary or foreign keys, no comments, no owners.

create view legacy_ops.vw_contract_base as
select c.* from legacy_ops.tbl_contract c;

-- the main contract "dim": denormalized CUSTOMER fields; "Producers" is the SELLING DEALER name
create view legacy_ops.vw_dim_contracts as
select b."ContractId", b."ContractNumber", b."fxDealerId", b."fxProductId", b."fxInsurerId", b."fxAdminCompanyId",
       b."EnteredDate", b."EffectiveDate", b."ExpirationDate", b."CurrentContractStatus", b."TermMonths",
       b."TermMiles", b."ContractPurchasePrice", b."VehiclePurchasePrice", b."VIN", b."VehicleYear",
       b."VehicleMake", b."VehicleModel", b."NewUsed", b."Reportable State", b."ProductType", b."ProductSubType",
       b."FirstName", b."LastName", b."FullName", b."Email", b."PhysicalState", b."PhysicalZipCode",
       b."EnteredByUser", b."SourceSystem",
       d."Dealer Name" as "Producers",
       p."ProviderCode",
       case when b."fxProviderId" is null then 'NON PAR' else 'PAR' end as "PAR/NP"
from legacy_ops.vw_contract_base b
left join legacy_ops.tbl_dealer d on d."DealerId" = b."fxDealerId"
left join legacy_ops.tbl_provider p on p."ProviderId" = b."fxProviderId";

-- one dealer can exist as several "DealerId" rows: active, inactive copy, prefixed "Number"
create view legacy_ops.vw_dim_dealers as
select d."DealerId", d."Dealer Name", d."Number", d."PhysicalCity", d."PhysicalState", d."Status",
       d."fxAdminCompanyId"
from legacy_ops.tbl_dealer d;

-- STALE: the product table stopped refreshing; the latest plan year is missing
create view legacy_ops.vw_dim_products as
select p."ProductId", p."ProductCode", p."Product", p."ProductType", p."ProductSubType", p."Category", p."PlanYear"
from legacy_ops.tbl_product p;

-- claims with status/substatus; the amount is shown NEGATIVE in this layer
create view legacy_ops.vw_dim_claims as
select c."ClaimId", c."ClaimNumber", c."fxContractId", c."LossDate", c."ReportedDate", c."PaidDate",
       c."ClaimStatus" as "Claim Status", c."ClaimSubstatus" as "Claim Substatus", c."RequestedAmount",
       -abs(c."PaidAmount") as "ClaimAmount"
from legacy_ops.tbl_claim c;

create view legacy_ops.vw_dim_cancels as
select x."CancelId", x."fxContractId", x."CancelDate", x."CancelReason", x."CancelMethod", x."CustomerRefund",
       x."CancelFee"
from legacy_ops.tbl_cancel x;

-- VIN-keyed, raw text: join with upper(trim(s."VIN")) = upper(trim(c."VIN"))
create view legacy_ops.vw_dim_stock_number as
select s.stock_number, s."VIN", s."DealerNumber", s."ReceivedDate"
from legacy_ops.tbl_stock_number s;

-- joins the dealer on "Number" (NOT "Dealer Number"); the inactive copy shares the number, so it fans out
create view legacy_ops.vw_dim_reps as
select r."RepId", r."Reps Name", r."Number", d."DealerId", d."Dealer Name"
from legacy_ops.tbl_rep r
join legacy_ops.tbl_dealer d on d."Number" = r."Number";
