-- Legacy product table. STALE by design: it stops before the last legacy plan year, so it is
-- kept only for the product-dim match-rate diagnostic. Contracts take their product from the
-- complete Admin Core catalog instead (int_contracts_conformed).
with latest as (
    {{ latest_version(source('legacy_admin', 'products'), 'product_code, plan_year, term_months') }}
)

select
    * replace (upper(trim(product_code)) as product_code)
from latest
