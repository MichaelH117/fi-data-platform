-- Contract money at sale (one row per reportable contract). dealer_markup = retail - dealer cost.
-- Two bases are kept side by side: coverage_cost (dealer cost) and retail_price.
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
)

select
    c.contract_key,
    c.purchase_price,
    c.coverage_cost,
    c.retail_price,
    c.retail_price - c.coverage_cost as dealer_markup,
    c.msrp,
    c.finance_type,
    c.finance_amount,
    c.apr,
    l.lienholder_code,
    l.lienholder_name,
    c.admin_fee,
    c.reserve_amount,
    c.commission,
    c.insurer_fee,
    c.premium_tax,
    c.profit_share_reserve,
    c.is_amount_sign_fixed,
    -- V6 edit 3: the contract's program type (= its product's; legacy rows take the catalog product's).
    coalesce(c.program_type, 'UNKNOWN') as program_type,
    {{ audit_columns() }}
from c
left join {{ ref('stg_admin_core__lienholder') }} as l using (lienholder_id)
