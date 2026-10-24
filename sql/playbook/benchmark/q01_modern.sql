-- database: modern
-- timeout: 60s
-- question: In-force contracts and coverage cost for dealer id 37 on the modern star schema.
-- fix:      the dealer master merges every source identity of a dealer into one dealer_key (survivorship +
--           crosswalk), so one filter finds the whole book.
set statement_timeout = '60s';

select count(*) as in_force_contracts, round(sum(f.coverage_cost)::numeric, 2) as coverage_cost
from core.dim_contract c
join core.dim_dealer d on d.dealer_key = c.dealer_key and d.is_current
left join core.dim_contract_finance f on f.contract_key = c.contract_key
where d.dealer_id = 37 and c.status = 'INFORCE';
