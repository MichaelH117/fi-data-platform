-- database: modern
-- timeout: 60s
-- question: The book for provider entity "Obligor 1" on the modern star schema.
-- fix:      dim_producer labels every producer with producer_role (selling_dealer | provider_entity) and the
--           contract dim carries both keys under different names, so the wrong column cannot be picked.
set statement_timeout = '60s';

select count(*) as contracts
from core.dim_contract c
join core.dim_producer p on p.producer_key = c.provider_entity_key
where p.producer_role = 'provider_entity' and p.producer_name ilike 'obligor 1 %';
