-- Calibration control total: the dealer-cost split of every transaction sums to the transaction.
-- The source rounds each of up to six buckets separately, so a single transaction may differ by a
-- few cents; the per-transaction tolerance is $0.05 and the book-wide tolerance is $1.00.
with split as (
    select txn_key, sum(amount) as split_amount
    from {{ ref('fct_disbursement') }}
    group by 1
),

compared as (
    select t.txn_key, t.txn_type, t.amount, coalesce(s.split_amount, 0) as split_amount
    from {{ ref('fct_transaction') }} as t
    left join split as s using (txn_key)
    where t.txn_type <> 'CASH'
)

select 'txn: ' || txn_key as check_name, amount as expected, split_amount as actual
from compared
where abs(amount - split_amount) > 0.05

union all

select 'book total', sum(amount), sum(split_amount)
from compared
having abs(sum(amount) - sum(split_amount)) > 1.00
