-- DEPRECATED V1 shape (gold.dim_product): one row per V1 product code = subcategory x term months
-- (for example 'VSC-PT-60'). coverage_tier keeps the V1 three-tier vocabulary so V1 pages still
-- render: Powertrain (VSC-PT), Gold (VSC-SC, VSC-EN), Platinum (VSC-EX); other products keep their
-- subcategory name. Use core.dim_product (four VSC tiers, full hierarchy) instead.
{{ config(schema='gold', alias='dim_product') }}
with codes as (
    select distinct
        k.subcategory_code,
        t.term_months,
        case k.subcategory_code
            when 'VSC-PT' then 'Powertrain'
            when 'VSC-SC' then 'Gold'
            when 'VSC-EN' then 'Gold'
            when 'VSC-EX' then 'Platinum'
            else p.subcategory
        end as coverage_tier,
        case k.subcategory_code
            when 'VSC-PT' then 1 when 'VSC-SC' then 2 when 'VSC-EN' then 2 when 'VSC-EX' then 3
        end as tier_rank
    from {{ ref('dim_contract') }} as k
    inner join {{ ref('dim_contract_terms') }} as t using (contract_key)
    left join {{ ref('dim_product') }} as p using (product_key)
)

select
    md5('v1_product|' || subcategory_code || '-' || cast(term_months as varchar)) as product_key,
    subcategory_code || '-' || cast(term_months as varchar) as product_code,
    any_value(coverage_tier) as coverage_tier,
    any_value(tier_rank) as tier_rank,
    term_months
from codes
where subcategory_code is not null
group by subcategory_code, term_months
