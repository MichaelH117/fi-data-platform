-- database: legacy
-- timeout: 60s
-- question: The reportable in-force population for a compliance filing, with each contract's rep.
-- trap:     two leaks at once. API test contracts ("TEST CUSTOMER", vehicle price 1) and demo dealers are
--           still in the contract view, and the rep view joins the dealer on its "Number", which the
--           inactive dealer copy shares, so contracts of duplicated dealers are counted twice.
set statement_timeout = '60s';

select count(*) as filing_rows,
       count(distinct c."ContractId") as distinct_contracts,
       count(*) - count(distinct c."ContractId") as rows_doubled_by_fanout,
       count(distinct c."ContractId") filter (where c."FullName" = 'TEST CUSTOMER') as test_contracts,
       count(distinct c."ContractId") filter (where dd."Dealer Id" is not null) as demo_dealer_contracts
from legacy_ops.vw_dim_contracts c
join legacy_ops.vw_dim_dealers d on d."DealerId" = c."fxDealerId"
left join legacy_ops.vw_dim_reps r on r."Number" = d."Number"
left join legacy_ops.dim_demo_dealers dd on dd."Dealer Id" = c."fxDealerId"
where c."CurrentContractStatus" = 'INFORCE';
