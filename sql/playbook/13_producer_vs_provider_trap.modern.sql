-- pattern:  producer vs provider filter trap (modern star schema)
-- question: The same count on the modern contract dim, where the provider entity and the selling dealer
--           are two differently named keys.
-- trap:     none expected: current_obligor_key and dealer_key cannot be confused, and the obligor dim
--           also keeps the original obligor, so a transferred book is counted on the right basis.

set statement_timeout = '30s';

select o.obligor_name,
       count(*) filter (where c.current_obligor_key = o.obligor_key) as current_basis,
       count(*) filter (where c.original_obligor_key = o.obligor_key) as original_basis
from core.dim_contract c
join core.dim_obligor o on o.obligor_key in (c.current_obligor_key, c.original_obligor_key)
where o.obligor_name ilike 'obligor 3%'
group by 1;
