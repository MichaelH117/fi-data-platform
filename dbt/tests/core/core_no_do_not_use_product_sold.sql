-- V6 edit 16: no do-not-use product on a sales page. A "- DNU" product twin reuses a live product's
-- code; it must never carry a contract, and never reach dim_product (every sales page reads it).
select 'contract' as found_in, c.contract_key as id
from {{ ref('int_contracts_conformed') }} as c
inner join {{ ref('stg_admin_core__product') }} as p using (product_id)
where p.is_do_not_use

union all

select 'dim_product', product_key
from {{ ref('dim_product') }}
where is_do_not_use
