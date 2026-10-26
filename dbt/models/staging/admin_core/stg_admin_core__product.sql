-- Admin Core product: latest version per product_id. Products by plan year.
-- V6 edit 16: product_name reads "<name> <year> - <SUFFIX>". The name is parsed here, once:
--   model_year      the 4-digit year in the name (falls back to plan_year when absent)
--   program_suffix  FI | AG | PT (program type) or DNU (do not use); null on an unsuffixed name
--   is_do_not_use   true on a "- DNU" twin, which reuses a live product's code and has no plans
--   product_base_name  the name without year and suffix (one code can carry several names)
with p as (
    {{ stg_admin_core('product', 'product_id') }}
),

parsed as (
    select
        *,
        nullif(regexp_extract(product_name, '\s-\s*([A-Za-z]+)\s*$', 1), '') as name_suffix,
        try_cast(nullif(regexp_extract(product_name, '\b((?:19|20)\d{2})\b', 1), '') as integer) as name_year
    from p
)

select
    * exclude (name_suffix, name_year),
    coalesce(name_year, cast(plan_year as integer)) as model_year,
    upper(name_suffix) as program_suffix,
    coalesce(upper(name_suffix) = 'DNU', false) as is_do_not_use,
    trim(regexp_replace(regexp_replace(product_name, '\s-\s*[A-Za-z]+\s*$', ''), '\s*\b(19|20)\d{2}\b\s*', ' ', 'g'))
        as product_base_name
from parsed
