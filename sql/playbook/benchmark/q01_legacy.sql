-- database: legacy
-- timeout: 60s
-- question: In-force contracts and coverage cost for one dealer (dealer id 37).
-- trap:     the dealer exists three times (active, an inactive upper-case copy, a "V"-prefixed re-onboarding).
--           Filtering the dealer dim by the one id the business knows finds only the active identity's rows.
set statement_timeout = '60s';

select count(*) filter (where c."fxDealerId" = 37) as in_force_contracts,
       round(sum(f."CoverageCost") filter (where c."fxDealerId" = 37), 2) as coverage_cost,
       count(*) as in_force_all_identities,
       count(distinct c."fxDealerId") as dealer_ids_for_one_dealer
from legacy_ops.vw_dim_contracts c
join legacy_ops.vw_dim_dealers d on d."DealerId" = c."fxDealerId"
left join legacy_finance.vw_fact_contracts f on f."fxContractId" = c."ContractId"
where regexp_replace(d."Number", '^V', '') = 'D1037'
  and c."CurrentContractStatus" = 'INFORCE';
