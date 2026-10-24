-- pattern:  diagnostics round 2, key-bridge repair
-- question: What share of the dealer stock list can I tie to a contract, and how much does normalizing
--           the VIN recover?
-- trap:     the stock list has no contract id and the VIN was typed by hand (lower case, padded spaces).
--           An exact join loses about a fifth of the rows. A correlated upper(trim()) lookup per row is
--           non-sargable and runs for minutes. Normalize ONCE in a CTE, then hash join: same answer, fast.

set statement_timeout = '60s';

explain with s as (
    select stock_number,
           upper(trim("VIN")) as vin_key
    from legacy_ops.tbl_stock_number
), c as (
    select distinct upper(trim("VIN")) as vin_key
    from legacy_ops.tbl_contract
    where "VIN" is not null
)
select count(*)
from s
join c using (vin_key);

with s as (
    select stock_number,
           "VIN" as vin_raw,
           upper(trim("VIN")) as vin_key
    from legacy_ops.tbl_stock_number
), c as (
    select distinct "VIN" as vin_raw,
           upper(trim("VIN")) as vin_key
    from legacy_ops.tbl_contract
    where "VIN" is not null
), exact as (
    select count(distinct s.stock_number) as n
    from s
    join c on c.vin_raw = s.vin_raw
), fixed as (
    select count(distinct s.stock_number) as n
    from s
    join c on c.vin_key = s.vin_key
)
select (
    select count(*)
    from s
) as stock_rows,
       exact.n as exact_match,
       fixed.n as normalized_match,
       round(100.0 * exact.n / (
           select count(*)
           from s
       ), 1) as exact_pct,
       round(100.0 * fixed.n / (
           select count(*)
           from s
       ), 1) as normalized_pct
from exact, fixed;
