-- pattern:  cancel export with tenant-specific exclusion lists
-- question: All cancellations entered this year, per tenant, with refund and fee, after each tenant's
--           own exclusion list is applied (every tenant drops the demo dealers and API test contracts;
--           some tenants also exclude named dealers or cancel reasons from their report).
-- trap:     tenant B also exists as a mirror schema, but it was copied a month ago and its columns
--           drifted, so it is stale. Read the live table filtered by "fxAdminCompanyId" instead, and keep
--           the exclusion lists as data (a values list) so one query serves every tenant and the
--           exclusions are visible and countable instead of buried in a where clause.

set statement_timeout = '60s';

with ex_dealer ("fxAdminCompanyId", "Number") as (
    values (1, 'D1004'), (1, 'D1037'), (2, 'D1011'), (2, 'D1017'), (3, 'D1031')
), ex_reason ("fxAdminCompanyId", "CancelReason") as (
    values (2, 'Dealer Error'), (3, 'Unwound Deal')
), base as (
    select c."fxAdminCompanyId",
           x."CancelId",
           c."ContractNumber",
           d."Number" as "Dealer Number",
           d."Dealer Name",
           c."ProductType",
           x."CancelDate",
           x."CancelReason",
           x."CancelMethod",
           x."CustomerRefund",
           x."CancelFee",
           c."fxDealerId",
           c."EnteredByUser",
           c."FullName"
    from legacy_ops.tbl_cancel x
    join legacy_ops.tbl_contract c on c."ContractId" = x."fxContractId"
    join legacy_ops.tbl_dealer d on d."DealerId" = c."fxDealerId"
    where x."CancelDate" >= date '2026-01-01'
        and x."CancelDate" <= date '2026-10-21'
)
select b."fxAdminCompanyId" as "Tenant",
       b."CancelId",
       b."ContractNumber",
       b."Dealer Number",
       b."Dealer Name",
       b."ProductType",
       b."CancelDate",
       b."CancelReason",
       b."CancelMethod",
       b."CustomerRefund",
       b."CancelFee"
from base b
where b."fxDealerId" not in (
    select "Dealer Id"
    from legacy_ops.dim_demo_dealers
)
    and b."EnteredByUser" <> 'svc_api'
    and upper(coalesce(b."FullName", '')) <> 'TEST CUSTOMER'
    and not exists (
        select 1
        from ex_dealer e
        where e."fxAdminCompanyId" = b."fxAdminCompanyId"
            and ltrim(e."Number", 'V') = ltrim(b."Dealer Number", 'V')
    )
    and not exists (
        select 1
        from ex_reason r
        where r."fxAdminCompanyId" = b."fxAdminCompanyId"
            and r."CancelReason" = b."CancelReason"
    )
order by 1, b."CancelDate";
