-- Contract terms (one row per reportable contract): term, mileage, waiting period, deductible,
-- coverage limit, options and surcharges.
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
),

options as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        string_agg(option_code, ',' order by option_code) as option_codes,
        sum(amount) as options_amount
    from {{ ref('stg_admin_core__contract_option') }}
    where not is_deleted
    group by 1
),

surcharges as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        string_agg(surcharge_code, ',' order by surcharge_code) as surcharge_codes,
        count(*) as surcharge_count,
        sum(amount) as surcharge_amount
    from {{ ref('stg_admin_core__contract_surcharge') }}
    where not is_deleted
    group by 1
)

select
    c.contract_key,
    c.term_months,
    c.term_miles,
    c.term_band,
    c.term_days,
    c.expiration_mileage,
    c.claim_wait_days,
    c.claim_wait_miles,
    c.deductible_amount,
    c.deductible_name,
    c.coverage_limit,
    o.option_codes,
    coalesce(o.options_amount, 0) as options_amount,
    s.surcharge_codes,
    coalesce(s.surcharge_count, 0) as surcharge_count,
    coalesce(s.surcharge_amount, 0) as surcharge_amount,
    {{ audit_columns() }}
from c
left join options as o using (contract_key)
left join surcharges as s using (contract_key)
