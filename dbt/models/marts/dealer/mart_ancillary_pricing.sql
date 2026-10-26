-- V7 edit 48: ancillary pricing and markup. "What each store charges for add-ons, and its markup."
-- Grain (GROUPING SETS): dealer group x dealer x ancillary subcategory, with subtotal rows
--   DEALER_TOTAL       dealer x "(all products)": every add-on a store sold
--   GROUP_SUBCATEGORY  dealer group x "(all dealers)" per subcategory, across the group's stores
--   GRAND_TOTAL        "(all groups)" x "(all products)"
-- Written business (DEALER_PRODUCTION status set), ancillary and limited-warranty categories only.
--   retail = retail_price (what the customer paid), dealer_cost = coverage_cost (what the store pays),
--   markup = retail - dealer cost; markup_pct = markup / dealer cost (blank with no cost, rule 1).
with sold as (
    select
        coalesce(g.group_name, 'No dealer group') as dealer_group,
        coalesce(d.dealer_number, c.dealer_number) as dealer_number,
        d.dealer_name,
        c.subcategory_code,
        coalesce(p.subcategory, c.subcategory_code) as subcategory,
        coalesce(f.retail_price, 0) as retail_price,
        coalesce(f.coverage_cost, 0) as coverage_cost
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'DEALER_PRODUCTION' and s.status = c.status
    left join {{ ref('dim_dealer') }} as d on d.dealer_key = c.dealer_key
    left join {{ ref('dim_dealer_group') }} as g on g.dealer_group_key = d.dealer_group_key
    left join {{ ref('dim_product') }} as p on p.product_key = c.product_key
    left join {{ ref('dim_contract_finance') }} as f on f.contract_key = c.contract_key
    where c.contract_key <> '-1' and c.category_code in ('ANCILLARY', 'LIMITED_WARRANTY')
),

rolled as (
    select
        dealer_group,
        dealer_number,
        any_value(dealer_name) as dealer_name,
        subcategory_code,
        any_value(subcategory) as subcategory,
        grouping(dealer_group, dealer_number, subcategory_code) as grouping_id,
        count(*) as contracts,
        avg(retail_price) as avg_retail_price,
        min(retail_price) as min_retail_price,
        max(retail_price) as max_retail_price,
        sum(retail_price) as retail_total,
        sum(coverage_cost) as dealer_cost_total
    from sold
    group by grouping sets (
        (dealer_group, dealer_number, subcategory_code),
        (dealer_group, dealer_number),
        (dealer_group, subcategory_code),
        ()
    )
)

select
    case grouping_id when 0 then 'DETAIL' when 1 then 'DEALER_TOTAL' when 2 then 'GROUP_SUBCATEGORY'
         else 'GRAND_TOTAL' end as row_type,
    coalesce(dealer_group, '(all groups)') as dealer_group,
    coalesce(dealer_number, '(all dealers)') as dealer_number,
    case when grouping_id in (0, 1) then dealer_name else '(all dealers)' end as dealer_name,
    coalesce(subcategory_code, '(all products)') as subcategory_code,
    case when grouping_id in (0, 2) then subcategory else '(all products)' end as subcategory,
    contracts,
    round(avg_retail_price, 2) as avg_retail_price,
    round(min_retail_price, 2) as min_retail_price,
    round(max_retail_price, 2) as max_retail_price,
    round(retail_total, 2) as retail_total,
    round(dealer_cost_total, 2) as dealer_cost_total,
    round(retail_total - dealer_cost_total, 2) as markup_total,
    round({{ ratio_or_blank('retail_total - dealer_cost_total', 'dealer_cost_total') }}, 4) as markup_pct,
    {{ audit_columns() }}
from rolled
