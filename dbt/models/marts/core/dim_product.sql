-- Product hierarchy at plan x program grain: category > subcategory > product (plan year) >
-- plan > program. Built from the complete Admin Core catalog, so every legacy vintage matches.
select
    md5('product|' || cast(pl.plan_id as varchar) || '|' || cast(pg.program_id as varchar)) as product_key,
    pl.plan_id,
    pg.program_id,
    p.product_id,
    c.category_code,
    c.category_name as category,
    s.subcategory_code,
    s.subcategory_name as subcategory,
    p.product_code,
    p.product_name as product,
    pl.plan_code,
    pl.plan_name as plan,
    pg.program_code,
    pg.program_name as program,
    p.plan_year,
    coalesce(p.is_cancellable, 0) = 1 as is_cancellable,
    p.contract_prefix,
    pl.deductible_amount,
    pl.deductible_name,
    -- VSC coverage tiers, low to high (powertrain to exclusionary); null outside VSC.
    case s.subcategory_code
        when 'VSC-PT' then 1 when 'VSC-SC' then 2 when 'VSC-EN' then 3 when 'VSC-EX' then 4
    end as tier_rank,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__program') }} as pg
inner join {{ ref('stg_admin_core__plan') }} as pl using (plan_id)
inner join {{ ref('stg_admin_core__product') }} as p using (product_id)
left join {{ ref('stg_admin_core__product_subcategory') }} as s using (subcategory_id)
left join {{ ref('stg_admin_core__product_category') }} as c on s.category_id = c.category_id
where not pg.is_deleted and not pl.is_deleted and not p.is_deleted

union all

select
    '-1', null, null, null, 'UNKNOWN', 'Unknown', 'UNKNOWN', 'Unknown', 'UNKNOWN', 'Unknown product',
    'UNKNOWN', 'Unknown plan', 'UNKNOWN', 'Unknown program', null, false, null, null, null, null,
    current_timestamp
