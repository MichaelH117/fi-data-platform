-- database: legacy
-- timeout: 60s
-- question: Stock-number match rate, second try: the fast exact-text join everyone falls back to.
-- trap:     exact text equality on a hand-typed VIN misses every lower-cased or space-padded row, so the
--           rate looks like a data problem when it is a key problem.
set statement_timeout = '60s';

select round(100.0 * count(distinct s.stock_number) filter (where c."VIN" is not null) / count(distinct s.stock_number), 2)
           as match_rate_pct,
       count(distinct s.stock_number) as stock_rows,
       count(distinct s.stock_number) filter (where c."VIN" is null) as unmatched_rows
from legacy_ops.tbl_stock_number s
left join (select distinct "VIN" from legacy_ops.tbl_contract where "VIN" is not null) c on c."VIN" = s."VIN";
