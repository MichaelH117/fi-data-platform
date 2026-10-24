-- pattern:  diagnostics round 4, fan-out guard
-- question: If I join disbursements to transactions to get coverage cost, do I still have one row per
--           disbursement, and what does a fan-out do to the total?
-- trap:     the transaction cube carries re-sent rows (the same "TransactionId" twice). A plain join
--           duplicates those disbursements and inflates coverage cost. Guard: count rows before and after
--           every join, and dedupe the bridge first (keep the max cash id per transaction).

set statement_timeout = '60s';

explain select count(*)
from legacy_finance.cube_disbursement d
join legacy_finance.cube_transaction t on t."TransactionId" = d."fxTransactionId";

with t1 as (
    select "TransactionId",
           max("fxCashTransactionId") as "fxCashTransactionId"
    from legacy_finance.cube_transaction
    group by 1
), base as (
    select count(*) as n,
           sum("Amount") as amt
    from legacy_finance.cube_disbursement
    where "fxTransactionTypeId" in (1, 3, 4)
), naive as (
    select count(*) as n,
           sum(d."Amount") as amt
    from legacy_finance.cube_disbursement d
    join legacy_finance.cube_transaction t on t."TransactionId" = d."fxTransactionId"
    where d."fxTransactionTypeId" in (1, 3, 4)
), dedup as (
    select count(*) as n,
           sum(d."Amount") as amt
    from legacy_finance.cube_disbursement d
    join t1 on t1."TransactionId" = d."fxTransactionId"
    where d."fxTransactionTypeId" in (1, 3, 4)
)
select base.n as rows_before,
       naive.n as rows_naive_join,
       dedup.n as rows_deduped_join,
       naive.n - dedup.n as fanned_out_rows,
       naive.amt - dedup.amt as inflated_coverage_cost,
       base.amt - dedup.amt as unmatched_cost
from base, naive, dedup;
