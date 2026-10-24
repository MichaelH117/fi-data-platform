-- database: modern
-- timeout: 2s
-- question: Contract attributes and coverage cost for provider entity "Obligor 1" on the modern star schema.
-- fix:      one contract dim TABLE with a primary key; the provider filter is a plain predicate on it and the
--           coverage cost is a one-to-one finance dim, so the lookup reads only the population.
set statement_timeout = '2s';

select count(*) as contracts, round(sum(f.coverage_cost)::numeric, 2) as coverage_cost,
       count(*) filter (where c.status = 'INFORCE') as in_force
from core.dim_contract c
join core.dim_obligor o on o.obligor_key = c.current_obligor_key
left join core.dim_contract_finance f on f.contract_key = c.contract_key
where o.obligor_id = 1;
