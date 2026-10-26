-- Diagnostic (V6 edit 15): booked coverage cost differs from the rate card. Grain: one row per Admin
-- Core contract whose booked coverage cost, less its surcharges and options, is not the card rate of
-- the rate it was booked on (or whose rate is missing from the card). Empty = every contract was
-- priced from the card. Legacy contracts have no rate id and are out of scope.
-- V6 interim: the contract's rate_id is read from staging until a core dim carries it.
with booked as (
    select contract_number, rate_id
    from {{ ref('stg_admin_core__contract') }}
    where not is_deleted
),

contracts as (
    select
        k.contract_key,
        k.contract_number,
        k.tenant_key,
        k.dealer_key,
        k.entered_date,
        b.rate_id,
        f.coverage_cost as booked_coverage_cost,
        coalesce(t.surcharge_amount, 0) as surcharge_amount,
        coalesce(t.options_amount, 0) as options_amount
    from {{ ref('dim_contract') }} as k
    inner join booked as b using (contract_number)
    left join {{ ref('dim_contract_finance') }} as f using (contract_key)
    left join {{ ref('dim_contract_terms') }} as t using (contract_key)
    where k.source_system = 'admin_core'
)

select
    c.contract_key,
    c.contract_number,
    c.tenant_key,
    c.dealer_key,
    c.entered_date,
    c.rate_id,
    r.product_code,
    r.term_months,
    r.odometer_band,
    r.class_code,
    c.booked_coverage_cost,
    c.surcharge_amount,
    c.options_amount,
    r.rate as rate_card_cost,
    round(c.booked_coverage_cost - c.surcharge_amount - c.options_amount - coalesce(r.rate, 0), 2) as difference,
    case when r.rate_id is null then 'RATE_NOT_ON_CARD' else 'COST_DIFFERS' end as exception_type,
    {{ audit_columns() }}
from contracts as c
left join {{ ref('mart_rate_card') }} as r using (rate_id)
where r.rate_id is null
    or abs(c.booked_coverage_cost - c.surcharge_amount - c.options_amount - r.rate) > 0.01
