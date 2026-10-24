-- pattern:  freshness probe on a huge table
-- question: How current are the contract table and the earnings series (3M+ rows, no index on the date
--           id) before I build a report on them? Is the latest month there yet?
-- trap:     max() over the full earnings table is a full scan, and min() lands on the 19000101 sentinel,
--           so "earliest = 1900" looks like a load bug. tablesample system reads a few pages, repeatable()
--           keeps the probe deterministic, and the sentinel is counted on its own.

set statement_timeout = '30s';

select max("EnteredDate") as approx_latest_entered,
       min("EnteredDate") as approx_earliest_entered,
       count(*) as sampled_rows
from legacy_ops.tbl_contract tablesample system (5) repeatable (17);

select max("fxEarningDateId") as approx_latest,
       min("fxEarningDateId") filter (where "fxEarningDateId" <> 19000101) as approx_earliest_real,
       count(*) filter (where "fxEarningDateId" = 19000101) as sampled_sentinel_rows,
       count(*) as sampled_rows
from legacy_finance.cube_earned_disbursement tablesample system (1) repeatable (17);
