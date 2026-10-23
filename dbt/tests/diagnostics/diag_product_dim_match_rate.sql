-- Diagnostic: product-dimension match rate by entered year >= var min_product_match_rate (99.5%).
-- Guards the stale-product-dim trap: an incomplete product table silently drops recent vintages
-- on an inner join. Returns one row per failing entered year.
with by_year as (
    select
        entered_year,
        count(*) as contracts,
        count(*) filter (where product_key <> '-1') as matched
    from {{ ref('dim_contract') }}
    where contract_key <> '-1'
    group by 1
)

select
    entered_year,
    contracts,
    matched,
    matched / contracts as match_rate,
    {{ var('min_product_match_rate') }} as threshold
from by_year
where matched / contracts < {{ var('min_product_match_rate') }}
