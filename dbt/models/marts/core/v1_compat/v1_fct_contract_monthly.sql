-- DEPRECATED V1 shape (gold.fct_contract_monthly): core.fct_contract_monthly with the V1
-- product_code (subcategory-term) and contract_id. Use core.fct_contract_monthly instead.
{{ config(schema='gold', alias='fct_contract_monthly') }}
select
    m.* replace (s.product_code as product_code),
    s.product_key as v1_product_key
from {{ ref('fct_contract_monthly') }} as m
inner join {{ ref('fct_contract_sale') }} as s using (contract_key)
