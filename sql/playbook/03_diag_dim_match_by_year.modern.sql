-- pattern:  diagnostics round 1, dim match rate by entered year (modern star schema)
-- question: The same question on the modern platform: does every contract resolve a product?
-- trap:     none expected. The modern product dim is rebuilt on every run and the contract dim carries
--           entered_year, so the match is 100% in every year (an unknown member catches any gap).

set statement_timeout = '60s';

select c.entered_year,
       count(*) as contracts,
       count(p.product_key) as matched,
       count(*) - count(p.product_key) as unmatched,
       round(100.0 * count(p.product_key) / count(*), 1) as match_pct
from core.dim_contract c
left join core.dim_product p on p.product_key = c.product_key
where c.entered_year is not null
group by 1
order by 1;
