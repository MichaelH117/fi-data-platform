-- Every cash receipt lands in exactly one aging bucket, so the late-fee bill ties to the cash fact.
with mart as (
    select sum(receipts) as receipts, sum(remitted_amount) as amount, sum(late_fee_billed) as late_fee
    from {{ ref('mart_remittance_late_fees') }}
),

cash as (
    select count(*) as receipts, sum(amount) as amount, sum(late_fee) as late_fee
    from {{ ref('fct_cash_transaction') }}
)

select m.*, c.receipts as cash_receipts, c.amount as cash_amount, c.late_fee as cash_late_fee
from mart as m, cash as c
where m.receipts <> c.receipts
    or abs(m.amount - c.amount) > 0.01
    or abs(m.late_fee - c.late_fee) > 0.01
