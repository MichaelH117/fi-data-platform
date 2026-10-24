-- database: modern
-- timeout: 60s
-- question: Contracts by product type and the latest plan year, on the modern star schema.
-- fix:      dim_product is complete and every fact key falls back to the unknown member (-1), so an inner
--           join cannot drop a contract; a relationships test fails the build if it ever would.
set statement_timeout = '60s';

select count(*) as contracts_counted, max(p.plan_year) as latest_plan_year_seen,
       count(distinct p.category_code) as product_types
from core.dim_contract c
join core.dim_product p on p.product_key = c.product_key;
