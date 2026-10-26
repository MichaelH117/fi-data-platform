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
    -- V6 edit 16: parsed once in stg_admin_core__product; edit 3 program type; edit 6 compliance family.
    p.model_year,
    p.program_suffix,
    p.is_do_not_use,
    p.product_base_name,
    p.program_type,
    coalesce(f.compliance_family, 'UNMAPPED') as compliance_family,
    x.short_code,
    coalesce(p.is_cancellable, 0) = 1 as is_cancellable,
    p.contract_prefix,
    pl.deductible_amount,
    pl.deductible_name,
    -- VSC coverage tiers, low to high (powertrain to exclusionary); null outside VSC.
    case s.subcategory_code
        when 'VSC-PT' then 1 when 'VSC-SC' then 2 when 'VSC-EN' then 3 when 'VSC-EX' then 4
    end as tier_rank,
    -- V7 edit 50: external reporting line and bundle flag. A bundle is the bundle code or any product
    -- whose name says bundle / pkg / package / combo (some bundles are set up under other subcategories).
    coalesce(x.is_bundle, false) or coalesce(regexp_matches(lower(p.product_base_name), '\b(bundle|pkg|package|combo)\b'), false) as is_bundle,
    coalesce(x.external_product_line, 'UNMAPPED') as external_product_line,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__program') }} as pg
inner join {{ ref('stg_admin_core__plan') }} as pl using (plan_id)
inner join {{ ref('stg_admin_core__product') }} as p using (product_id)
left join {{ ref('stg_admin_core__product_subcategory') }} as s using (subcategory_id)
left join {{ ref('stg_admin_core__product_category') }} as c on s.category_id = c.category_id
left join {{ ref('compliance_product_family') }} as f on f.subcategory_code = s.subcategory_code
left join {{ ref('product_taxonomy_crosswalk') }} as x on x.product_code = p.product_code
where not pg.is_deleted and not pl.is_deleted and not p.is_deleted

union all

select
    '-1', null, null, null, 'UNKNOWN', 'Unknown', 'UNKNOWN', 'Unknown', 'UNKNOWN', 'Unknown product',
    'UNKNOWN', 'Unknown plan', 'UNKNOWN', 'Unknown program', null,
    null, null, false, 'Unknown product', 'UNKNOWN', 'UNMAPPED', 'UNKNOWN',
    false, null, null, null, null, false, 'UNMAPPED', current_timestamp
