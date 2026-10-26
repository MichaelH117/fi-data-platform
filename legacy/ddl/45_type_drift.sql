-- Type drift (V7 edit 31): the old accounting extract ("_acct" tables) stored the same values with other
-- types than the ops and reporting layers. Joins and comparisons across layers then fail:
--   * amounts are Postgres money here (numeric elsewhere): money = numeric raises 42883 (no such operator);
--   * reserves stayed numeric, so premium minus reserve needs a cast first;
--   * the contract id is padded text, character(12), against bigint "ContractId": 42883 again;
--   * the cancel period is a text month plus a double precision year, not a date;
--   * yes/no flags are bigint 0/1, not boolean;
--   * the dealer number is "Dealer Number" in vw_dealer_premium but "Number" in legacy_ops.vw_dim_dealers,
--     so a query written against one view raises 42703 (no such column) on the other.
-- The existing tables and views are unchanged; these sit next to them, as they did in the old warehouse.

create table legacy_finance.tbl_premium_acct as
select lpad(c."ContractId"::text, 12, '0')::character(12) as "ContractId",
       c."ContractPurchasePrice"::money as "GrossPremium",
       coalesce(r."ReserveAmount", 0)::numeric(14, 2) as "ReserveAmount",
       (c."SourceSystem" = 'CONVERSION')::int::bigint as "IsConverted",
       c."fxDealerId", c."EnteredDate"
from legacy_ops.tbl_contract c
left join (select "fxContractId", sum("Amount") as "ReserveAmount" from legacy_finance.cube_disbursement
           where "fxTransactionTypeId" = 1 and "fxDisbursementTypeId" = 1 group by 1) r
       on r."fxContractId" = c."ContractId";

create table legacy_finance.tbl_cancel_acct as
select lpad(x."fxContractId"::text, 12, '0')::character(12) as "ContractId",
       to_char(x."CancelDate", 'MM') as "CancelMonth",
       extract(year from x."CancelDate")::double precision as "CancelYear",
       x."CustomerRefund"::money as "RefundAmount",
       x."CancelFee"::money as "CancelFee",
       (x."CustomerRefund" < 0)::int::bigint as "IsNegativeRefund"
from legacy_ops.tbl_cancel x;

create table legacy_finance.tbl_claim_acct as
select lpad(cl."fxContractId"::text, 12, '0')::character(12) as "ContractId",
       cl."ClaimNumber",
       abs(cl."PaidAmount")::money as "PaidAmount",
       (cl."ClaimStatus" = 'DENIED')::int::bigint as "IsDenied",
       extract(year from cl."ReportedDate")::double precision as "ReportedYear"
from legacy_ops.tbl_claim cl;

create view legacy_finance.vw_dealer_premium as
select d."Number" as "Dealer Number", d."Dealer Name", sum(p."GrossPremium") as "GrossPremium",
       sum(p."ReserveAmount") as "ReserveAmount", count(*) as "Contracts"
from legacy_finance.tbl_premium_acct p
join legacy_ops.tbl_dealer d on d."DealerId" = p."fxDealerId"
group by 1, 2;
