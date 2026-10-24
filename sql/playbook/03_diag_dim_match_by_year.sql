-- pattern:  diagnostics round 1, dim match rate by entered year
-- question: Does every contract find its product in the product dim, year by year?
-- trap:     the product dim stopped refreshing, so an INNER join silently drops the newest vintage and
--           every downstream count looks like a sales drop. count(*) - count(dim key) on a LEFT join,
--           sliced by entered year, shows exactly where the dim goes stale.

set statement_timeout = '60s';

select extract(year from c."EnteredDate")::int as entered_year,
       count(*) as contracts,
       count(p."ProductId") as matched,
       count(*) - count(p."ProductId") as unmatched,
       round(100.0 * count(p."ProductId") / count(*), 1) as match_pct
from legacy_ops.tbl_contract c
left join legacy_ops.vw_dim_products p on p."ProductId" = c."fxProductId"
group by 1
order by 1;
