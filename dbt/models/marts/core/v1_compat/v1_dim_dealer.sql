-- DEPRECATED V1 shape (gold.dim_dealer). Same keys as core.dim_dealer; dealer_code = dealer_number.
{{ config(schema='gold', alias='dim_dealer') }}
select
    dealer_key,
    dealer_number as dealer_code,
    dealer_name,
    city,
    state,
    agent_key as agent_id,
    reinsurance_company,
    participation_label,
    dealer_status,
    onboarded_date,
    valid_from,
    timestamp '9999-12-31' as valid_to,
    is_current
from {{ ref('dim_dealer') }}
