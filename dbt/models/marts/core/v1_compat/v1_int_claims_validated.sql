-- DEPRECATED V1 name (silver.int_claims_validated): every reportable claim with its validity flags.
-- count(this) - count(gold.fct_claim) = claims with no contract key or a loss dated before the sale.
{{ config(schema='silver', alias='int_claims_validated') }}
select
    c.claim_key as claim_id,
    c.contract_key as contract_id,
    c.loss_date,
    not c.has_contract_key as is_orphan,
    c.loss_date < k.entered_date is true as is_before_sale,
    c.has_contract_key and c.loss_date >= k.entered_date is true as is_valid
from {{ ref('dim_claim') }} as c
left join {{ ref('dim_contract') }} as k using (contract_key)
