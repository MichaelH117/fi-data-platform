-- database: legacy
-- timeout: 2s
-- question: Contract attributes (status, state, product type, coverage cost) for one provider entity's
--           population, the lookup a report visual or a scoped export fires on every click.
-- trap:     the reporting contract "dim" is a view: a UNION ALL spine of contract ids over four facts
--           (including the multi-million-row earnings series), DISTINCT at query time, left-joined to a
--           coverage-cost aggregate over every disbursement. The provider filter cannot be pushed into the
--           materialized steps, so every click rebuilds the whole spine. The budget here is an interactive
--           2 s (a report visual), not a batch window.
set statement_timeout = '2s';

select count(*) as contracts, round(sum("CoverageCost"), 2) as coverage_cost,
       count(*) filter (where "CurrentContractStatus" = 'INFORCE') as in_force
from legacy_reporting.vw_dim_contracts
where "fxProducerId" = 1;
