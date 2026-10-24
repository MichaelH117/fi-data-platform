-- database: legacy
-- timeout: 60s
-- question: Stock-number match rate: share of the dealer stock list that finds its contract by VIN, first
--           the way the old lookup view did it (a correlated, text-normalized lookup per stock row).
-- trap:     no key: the stock list has no contract id and its VIN was typed by hand. Normalizing both sides
--           with upper(trim()) inside a correlated subquery defeats every index and runs per row.
set statement_timeout = '60s';

select round(100.0 * count(*) filter (where exists (
           select 1 from legacy_ops.tbl_contract c
           where upper(trim(c."VIN")) = upper(trim(s."VIN")) and c."VIN" is not null
             and c."fxDealerId" in (select d."DealerId" from legacy_ops.tbl_dealer d
                                    where upper(trim(d."Number")) = upper(trim(s."DealerNumber")))))
       / count(*), 2) as match_rate_pct
from legacy_ops.tbl_stock_number s;
