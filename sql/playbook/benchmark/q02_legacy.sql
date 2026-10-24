-- database: legacy
-- timeout: 60s
-- question: Contracts by product type, and the latest plan year on the book.
-- trap:     the product dim stopped refreshing after one plan year. The house pattern is an INNER join to it,
--           so every contract on a newer plan silently disappears and the "latest" year looks a year old.
set statement_timeout = '60s';

select count(*) as contracts_counted,
       max(p."PlanYear") as latest_plan_year_seen,
       count(distinct p."ProductType") as product_types,
       (select count(*) from legacy_ops.vw_dim_contracts) as contracts_on_book
from legacy_ops.vw_dim_contracts c
join legacy_ops.vw_dim_products p on p."ProductId" = c."fxProductId";
